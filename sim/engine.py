"""Simulation engine: climate, day planning, timeline execution, recording and ground-truth labels."""

import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from schema.canonical import EVENT_SCHEMA, SIGNAL_SCHEMA, load_catalogue
from memory.store import write_source_meta
from sim.drive_cycles import trip_profile
from sim.vehicle import Faults, VehicleParams, VehicleSim, VehicleState

GENERATOR_VERSION = "0.1.0"

PARK_SIGNALS = {
    "Vehicle.Powertrain.TractionBattery.Temperature.Average",
    "Vehicle.Powertrain.TractionBattery.Temperature.Max",
    "Vehicle.Exterior.AirTemperature",
    "Vehicle.Powertrain.TractionBattery.StateOfCharge.Current",
}
PARK_PERIOD_S = 900
CHARGE_EXCLUDED = {
    "Vehicle.Speed",
    "Vehicle.TraveledDistance",
    "Vehicle.CurrentLocation.Latitude",
    "Vehicle.CurrentLocation.Longitude",
    "Vehicle.VendorExt.Cabin.HeaterPower",
    "Vehicle.VendorExt.Cabin.AirConditioningPower",
    "Vehicle.ADAS.CruiseControl.IsActive",
}
ADAS_REASONS = ["driver_steering_override", "lane_markings_lost", "sensor_blocked", "system_fault"]
ADAS_REASON_P = np.array([0.5, 0.3, 0.12, 0.08])
TOWING_MASS_KG = 750.0
TOWING_CDA_M2 = 1.2
AC_CHARGER_W = 7400.0
AC_PLUG_BELOW_SOC = 0.8
AC_TARGET_SOC = 0.9


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def uniform_or_value(rng, v):
    if isinstance(v, list) and len(v) == 2:
        return float(rng.uniform(v[0], v[1]))
    return v


# ---------------------------------------------------------------------------
class Climate:
    """Ambient temperature: daily mean (per local day) plus a diurnal sine peaking ~15:00."""

    def __init__(self, tz: ZoneInfo, day0: date, day_means: list[float], diurnal_amp_c: float):
        self.tz = tz
        self.day0_wall = datetime.combine(day0, time(0), tzinfo=timezone.utc).timestamp()
        self.day_means = day_means
        self.amp = diurnal_amp_c
        self._off_cache: dict[int, float] = {}

    def wall_clock(self, t: float) -> float:
        """Local wall-clock time expressed as a naive epoch (seconds)."""
        hour_key = int(t // 3600)
        off = self._off_cache.get(hour_key)
        if off is None:
            off = datetime.fromtimestamp(t, self.tz).utcoffset().total_seconds()
            self._off_cache[hour_key] = off
        return t + off

    def __call__(self, t: float) -> float:
        lt = self.wall_clock(t)
        day = int((lt - self.day0_wall) // 86400)
        day = min(max(day, 0), len(self.day_means) - 1)
        hour = (lt % 86400) / 3600.0
        return self.day_means[day] + self.amp * math.sin(2 * math.pi * (hour - 9.0) / 24.0)


# ---------------------------------------------------------------------------
class Recorder:
    """Buffers canonical rows and writes one Parquet file per UTC day (signals and events)."""

    def __init__(self, out_dir: Path, source_id: str, run_id: str, catalogue: dict):
        self.out_dir = out_dir
        self.source_id = source_id
        self.run_id = run_id
        self.units = {k: v["unit"] for k, v in catalogue["signals"].items()}
        self.periods = {k: int(v["sim_period_s"]) for k, v in catalogue["signals"].items() if "sim_period_s" in v}
        self.day_key: int | None = None
        self.parts: dict[int, int] = {}
        self._reset()
        self.seq = 0
        self.event_seq = 0
        self.n_signals = 0
        self.n_events = 0

    def _reset(self):
        self.ts: list[int] = []
        self.paths: list[str] = []
        self.values: list[float] = []
        self.ev_rows: list[dict] = []

    def _roll(self, t: float):
        key = int(t) // 86400
        if key != self.day_key:
            self.flush()
            self.day_key = key

    def signals(self, t: float, measured: dict, mode: str, dt: float):
        self._roll(t)
        ti = int(t)
        ms = ti * 1000
        for path, val in measured.items():
            if mode == "park":
                if path not in PARK_SIGNALS:
                    continue
                period = PARK_PERIOD_S
            else:
                if mode.startswith("charge") and path in CHARGE_EXCLUDED:
                    continue
                period = self.periods[path]
            if ti % period < dt:
                self.ts.append(ms)
                self.paths.append(path)
                self.values.append(float(val))

    def event(self, t: float, event_type: str, subsystem: str, severity: str, code: str, attrs: dict):
        self._roll(t)
        self.event_seq += 1
        self.ev_rows.append({
            "ts": int(t) * 1000,
            "event_id": f"{self.run_id}-ev{self.event_seq:06d}",
            "event_type": event_type,
            "subsystem": subsystem,
            "severity": severity,
            "code": code,
            "attrs": json.dumps(attrs, sort_keys=True),
            "source_id": self.source_id,
            "scenario_id": self.run_id,
            "source_type": "simulated",
            "seq": self.event_seq,
        })

    @staticmethod
    def _const(value: str, n: int) -> pa.DictionaryArray:
        return pa.DictionaryArray.from_arrays(pa.array(np.zeros(n, dtype=np.int32)), pa.array([value]))

    def flush(self):
        if self.day_key is None:
            return
        day = datetime.fromtimestamp(self.day_key * 86400, timezone.utc).date().isoformat()
        part = self.parts.get(self.day_key, 0)
        self.parts[self.day_key] = part + 1
        n = len(self.ts)
        if n:
            seq = np.arange(self.seq + 1, self.seq + 1 + n, dtype=np.int64)
            self.seq += n
            table = pa.Table.from_arrays(
                [
                    pa.array(self.ts, type=pa.timestamp("ms", tz="UTC")),
                    pa.array(self.paths).dictionary_encode(),
                    pa.array(self.values, type=pa.float64()),
                    pa.array([self.units[p] for p in self.paths]).dictionary_encode(),
                    self._const("valid", n),
                    self._const(self.source_id, n),
                    self._const(self.run_id, n),
                    self._const("simulated", n),
                    pa.array(seq),
                ],
                schema=SIGNAL_SCHEMA,
            )
            d = self.out_dir / "signals" / f"date={day}"
            d.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, d / f"part-{part}.parquet", compression="zstd")
            self.n_signals += n
        if self.ev_rows:
            cols = {name: [r[name] for r in self.ev_rows] for name in EVENT_SCHEMA.names}
            ev = pa.Table.from_pydict(cols, schema=EVENT_SCHEMA)
            d = self.out_dir / "events" / f"date={day}"
            d.mkdir(parents=True, exist_ok=True)
            pq.write_table(ev, d / f"part-{part}.parquet", compression="zstd")
            self.n_events += len(self.ev_rows)
        self._reset()


# ---------------------------------------------------------------------------
class Engine:
    def __init__(self, vehicle_cfg: dict, run_id: str, seed: int, out_dir: Path,
                 start_local: datetime, end_local: datetime, climate: Climate):
        self.cfg = vehicle_cfg
        self.tz = ZoneInfo(vehicle_cfg["timezone"])
        self.run_id = run_id
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.home = tuple(vehicle_cfg["home"])
        self.work = tuple(vehicle_cfg["work"])
        init = vehicle_cfg["initial"]
        state = VehicleState(soc=init["soc"], temp_c=init["temp_c"], odometer_km=init["odometer_km"],
                             lat=self.home[0], lon=self.home[1])
        self.sim = VehicleSim(VehicleParams(), state, self.rng)
        self.climate = climate
        self.start_local = start_local
        self.t = start_local.replace(tzinfo=self.tz).timestamp()
        self.t_end = end_local.replace(tzinfo=self.tz).timestamp()
        self.rec = Recorder(out_dir, vehicle_cfg["source_id"], run_id, load_catalogue())
        self.fault_specs: list[dict] = []
        self.at_home = True
        self.ac_target: float | None = None
        self.charging: dict | None = None
        self.far_point = self.home
        self.current_tag: str | None = None
        self.labels = {
            "run_id": run_id, "seed": seed, "source_id": vehicle_cfg["source_id"],
            "generator_version": GENERATOR_VERSION, "source_type": "simulated",
            "timezone": vehicle_cfg["timezone"], "start_utc": iso(self.t), "end_utc": iso(self.t_end),
            "faults": [], "scenario_windows": {}, "trips": [], "charges": [], "warnings": [],
        }

    # ---- helpers ----------------------------------------------------------
    def local_epoch(self, day: date, hhmm: str) -> float:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return datetime.combine(day, time(hh, mm), tzinfo=self.tz).timestamp()

    def resolve_time(self, spec: str, day0: date) -> float:
        """'D+HH:MM' relative to day0 (local), or 'end' for the end of the run."""
        if spec == "end":
            return self.t_end
        d, hhmm = spec.split("+")
        return self.local_epoch(day0 + timedelta(days=int(d)), hhmm)

    def add_fault(self, spec: dict, day0: date, tag: str, scenario: str):
        params = {k: uniform_or_value(self.rng, v) for k, v in (spec.get("params") or {}).items()}
        f = {"type": spec["type"], "start": self.resolve_time(spec["start"], day0),
             "end": self.resolve_time(spec["end"], day0), "ramp_s": float(spec.get("ramp_days", 0)) * 86400,
             "params": params, "tag": tag, "scenario": scenario}
        self.fault_specs.append(f)
        self.labels["faults"].append({"type": f["type"], "scenario": scenario, "tag": tag,
                                      "start_utc": iso(f["start"]), "end_utc": iso(f["end"]),
                                      "ramp_days": spec.get("ramp_days", 0), "params": params})
        self._extend_window(tag, scenario, f["start"], f["end"])

    def _extend_window(self, tag: str | None, scenario: str | None, a: float, b: float, **extra):
        if not tag:
            return
        w = self.labels["scenario_windows"].setdefault(tag, {"scenario": scenario, "start": a, "end": b})
        w["start"], w["end"] = min(w["start"], a), max(w["end"], b)
        w.update(extra)

    def active_faults(self, t: float, towing: bool) -> list[str]:
        f = Faults()
        names = []
        for fs in self.fault_specs:
            if not fs["start"] <= t < fs["end"]:
                continue
            names.append(fs["type"])
            p = fs["params"]
            if fs["type"] == "cooling_degradation":
                frac = min(1.0, (t - fs["start"]) / fs["ramp_s"]) if fs["ramp_s"] > 0 else 1.0
                f.cooling_effectiveness = 1.0 - (1.0 - p["effectiveness"]) * frac
                f.pump_speed_factor = 1.0 - (1.0 - p["pump_speed_factor"]) * frac
            elif fs["type"] == "temp_avg_bias":
                f.temp_avg_bias_c = p["bias_c"]
            elif fs["type"] == "temp_max_bias":
                f.temp_max_bias_c = p["bias_c"]
            elif fs["type"] == "pump_signal_stuck":
                f.pump_signal_stuck = True
            elif fs["type"] == "pump_signal_missing":
                f.pump_signal_missing = True
        if towing:
            f.extra_mass_kg, f.extra_cda_m2 = TOWING_MASS_KG, TOWING_CDA_M2
            names.append("towing_load")
        self.sim.f = f
        return names

    def _handle_events(self, t: float, events: list, faults: list[str]):
        for etype, attrs in events:
            if etype.startswith("BATT_OVERTEMP"):
                sev = "warning" if etype.endswith("SET") else "info"
                self.rec.event(t, etype, "bms", sev, "P0A7E", attrs)
                if etype.endswith("SET"):
                    self.labels["warnings"].append({"ts_utc": iso(t), "active_faults": faults,
                                                    "tag": self.current_tag})
            else:
                self.rec.event(t, etype, "bms", "info", "", attrs)

    # ---- activities -------------------------------------------------------
    def _stop_charging(self):
        c = self.charging
        if c is None:
            return
        s = self.sim.s
        energy = (s.soc - c["soc0"]) * self.sim.p.capacity_kwh
        self.rec.event(self.t, "CHARGING_STOPPED", "charging", "info", "",
                       {"type": c["type"], "soc_end_pct": round(s.soc * 100, 1),
                        "energy_added_kwh": round(energy, 2)})
        self.labels["charges"].append({"type": c["type"], "requested_power_kw": c["power_w"] / 1000,
                                       "start_utc": iso(c["start"]), "end_utc": iso(self.t),
                                       "soc_start": round(c["soc0"], 3), "soc_end": round(s.soc, 3),
                                       "tag": c["tag"]})
        self._extend_window(c["tag"], None, c["start"], self.t)
        self.charging = None

    def _start_charging(self, kind: str, power_w: float, tag: str | None):
        self.charging = {"type": kind, "power_w": power_w, "start": self.t, "soc0": self.sim.s.soc, "tag": tag}
        self.rec.event(self.t, "CHARGING_STARTED", "charging", "info", "",
                       {"type": kind, "requested_power_kw": power_w / 1000,
                        "soc_start_pct": round(self.sim.s.soc * 100, 1)})

    def idle_until(self, t_until: float):
        while self.t < t_until:
            amb = self.climate(self.t)
            faults = self.active_faults(self.t, towing=False)
            s = self.sim.s
            if self.ac_target is not None and self.at_home and s.soc < self.ac_target:
                if self.charging is None:
                    self._start_charging("AC", AC_CHARGER_W, None)
                dt = 10.0
                res = self.sim.step(dt, "charge_ac", amb, charge_power_w=AC_CHARGER_W)
                mode = "charge_ac"
            else:
                if self.charging is not None:
                    self._stop_charging()
                    self.ac_target = None
                dt = float(min(300.0, max(1.0, t_until - self.t)))
                res = self.sim.step(dt, "park", amb)
                mode = "park"
            self.rec.signals(self.t, res.measured, mode, dt)
            self._handle_events(self.t, res.events, faults)
            self.t += dt

    def charge_dc(self, power_w: float, target_soc: float, tag: str | None, max_minutes: float = 90):
        self._start_charging("DC", power_w, tag)
        t_stop = self.t + max_minutes * 60
        while self.sim.s.soc < target_soc and self.t < t_stop:
            amb = self.climate(self.t)
            faults = self.active_faults(self.t, towing=False)
            res = self.sim.step(10.0, "charge_dc", amb, charge_power_w=power_w)
            self.rec.signals(self.t, res.measured, "charge_dc", 10.0)
            self._handle_events(self.t, res.events, faults)
            self.t += 10.0
        self._stop_charging()

    def _destination(self, dest: str, distance_km: float) -> tuple[float, float]:
        s = self.sim.s
        if dest == "home":
            return self.home
        if dest == "work":
            return self.work
        if dest == "far":
            bearing = self.rng.uniform(0, 2 * math.pi)
            r = distance_km * 0.75 / 111.0
            self.far_point = (self.home[0] + r * math.cos(bearing),
                              self.home[1] + r * math.sin(bearing) / math.cos(math.radians(self.home[0])))
            return self.far_point
        r = self.rng.uniform(2, 6) / 111.0  # errand
        b = self.rng.uniform(0, 2 * math.pi)
        return (s.lat + r * math.cos(b), s.lon + r * math.sin(b) / math.cos(math.radians(s.lat)))

    def drive(self, trip: dict):
        kind, dist = trip["kind"], float(trip["distance_km"])
        towing = bool(trip.get("towing", False))
        tag = trip.get("tag")
        # make sure the trip is feasible: opportunistic DC top-up before departure
        need = dist * (0.40 if towing else 0.25) / self.sim.p.capacity_kwh
        if self.sim.s.soc - need < 0.1 and self.sim.s.soc < 0.8:
            self.charge_dc(100000.0, 0.8, None)
        dest_name = trip.get("dest", "errand")
        dest = self._destination(dest_name, dist)
        v, road = trip_profile(self.rng, kind, dist, towing)
        s = self.sim.s
        t0, odo0 = self.t, s.odometer_km
        self.current_tag = tag
        self.rec.event(self.t, "IGNITION_ON", "body", "info", "", {"soc_pct": round(s.soc * 100, 1)})
        engaged, cooldown, disengagements = False, 0, 0
        heading = 0.0
        amb = self.climate(self.t)
        faults: list[str] = []
        for i in range(len(v)):
            if i % 10 == 0:
                amb = self.climate(self.t)
                faults = self.active_faults(self.t, towing)
                dlat, dlon = dest[0] - s.lat, (dest[1] - s.lon) * math.cos(math.radians(s.lat))
                if math.hypot(dlat, dlon) * 111_000 > 300:
                    heading = math.atan2(dlon, dlat) + self.rng.normal(0, 0.3)
                else:
                    heading += self.rng.normal(0, 0.6)
            a = v[i] - v[i - 1] if i else v[0]
            res = self.sim.step(1.0, "drive", amb, v=v[i], a=a, heading=heading)

            # ADAS (cruise / lane assist) state machine on highway sections
            kmh = v[i] * 3.6
            if not engaged:
                cooldown -= 1
                if road[i] == "highway" and kmh > 65 and cooldown <= 0:
                    engaged = True
                    self.rec.event(self.t, "ADAS_ENGAGED", "adas", "info", "", {"speed_kmh": round(kmh, 1)})
            else:
                reason = None
                if road[i] != "highway" or kmh < 60:
                    reason = "driver_brake_override"
                elif self.rng.random() < 1 / 700:
                    p = ADAS_REASON_P.copy()
                    if amb < 3:
                        p[2] *= 3
                    reason = str(self.rng.choice(ADAS_REASONS, p=p / p.sum()))
                if reason:
                    engaged = False
                    cooldown = int(self.rng.uniform(30, 120))
                    disengagements += 1
                    self.rec.event(self.t, "ADAS_DISENGAGED", "adas", "info", "",
                                   {"reason": reason, "speed_kmh": round(kmh, 1)})
            res.measured["Vehicle.ADAS.CruiseControl.IsActive"] = 1.0 if engaged else 0.0

            self.rec.signals(self.t, res.measured, "drive", 1.0)
            self._handle_events(self.t, res.events, faults)
            self.t += 1.0
        self.rec.event(self.t, "IGNITION_OFF", "body", "info", "",
                       {"soc_pct": round(s.soc * 100, 1), "trip_km": round(s.odometer_km - odo0, 2)})
        self.at_home = dest_name == "home"
        trip_id = f"trip{len(self.labels['trips']) + 1:04d}"
        self.labels["trips"].append({
            "trip_id": trip_id, "start_utc": iso(t0), "end_utc": iso(self.t), "kind": kind, "dest": dest_name,
            "distance_km": round(s.odometer_km - odo0, 2), "towing": towing,
            "city_s": int((road == "city").sum()), "highway_s": int((road == "highway").sum()),
            "adas_disengagements": disengagements, "tag": tag,
        })
        if tag:
            self._extend_window(tag, None, t0, self.t)
        dc = trip.get("dc_charge")
        if dc:
            self.charge_dc(float(dc["power_w"]), float(dc["target_soc"]), tag)
        elif self.sim.s.soc < 0.25 and not self.at_home:
            self.charge_dc(100000.0, 0.8, None)
        self.current_tag = None

    # ---- run ----------------------------------------------------------------
    def run(self, planner, n_days: int):
        """planner(day_index, date) -> list of trip dicts with 'depart' (HH:MM) and optional 'tag'."""
        day0 = self.start_local.date()
        for k in range(n_days):
            d = day0 + timedelta(days=k)
            for trip in planner(k, d):
                dep = self.local_epoch(d, trip["depart"]) + float(trip.get("jitter_s", 0))
                if dep >= self.t_end:
                    break
                self.idle_until(max(dep, self.t + 60))
                self.drive(trip)
            if self.at_home and self.sim.s.soc < AC_PLUG_BELOW_SOC:
                self.ac_target = AC_TARGET_SOC
        self.idle_until(self.t_end)
        self._stop_charging()
        self.rec.flush()
        write_source_meta(self.rec.out_dir, source_id=self.cfg["source_id"], run_id=self.run_id,
                          source_type="simulated", timezone=self.cfg["timezone"],
                          description="DriveTrace synthetic EV (sim/scenarios.yaml)")

    def write_labels(self, path: Path, extra: dict):
        self.labels.update(extra)
        for w in self.labels["scenario_windows"].values():
            w["start_utc"], w["end_utc"] = iso(w.pop("start")), iso(w.pop("end"))
        self.labels["counts"] = {"signal_rows": self.rec.n_signals, "events": self.rec.n_events}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.labels, indent=2), encoding="utf-8")
