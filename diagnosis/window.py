"""Sessions, signal windows and window features for the diagnostic path (P4).

A *session* is a trip (ignition on/off), a charging session (CHARGING_STARTED/STOPPED) or the parked
time between them. Features are computed deterministically from raw canonical signals; nothing here
uses labels or the simulator.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from memory.store import Store

P = "Vehicle.Powertrain.TractionBattery."
V = "Vehicle.VendorExt.TractionBattery.Cooling."
T_AVG, T_MAX, CURRENT, SOC = P + "Temperature.Average", P + "Temperature.Max", P + "CurrentCurrent", P + "StateOfCharge.Current"
PUMP_CMD, PUMP_RPM, COOLANT = V + "PumpCommand", V + "PumpSpeed", V + "CoolantOutletTemperature"
AMBIENT = "Vehicle.Exterior.AirTemperature"
CORE = [T_AVG, T_MAX, CURRENT, SOC, PUMP_CMD, PUMP_RPM, COOLANT, AMBIENT]
BINARY = {PUMP_CMD}


@dataclass(frozen=True)
class Session:
    kind: str            # trip | charge_dc | charge_ac | park
    start: datetime
    end: datetime
    ref: str             # trip number or charging event id
    attrs: dict

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def sessions(store: Store, key: str) -> list[Session]:
    out = [Session("trip", t_on, t_off, f"trip {n}", {}) for n, t_on, t_off in
           store.query(f"SELECT trip_no, t_on, t_off FROM {store.trips(key)} ORDER BY t_on")]
    rows = store.query(f"""SELECT event_id, event_type, ts, attrs FROM {store.event_index(key)}
                           WHERE event_type IN ('CHARGING_STARTED', 'CHARGING_STOPPED') ORDER BY ts, seq""")
    for (eid, et, ts, attrs), nxt in zip(rows, rows[1:] + [None]):
        if et == "CHARGING_STARTED" and nxt and nxt[1] == "CHARGING_STOPPED":
            a = json.loads(attrs or "{}")
            kind = "charge_dc" if a.get("type") == "DC" else "charge_ac"
            out.append(Session(kind, ts, nxt[2], eid, a))
    return sorted(out, key=lambda s: s.start)


def session_at(all_sessions: list[Session], ts: datetime) -> Session:
    """The trip or charge containing ts (charges win if both match), else the parked gap around it."""
    hits = [s for s in all_sessions if s.start <= ts <= s.end]
    if hits:
        return sorted(hits, key=lambda s: s.kind == "trip")[0]
    before = [s.end for s in all_sessions if s.end < ts]
    after = [s.start for s in all_sessions if s.start > ts]
    start = max(before) if before else ts - timedelta(hours=12)
    end = min(after) if after else ts + timedelta(hours=1)
    return Session("park", start, end, "parked", {})


def load_window(store: Store, key: str, start: datetime, end: datetime, paths=CORE) -> dict[str, pd.DataFrame]:
    ph = ", ".join("?" for _ in paths)
    df = store.con.execute(f"""
        SELECT signal_path, ts, value FROM {store.signals(key)}
        WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) AND ts BETWEEN ? AND ? AND signal_path IN ({ph})
        ORDER BY ts""", [start, end, start, end, *paths]).df()
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return {p: g[["ts", "value"]].reset_index(drop=True) for p, g in df.groupby("signal_path")}


# ---------------------------------------------------------------------- per-signal summary
def _longest_constant_s(g: pd.DataFrame) -> float:
    if len(g) < 2:
        return 0.0
    v = g["value"].to_numpy()
    t = (g["ts"] - g["ts"].iloc[0]).dt.total_seconds().to_numpy()  # unit-safe (DuckDB returns microseconds)
    best, run_start = 0.0, 0
    for i in range(1, len(v)):
        if v[i] != v[i - 1]:
            run_start = i
        else:
            best = max(best, t[i] - t[run_start])
    return best


def summary(g: pd.DataFrame | None, start: datetime, end: datetime) -> dict:
    duration = (end - start).total_seconds()
    if g is None or g.empty:
        return {"n": 0, "coverage": 0.0}
    t = (g["ts"] - pd.Timestamp(start)).dt.total_seconds().to_numpy()
    v = g["value"].to_numpy(dtype=float)
    gaps = np.diff(np.concatenate([[0.0], t, [duration]]))
    interval = float(np.median(np.diff(t))) if len(t) > 1 else duration
    slope = float(np.polyfit(t / 60, v, 1)[0]) if len(t) >= 3 and np.ptp(t) > 0 else None
    return {"n": int(len(v)), "median": float(np.median(v)), "mean": float(v.mean()), "min": float(v.min()),
            "max": float(v.max()), "first": float(v[0]), "last": float(v[-1]), "slope_per_min": slope,
            "distinct": int(len(np.unique(v))), "interval_s": interval,
            "max_gap_s": float(gaps.max()), "coverage": float(min(1.0, len(v) * interval / duration)) if duration else 0.0,
            "longest_constant_s": _longest_constant_s(g), "last_ts": g["ts"].iloc[-1].isoformat()}


def _align(base: pd.DataFrame, other: pd.DataFrame, name: str, tol_s: float = 10) -> pd.DataFrame:
    return pd.merge_asof(base, other.rename(columns={"value": name}), on="ts", direction="nearest",
                         tolerance=pd.Timedelta(seconds=tol_s))


def features(w: dict[str, pd.DataFrame], start: datetime, end: datetime) -> dict:
    """Signal summaries plus derived thermal features for one window."""
    sig = {p: summary(w.get(p), start, end) for p in CORE}
    f: dict = {"signals": sig, "derived": {}}
    d = f["derived"]
    if CURRENT in w and len(w[CURRENT]):
        i = w[CURRENT]["value"].to_numpy()
        d["load_i2"] = float(np.mean(i ** 2))
    if sig[T_AVG]["n"] >= 3:
        d["rise_rate"] = sig[T_AVG]["slope_per_min"]
        d["peak_avg"] = sig[T_AVG]["max"]
    if sig[T_MAX]["n"]:
        d["peak_max"] = sig[T_MAX]["max"]
    if "load_i2" in d and "rise_rate" in d and d["load_i2"] > 2000:
        d["thermal_response"] = d["rise_rate"] / (d["load_i2"] / 1e4)  # degC/min per 10^4 A^2
    if AMBIENT in w and len(w[AMBIENT]):
        d["ambient"] = sig[AMBIENT]["median"]
    if PUMP_CMD in w and len(w[PUMP_CMD]):
        d["pump_duty"] = sig[PUMP_CMD]["mean"]
    if T_AVG in w and T_MAX in w and len(w[T_AVG]) and len(w[T_MAX]):
        a = _align(w[T_AVG].rename(columns={"value": "avg"}), w[T_MAX], "mx").dropna()
        if len(a):
            d["max_minus_avg"] = float(np.median(a["mx"] - a["avg"]))
            d["max_minus_avg_min"] = float((a["mx"] - a["avg"]).min())
            d["max_below_avg_share"] = float(((a["mx"] - a["avg"]) < -0.5).mean())  # persistence, v1.1
    if PUMP_CMD in w and PUMP_RPM in w and len(w[PUMP_RPM]):
        a = _align(w[PUMP_RPM].rename(columns={"value": "rpm"}), w[PUMP_CMD], "cmd").dropna()
        on = a[a["cmd"] > 0.5]
        if len(on) >= 3:
            d["pump_speed_cmd"] = float(np.median(on["rpm"]))
        d["pump_zero_while_commanded_s"] = float(((on["rpm"] < 50).sum()) * (sig[PUMP_RPM]["interval_s"] or 0))
    if all(p in w and len(w[p]) for p in (COOLANT, AMBIENT, T_AVG, PUMP_CMD)):
        a = _align(_align(_align(w[T_AVG].rename(columns={"value": "avg"}), w[COOLANT], "cool"),
                          w[AMBIENT], "amb", 60), w[PUMP_CMD], "cmd").dropna()
        a = a[(a["cmd"] > 0.5) & (a["avg"] - a["amb"] > 3)]
        if len(a) >= 3:
            d["coolant_ratio"] = float(np.median((a["cool"] - a["amb"]) / (a["avg"] - a["amb"])))
    return f
