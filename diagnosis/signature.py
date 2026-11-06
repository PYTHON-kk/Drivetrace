"""Δ-Signature (solution_journey §8.5, FR-ANA-03) and the evidence gate (§8.6, FR-DX-01)."""

import numpy as np

from diagnosis.window import BINARY, CORE, PUMP_CMD, PUMP_RPM, T_AVG, T_MAX


def delta_signature(target: dict, baselines: list[dict], floors: dict) -> dict:
    """Per derived feature: observed, baseline median, absolute/relative difference, robust z."""
    out = {}
    for name, obs in target["derived"].items():
        base = [b["derived"][name] for b in baselines if b["derived"].get(name) is not None]
        e = {"obs": obs}
        if base and obs is not None:
            med = float(np.median(base))
            mad = float(np.median(np.abs(np.array(base) - med))) * 1.4826 if len(base) > 1 else 0.0
            scale = max(mad, floors.get(name, 1e-9))
            e.update(base=med, n_base=len(base), abs=obs - med,
                     rel=(obs - med) / abs(med) if abs(med) > 1e-9 else None)
            if name in floors:
                e.update(z=(obs - med) / scale, scale=scale)
        out[name] = e
    return out


def gate(target: dict, signature: dict, baseline: dict, cfg: dict, warning_present: bool, kind: str = "trip") -> dict:
    """Run the declared checks. Returns status SUFFICIENT | AMBIGUOUS | INSUFFICIENT, checks and flags."""
    g = cfg["gate"]
    sig = target["signals"]
    checks, flags = [], set()

    def check(name, passed, detail, signals=()):
        checks.append({"name": name, "passed": bool(passed), "detail": detail, "signals": list(signals)})
        return passed

    expected = g.get("expected_signals", {}).get(kind, CORE)
    for p in expected:
        s = sig[p]
        if s["n"] == 0:
            check("coverage", False, f"no samples of {p}", [p])
            if p == PUMP_RPM:
                flags.add("pump_speed_missing")
            continue
        ok = s["coverage"] >= g["min_coverage"] and s["max_gap_s"] <= g["max_gap_s"]
        check("coverage", ok, f"{p}: coverage {s['coverage']:.2f}, max gap {s['max_gap_s']:.0f} s", [p])
        if not ok and p == PUMP_RPM:
            flags.add("pump_speed_missing")
        lo, hi = g["physical_range"].get(p, (-1e9, 1e9))
        check("range", lo <= s["min"] and s["max"] <= hi, f"{p}: {s['min']:.1f}..{s['max']:.1f} within {lo}..{hi}", [p])
        if p not in BINARY:
            constant = s["longest_constant_s"]
            stale = constant >= g["stale_constant_s"] and (p != PUMP_RPM or s["median"] > 50 or
                                                            _cmd_toggles(target) or sig[PUMP_CMD].get("min") == 1)
            check("freshness", not stale, f"{p}: longest identical run {constant:.0f} s", [p])
            if stale and p == PUMP_RPM:
                flags.add("pump_speed_stale")
            elif stale:
                flags.add("stale_" + p.rsplit(".", 1)[-1])

    d = target["derived"]
    if d.get("max_below_avg_share") is not None:
        # v1.1: a contradiction must persist; one noisy sample below the average is not a sensor fault
        share = d["max_below_avg_share"]
        ok = share < g["contradiction_min_share"]
        check("contradiction", ok, f"maximum below average temperature in {share:.0%} of samples "
              f"(lowest gap {d['max_minus_avg_min']:.1f} degC)", [T_MAX, T_AVG])
        if not ok:
            flags.add("max_below_avg")
    if "pump_speed_stale" not in flags and "pump_speed_missing" not in flags:
        zero_cmd = d.get("pump_zero_while_commanded_s", 0)
        if zero_cmd >= 60:
            flags.add("pump_zero_while_commanded")
        if sig[PUMP_CMD]["n"] and sig[PUMP_CMD]["max"] < 0.5 and sig[PUMP_RPM]["n"] and sig[PUMP_RPM]["max"] < 50:
            flags.add("pump_off_not_commanded")
        check("command_state", zero_cmd < 60, f"pump at 0 rpm while commanded for {zero_cmd:.0f} s", [PUMP_RPM, PUMP_CMD])

    comparable = baseline["status"] in ("matched", "relaxed")
    check("baseline", comparable, f"baseline {baseline['status']} ({len(baseline['windows'])} windows)")
    if not comparable:
        flags.add("baseline_not_comparable")
    if warning_present:
        flags.add("warning_present")
    deviating = [k for k, e in signature.items() if e.get("z") is not None and abs(e["z"]) >= 2.5]
    if comparable and not deviating and not warning_present and not (flags & {"max_below_avg", "pump_speed_stale", "pump_speed_missing", "pump_zero_while_commanded"}):
        flags.add("no_deviation")
    # Pump at 0 rpm while commanded is either a dead pump or a wrong reading. If heat is still removed normally
    # (heating per unit load and coolant pick-up match the baseline), the pump is working: the reading is wrong.
    if "pump_zero_while_commanded" in flags:
        tr, cr = signature.get("thermal_response", {}).get("rel"), signature.get("coolant_ratio", {}).get("rel")
        if tr is not None and cr is not None and tr < 0.3 and cr > -0.15:
            flags.discard("pump_zero_while_commanded")
            flags.add("pump_reading_contradicts_cooling")
    peak = d.get("peak_avg")
    if peak is not None and peak < cfg["counterfactual"]["warning_threshold_c"] - 4 and not deviating:
        flags.add("temperatures_normal")
    # v1.1: load and cooling only explain ABNORMAL heating - a warning, or peak / rise rate far above the baseline
    zs = [signature.get(k, {}).get("z") for k in ("peak_avg", "rise_rate")]
    if not warning_present and all(z is not None and z < 2.5 for z in zs):
        flags.add("heating_normal")
    # v1.1: a frozen or missing core temperature makes heating undecidable
    for p, tag in ((T_AVG, "Average"), (T_MAX, "Max")):
        if p in expected and sig[p]["n"] == 0:
            flags.add(f"missing_{tag}")
    core_bad = flags & {"stale_Average", "stale_Max", "missing_Average", "missing_Max"}

    required_ok = all(sig[p]["n"] > 0 for p in g["required"])
    thermal_ok = (kind != "park" and all(sig[p]["n"] > 0 for p in g["thermal_signals"])
                  and not (flags & {"pump_speed_stale", "pump_speed_missing", "pump_reading_contradicts_cooling"}))
    if not required_ok:
        status = "INSUFFICIENT"
    elif not comparable or (kind != "park" and not thermal_ok) or core_bad:
        status = "AMBIGUOUS"
    else:
        status = "SUFFICIENT"
    reasons = [c["detail"] for c in checks if not c["passed"]]
    return {"status": status, "checks": checks, "failed": reasons, "flags": sorted(flags),
            "deviating_features": deviating, "cooling_assessable": thermal_ok}


def _cmd_toggles(target: dict) -> bool:
    s = target["signals"][PUMP_CMD]
    return s["n"] > 0 and s["min"] < 0.5 < s["max"]
