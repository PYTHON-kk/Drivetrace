"""Physical-model fit and counterfactual check (solution_journey §8.7, FR-DX-03).

The declared model is the lumped thermal model in sim/vehicle.py:
    C·dT/dt = I²·R(T, SoC) − k_cool·eff·flow·(T − T_sink) − k_amb·(T − T_amb)
driven by the *observed* current, state of charge, ambient temperature and pump signals of the target window.

1. `fit`: find the cooling effectiveness `eff` that reproduces the observed average temperature. This gives a
   baseline-free reference (eff ≈ 1 = cooling as declared), used as the feature `model_effectiveness`.
2. `check`: re-run with one candidate fault removed (healthy cooling, or baseline load) and everything else
   fixed. If the observed temperatures crossed the BMS warning rule and the counterfactual does not, the fault
   is a but-for cause *within this model*.

IMPORTANT: for the synthetic scenarios the model is the same one that generated the data, so a pass only
shows the method works mechanically; it is not independent evidence. On real vehicles the model must be
calibrated first, and a poor fit (rmse_c above MAX_RMSE_C) blocks both uses.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from diagnosis.window import AMBIENT, CURRENT, PUMP_CMD, PUMP_RPM, SOC, T_AVG
from physics.thermal_model import VehicleParams, VehicleSim, VehicleState, resistance

MAX_RMSE_C = 1.0
MODEL = "physics/thermal_model.py lumped thermal model (same equations as the simulator that generated the synthetic data)"


@dataclass
class ModelInputs:
    i_dis: np.ndarray
    soc: np.ndarray
    amb: np.ndarray
    sink: np.ndarray
    t_avg: np.ndarray
    flow_obs: np.ndarray
    flow_healthy: np.ndarray
    params: VehicleParams


def _series(w, path, t_grid):
    g = w.get(path)
    if g is None or g.empty:
        return None
    t = (g["ts"] - t_grid[0]).dt.total_seconds().to_numpy()
    return np.interp(np.arange(len(t_grid)) * 10.0, t, g["value"].to_numpy(dtype=float))


def prepare(w: dict, start, end, pump_speed_valid: bool) -> ModelInputs | None:
    t_grid = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="10s")
    if len(t_grid) < 6:
        return None
    cur, soc, amb, tav = (_series(w, p, t_grid) for p in (CURRENT, SOC, AMBIENT, T_AVG))
    if any(x is None for x in (cur, soc, amb, tav)):
        return None
    cmd = _series(w, PUMP_CMD, t_grid)
    rpm = _series(w, PUMP_RPM, t_grid) if pump_speed_valid else None
    p = VehicleParams()
    sim = VehicleSim(p, VehicleState(), np.random.default_rng(0))
    on = (cmd > 0.5).astype(float) if cmd is not None else np.ones_like(cur)
    flow_obs = np.clip(rpm / p.pump_max_rpm, 0, 1.2) if rpm is not None else on
    return ModelInputs(-cur, soc, amb, np.array([sim.sink_temp(a) for a in amb]), tav, flow_obs, on, p)


def simulate(m: ModelInputs, eff: float, flow=None, current_scale: float = 1.0, dt: float = 10.0) -> np.ndarray:
    p, flow = m.params, m.flow_obs if flow is None else flow
    out = np.empty(len(m.i_dis))
    temp = m.t_avg[0]
    for k in range(len(m.i_dis)):
        out[k] = temp
        i = m.i_dis[k] * current_scale
        heat = i * i * resistance(p, temp, m.soc[k] / 100.0)
        q_cool = p.k_cool_w_per_k * eff * flow[k] * (temp - m.sink[k])
        q_amb = p.k_amb_w_per_k * (temp - m.amb[k])
        temp += (heat - q_cool - q_amb) / p.thermal_mass_j_per_k * dt
    return out


def fit(m: ModelInputs, grid_cfg) -> dict:
    lo, hi, step = grid_cfg
    best = min((float(np.sqrt(np.mean((simulate(m, e) - m.t_avg) ** 2))), float(e))
               for e in np.arange(lo, hi + 1e-9, step))
    # Effectiveness is only identifiable if the pump ran and the battery was warmer than the coolant sink.
    identifiable = bool(m.flow_obs.mean() > 0.2 and np.mean(m.t_avg - m.sink) > 3)
    return {"model": MODEL, "effectiveness": round(best[1], 2), "rmse_c": round(best[0], 2),
            "identifiable": identifiable, "usable": identifiable and best[0] <= MAX_RMSE_C}


def check(m: ModelInputs | None, model_fit: dict | None, accepted: set, baseline_load_i2: float | None, cfg: dict,
          observed_peak_max: float | None) -> list[dict]:
    if m is None or model_fit is None:
        return [{"hypothesis": h, "verdict": "not_applicable", "reason": "current, state of charge, ambient or "
                 "temperature missing in the window"} for h in accepted]
    c = cfg["counterfactual"]
    thr, thr_max = c["warning_threshold_c"], c["warning_max_c"]
    peak_avg = float(m.t_avg.max())
    gap = max(0.0, (observed_peak_max or peak_avg) - peak_avg)  # observed max-avg gap at the peak

    def crosses(avg_peak: float, max_peak: float) -> bool:  # the BMS rule
        return avg_peak >= thr or max_peak >= thr_max

    observed = crosses(peak_avg, observed_peak_max if observed_peak_max is not None else peak_avg)
    out = []
    for h in sorted(accepted):
        if h == "cooling_degradation":
            cf = simulate(m, 1.0, flow=m.flow_healthy)
            what = "healthy cooling (effectiveness 1.0, pump at nominal speed when commanded)"
        elif h == "elevated_electrical_load" and baseline_load_i2:
            obs_i2 = float(np.mean(m.i_dis ** 2))
            scale = float(np.sqrt(min(1.0, baseline_load_i2 / obs_i2))) if obs_i2 > 0 else 1.0
            cf = simulate(m, model_fit["effectiveness"], current_scale=scale)
            what = f"the baseline load (current x{scale:.2f}) and the fitted cooling"
        else:
            continue
        cf_avg = float(cf.max())
        if not model_fit["usable"]:
            verdict = "model_mismatch" if model_fit["identifiable"] else "not_identifiable"
        elif observed and not crosses(cf_avg, cf_avg + gap):
            verdict = "but_for_cause"
        elif observed:
            verdict = "not_but_for"
        else:
            verdict = "no_threshold_crossing"
        out.append({"hypothesis": h, "counterfactual": what, "verdict": verdict,
                    "observed_peak_avg_c": round(peak_avg, 1), "observed_peak_max_c": observed_peak_max,
                    "counterfactual_peak_avg_c": round(cf_avg, 1), "counterfactual_peak_max_c": round(cf_avg + gap, 1),
                    "threshold_avg_c": thr, "threshold_max_c": thr_max, **{f"fit_{k}": v for k, v in model_fit.items()}})
    return out
