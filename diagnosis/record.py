"""Evidence record, format v1.0 - FROZEN for P5 (Evidence-Compiled Decoding).

The record is the only input ECD's grammar compiler may use. Every number an answer can show is a *slot*
(`e1`, `e2`, ...) with value, unit and provenance; every conclusion is a *claim* with an epistemic level:

    OBSERVED         directly measured (a value, an event, a command state)
    ASSOCIATED       differs (or does not differ) from the matched baseline
    CONSISTENT_WITH  a hypothesis is compatible with the evidence; alternatives are not excluded
    ATTRIBUTED       a counterfactual in the declared model shows a but-for cause (model-scoped)
    INSUFFICIENT     the data cannot support a conclusion

Changing field names or level semantics after P5 starts requires bumping RECORD_VERSION.
"""

from datetime import datetime

RECORD_VERSION = "1.0"
LEVELS = ["INSUFFICIENT", "OBSERVED", "ASSOCIATED", "CONSISTENT_WITH", "ATTRIBUTED"]

FEATURE_UNITS = {"load_i2": "A²", "rise_rate": "°C/min", "thermal_response": "°C/min per unit load",
                 "pump_speed_cmd": "rpm", "coolant_ratio": "", "max_minus_avg": "°C", "max_minus_avg_min": "°C",
                 "peak_avg": "°C", "peak_max": "°C", "ambient": "°C", "pump_duty": "",
                 "pump_zero_while_commanded_s": "s", "model_effectiveness": "", "max_below_avg_share": ""}
FEATURE_LABELS = {"load_i2": "battery load (mean current²)", "rise_rate": "battery temperature rise rate",
                  "thermal_response": "heating per unit load", "pump_speed_cmd": "coolant pump speed when commanded",
                  "coolant_ratio": "coolant heat pick-up ratio", "max_minus_avg": "maximum minus average temperature",
                  "max_minus_avg_min": "lowest maximum-minus-average temperature", "peak_avg": "peak average temperature",
                  "peak_max": "peak maximum temperature", "ambient": "outside temperature",
                  "pump_duty": "pump command duty", "pump_zero_while_commanded_s": "time at zero rpm while commanded",
                  "model_effectiveness": "cooling effectiveness fitted by the physical model, relative to design",
                  "max_below_avg_share": "share of samples with the maximum below the average temperature"}
NORMAL = {"none": "ASSOCIATED", "pump_not_commanded": "OBSERVED", "stale_or_missing_signal": "OBSERVED"}


def _iso(t) -> str:
    return t.isoformat() if isinstance(t, datetime) else str(t)


def build_record(key, anchor, event, target, w_start, tgt, base, sig, gate, hyps, cf, model_fit=None) -> dict:
    slots, n = {}, 0

    def slot(value, unit, label, provenance) -> str:
        nonlocal n
        n += 1
        sid = f"e{n}"
        slots[sid] = {"value": value, "unit": unit, "label": label, "provenance": provenance}
        return sid

    features = {}
    for name, e in sig.items():
        if e.get("obs") is None:
            continue
        ref = {"obs": slot(e["obs"], FEATURE_UNITS.get(name, ""), FEATURE_LABELS.get(name, name),
                           f"target window {_iso(w_start)}..{_iso(anchor)}")}
        if "base" in e:
            ref["base"] = slot(e["base"], FEATURE_UNITS.get(name, ""), f"baseline {FEATURE_LABELS.get(name, name)}",
                               f"median of {e['n_base']} baseline windows")
            if e.get("rel") is not None:
                ref["rel"] = slot(e["rel"] * 100, "%", f"change in {FEATURE_LABELS.get(name, name)}", "derived")
        features[name] = {**{k: v for k, v in e.items()}, "slots": ref}

    cf_by_h = {c["hypothesis"]: c for c in cf}
    claims = []
    for h in hyps:
        if not h["accepted"]:
            continue
        if h["id"] in NORMAL:
            level = NORMAL[h["id"]]
        elif cf_by_h.get(h["id"], {}).get("verdict") == "but_for_cause":
            level = "ATTRIBUTED"
        else:
            level = "CONSISTENT_WITH"
        claims.append({"claim_id": f"c{len(claims) + 1}", "hypothesis": h["id"], "label": h["label"], "level": level,
                       "score": h["score"], "supports": h["supports"], "contradictions": h["contradictions"],
                       "counterfactual": cf_by_h.get(h["id"])})
    if gate["status"] == "INSUFFICIENT" and not claims:
        conclusion = {"level": "INSUFFICIENT", "hypotheses": [], "summary": "insufficient"}
    elif not claims:
        conclusion = {"level": "INSUFFICIENT", "hypotheses": ["unknown"], "summary": "no hypothesis fits"}
    else:
        top = max(claims, key=lambda c: LEVELS.index(c["level"]))
        conclusion = {"level": top["level"], "hypotheses": sorted(c["hypothesis"] for c in claims),
                      "summary": "multiple" if len(claims) > 1 else claims[0]["hypothesis"]}

    return {
        "record_version": RECORD_VERSION,
        "source": key,
        "anchor": {"ts": _iso(anchor), "event": event,
                   "session": {"kind": target.kind, "ref": target.ref, "start": _iso(target.start),
                               "end": _iso(target.end), "attrs": target.attrs}},
        "target_window": {"start": _iso(w_start), "end": _iso(anchor)},
        "baseline": {k: v for k, v in base.items() if not k.startswith("_")},
        "signals": tgt["signals"],
        "features": features,
        "gate": gate,
        "hypotheses": hyps,
        "model_fit": model_fit,
        "counterfactual": cf,
        "claims": claims,
        "conclusion": conclusion,
        "slots": slots,
    }
