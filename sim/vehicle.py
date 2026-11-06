"""Lumped electro-thermal EV model with BMS logic and fault injection (FR-SIM-01/02).

Physics (one thermal node for the pack):
    C * dT/dt = I^2 R(T, SoC) - k_cool * eff * flow * (T - T_sink) - k_amb * (T - T_amb)

This is a deliberately simple, declared model for generating labelled scenarios.
It is not calibrated to any real vehicle; every output is marked source_type="simulated".
"""

import math
from dataclasses import dataclass, field

G = 9.81
AIR_DENSITY = 1.2


@dataclass
class VehicleParams:
    mass_kg: float = 1850.0
    cda_m2: float = 0.62
    crr: float = 0.011
    drivetrain_eff: float = 0.90
    regen_eff: float = 0.65
    regen_limit_w: float = 60000.0
    aux_power_w: float = 350.0
    n_series: int = 96
    capacity_ah: float = 180.0  # ~64 kWh nominal
    r0_ohm: float = 0.12
    thermal_mass_j_per_k: float = 2.2e5
    k_amb_w_per_k: float = 18.0
    k_cool_w_per_k: float = 900.0
    pump_max_rpm: float = 3200.0
    pump_power_w: float = 120.0
    pump_on_c: float = 32.0
    pump_off_c: float = 28.0
    pump_on_charging_c: float = 25.0
    warn_avg_c: float = 42.0
    warn_max_c: float = 44.0
    warn_clear_c: float = 39.0
    derate_c: float = 44.0
    derate_hard_c: float = 47.0
    derate_clear_c: float = 41.0
    chiller_limit_ambient_c: float = 25.0  # above this, chiller sink temperature starts to rise
    heater_w_per_k: float = 250.0
    heater_max_w: float = 5000.0
    ac_w_per_k: float = 120.0
    ac_max_w: float = 2000.0

    @property
    def capacity_kwh(self) -> float:
        return self.capacity_ah * self.n_series * 3.7 / 1000.0


@dataclass
class Faults:
    """Currently active faults. Magnitudes are applied by VehicleSim each step."""

    extra_mass_kg: float = 0.0
    extra_cda_m2: float = 0.0
    cooling_effectiveness: float = 1.0  # 1 = healthy heat transfer
    pump_speed_factor: float = 1.0  # 1 = pump reaches commanded speed
    temp_avg_bias_c: float = 0.0
    temp_max_bias_c: float = 0.0
    pump_signal_stuck: bool = False
    pump_signal_missing: bool = False


@dataclass
class VehicleState:
    soc: float = 0.8
    temp_c: float = 20.0
    odometer_km: float = 12000.0
    pump_cmd: bool = False
    warning: bool = False
    derate: bool = False
    cons_wh_per_km: float = 165.0
    km_accum: float = 0.0
    wh_accum: float = 0.0
    stuck_pump_rpm: float | None = None
    lat: float = 48.137
    lon: float = 11.575


@dataclass
class StepResult:
    true: dict = field(default_factory=dict)
    measured: dict = field(default_factory=dict)  # signal_path -> value (absent = not emitted)
    events: list = field(default_factory=list)  # (event_type, attrs)


def ocv_pack(p: VehicleParams, soc: float) -> float:
    soc = min(max(soc, 0.0), 1.0)
    cell = 3.30 + 0.85 * soc + 0.05 * math.tanh((soc - 0.05) * 20) - 0.05
    return p.n_series * cell


def resistance(p: VehicleParams, temp_c: float, soc: float) -> float:
    cold = 1.0 + 0.025 * max(0.0, 25.0 - temp_c)
    low_soc = 1.0 + 0.5 * max(0.0, 0.2 - soc) / 0.2
    return p.r0_ohm * cold * low_soc


def solve_current(ocv: float, r: float, power_out_w: float) -> float:
    """Discharge-positive current for a requested battery output power P = I*(OCV - I*R)."""
    disc = ocv * ocv - 4.0 * r * power_out_w
    if disc < 0:  # power beyond the pack's limit: clamp to max-power point
        return ocv / (2.0 * r)
    return (ocv - math.sqrt(disc)) / (2.0 * r)


class VehicleSim:
    def __init__(self, params: VehicleParams, state: VehicleState, rng):
        self.p = params
        self.s = state
        self.f = Faults()
        self.rng = rng

    # --- cabin loads -------------------------------------------------------
    def cabin_loads(self, ambient_c: float, occupied: bool) -> tuple[float, float]:
        if not occupied:
            return 0.0, 0.0
        heater = min(self.p.heater_max_w, self.p.heater_w_per_k * max(0.0, 18.0 - ambient_c))
        ac = min(self.p.ac_max_w, self.p.ac_w_per_k * max(0.0, ambient_c - 22.0))
        return heater, ac

    def sink_temp(self, ambient_c: float) -> float:
        """Coolant sink: radiator when cool, chiller (20 degC) when warm, chiller saturating in heat."""
        sink = min(ambient_c + 3.0, 20.0)
        if ambient_c > self.p.chiller_limit_ambient_c:
            sink = 20.0 + 0.5 * (ambient_c - self.p.chiller_limit_ambient_c)
        return sink

    def traction_power(self, v: float, a: float) -> float:
        p, f = self.p, self.f
        mass = p.mass_kg + f.extra_mass_kg
        force = mass * G * p.crr * (1.0 if v > 0.1 else 0.0)
        force += 0.5 * AIR_DENSITY * (p.cda_m2 + f.extra_cda_m2) * v * v + mass * a
        wheel = force * v
        if wheel >= 0:
            return wheel / p.drivetrain_eff
        if v < 2.0:
            return 0.0
        return max(-p.regen_limit_w, wheel * p.regen_eff)

    # --- one integration step ---------------------------------------------
    def step(self, dt: float, mode: str, ambient_c: float, v: float = 0.0, a: float = 0.0,
             charge_power_w: float = 0.0, heading: float = 0.0) -> StepResult:
        """mode: 'drive' | 'charge_ac' | 'charge_dc' | 'park'. charge_power_w is the requested charger power."""
        p, s, f, rng = self.p, self.s, self.f, self.rng
        res = StepResult()

        meas_avg_prev = s.temp_c + f.temp_avg_bias_c
        charging = mode in ("charge_ac", "charge_dc")

        # BMS pump control (acts on the measured average temperature)
        on_threshold = p.pump_on_charging_c if mode == "charge_dc" else p.pump_on_c
        if mode == "park":
            s.pump_cmd = False
        elif not s.pump_cmd and meas_avg_prev > on_threshold:
            s.pump_cmd = True
        elif s.pump_cmd and meas_avg_prev < p.pump_off_c:
            s.pump_cmd = False

        # BMS derate on measured temperature
        derating = meas_avg_prev > p.derate_c or (s.derate and meas_avg_prev > p.derate_clear_c)
        derate_factor = 1.0
        if derating:
            derate_factor = 0.6 if meas_avg_prev < p.derate_hard_c else 0.3
        if derating != s.derate:
            s.derate = derating
            res.events.append(("BMS_POWER_DERATE_SET" if s.derate else "BMS_POWER_DERATE_CLEARED",
                               {"measured_avg_c": round(meas_avg_prev, 2), "factor": derate_factor}))

        heater, ac = self.cabin_loads(ambient_c, occupied=(mode == "drive"))
        pump_rpm_true = 0.0
        if s.pump_cmd:
            pump_rpm_true = max(0.0, p.pump_max_rpm * f.pump_speed_factor + rng.normal(0, 15))
        flow = pump_rpm_true / p.pump_max_rpm
        aux = (p.aux_power_w if mode != "park" else 15.0) + heater + ac + p.pump_power_w * flow

        if mode == "drive":
            power_out = self.traction_power(v, a) + aux
        elif charging:
            taper = 1.0 if s.soc < 0.6 else max(0.1, 1.0 - (s.soc - 0.6) * 2.8)
            limit = derate_factor if mode == "charge_dc" else 1.0
            power_out = -charge_power_w * taper * limit + aux
        else:
            power_out = aux

        ocv = ocv_pack(p, s.soc)
        r = resistance(p, s.temp_c, s.soc)
        i_dis = solve_current(ocv, r, power_out)
        v_term = ocv - i_dis * r
        heat = i_dis * i_dis * r

        t_sink = self.sink_temp(ambient_c)
        q_cool = p.k_cool_w_per_k * f.cooling_effectiveness * flow * (s.temp_c - t_sink)
        q_amb = p.k_amb_w_per_k * (s.temp_c - ambient_c)
        s.temp_c += (heat - q_cool - q_amb) / p.thermal_mass_j_per_k * dt
        s.soc = min(1.0, max(0.0, s.soc - i_dis * dt / (3600.0 * p.capacity_ah)))

        # odometer, consumption EMA and position
        if mode == "drive":
            km = v * dt / 1000.0
            s.odometer_km += km
            s.km_accum += km
            s.wh_accum += power_out * dt / 3600.0
            if s.km_accum >= 1.0:
                s.cons_wh_per_km = 0.97 * s.cons_wh_per_km + 0.03 * max(60.0, s.wh_accum / s.km_accum)
                s.km_accum = s.wh_accum = 0.0
            s.lat += v * dt * math.cos(heading) / 111_111.0
            s.lon += v * dt * math.sin(heading) / (111_111.0 * math.cos(math.radians(s.lat)))

        cap_factor = 1.0 - 0.006 * max(0.0, 20.0 - s.temp_c)
        range_m = s.soc * p.capacity_kwh * 1000.0 * cap_factor / s.cons_wh_per_km * 1000.0

        # ---- measured signals (sensor faults applied here only) ----------
        spread = 1.2 + 0.006 * abs(i_dis)
        meas_avg = s.temp_c + rng.normal(0, 0.15) + f.temp_avg_bias_c
        meas_max = s.temp_c + spread + rng.normal(0, 0.15) + f.temp_max_bias_c
        if flow > 0.05:
            coolant = t_sink + 0.6 * f.cooling_effectiveness * (s.temp_c - t_sink) + rng.normal(0, 0.2)
        else:
            coolant = 0.5 * (s.temp_c + ambient_c) + rng.normal(0, 0.2)
        pump_meas = pump_rpm_true
        if f.pump_signal_stuck:
            if s.stuck_pump_rpm is None:
                s.stuck_pump_rpm = pump_meas
            pump_meas = s.stuck_pump_rpm
        else:
            s.stuck_pump_rpm = None

        m = res.measured
        m["Vehicle.Speed"] = v * 3.6
        m["Vehicle.TraveledDistance"] = s.odometer_km
        m["Vehicle.Exterior.AirTemperature"] = ambient_c + rng.normal(0, 0.3)
        m["Vehicle.CurrentLocation.Latitude"] = s.lat
        m["Vehicle.CurrentLocation.Longitude"] = s.lon
        m["Vehicle.Powertrain.Range"] = range_m
        m["Vehicle.Powertrain.TractionBattery.StateOfCharge.Current"] = s.soc * 100.0
        m["Vehicle.Powertrain.TractionBattery.CurrentVoltage"] = v_term + rng.normal(0, 0.3)
        m["Vehicle.Powertrain.TractionBattery.CurrentCurrent"] = -i_dis + rng.normal(0, 0.2)
        m["Vehicle.Powertrain.TractionBattery.CurrentPower"] = -i_dis * v_term
        m["Vehicle.Powertrain.TractionBattery.Temperature.Average"] = meas_avg
        m["Vehicle.Powertrain.TractionBattery.Temperature.Max"] = meas_max
        m["Vehicle.VendorExt.TractionBattery.Cooling.PumpCommand"] = 1.0 if s.pump_cmd else 0.0
        if not f.pump_signal_missing:
            m["Vehicle.VendorExt.TractionBattery.Cooling.PumpSpeed"] = pump_meas
        m["Vehicle.VendorExt.TractionBattery.Cooling.CoolantOutletTemperature"] = coolant
        m["Vehicle.VendorExt.Cabin.HeaterPower"] = heater
        m["Vehicle.VendorExt.Cabin.AirConditioningPower"] = ac

        # BMS over-temperature warning on measured values
        over = meas_avg > p.warn_avg_c or meas_max > p.warn_max_c
        if over and not s.warning:
            s.warning = True
            res.events.append(("BATT_OVERTEMP_WARNING_SET",
                               {"measured_avg_c": round(meas_avg, 2), "measured_max_c": round(meas_max, 2),
                                "mode": mode}))
        elif s.warning and meas_avg < p.warn_clear_c and meas_max < p.warn_clear_c + 2:
            s.warning = False
            res.events.append(("BATT_OVERTEMP_WARNING_CLEARED",
                               {"measured_avg_c": round(meas_avg, 2), "measured_max_c": round(meas_max, 2)}))

        res.true = {"temp_c": s.temp_c, "heat_w": heat, "q_cool_w": q_cool, "i_dis": i_dis,
                    "power_out_w": power_out, "flow": flow}
        return res
