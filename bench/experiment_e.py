"""Experiment E - evidence quality and abstention (solution_journey §9.3).

    python -m bench.experiment_e

1. Corrupt the target window of each dev/test episode and history case in six ways and diagnose again.
   Desired behaviour: the evidence gate notices (flags or a non-SUFFICIENT status) and the system does not
   give a confidently wrong answer.
2. Risk-coverage: re-score every (clean + corrupted) case with acceptance thresholds from 1.5 to 7 using the
   stored Δ-Signature and gate flags. Coverage = cases with a conclusion; risk = wrong conclusions among them.
"""

import json
from collections import defaultdict

import numpy as np
import pandas as pd

from bench.diagnosis_eval import FAULTS, LABELS, cases_from
from bench.env import header
from diagnosis.engine import Diagnoser, load_rules
from diagnosis.hypotheses import evaluate
from diagnosis.window import CURRENT, PUMP_RPM, T_AVG, T_MAX
from memory.store import ROOT, Store

OUT = ROOT / "bench" / "results" / "experiment_e.json"
THRESHOLDS = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 7.0]


def _copy(w):
    return {k: v.copy() for k, v in w.items()}


def missing_pump(w):
    w = _copy(w); w.pop(PUMP_RPM, None); return w


def frozen_temp(w):
    w = _copy(w)
    if T_AVG in w and len(w[T_AVG]):
        w[T_AVG]["value"] = w[T_AVG]["value"].iloc[0]
    return w


def unsynced_current(w):
    w = _copy(w)
    if CURRENT in w:
        w[CURRENT]["ts"] = w[CURRENT]["ts"] + pd.Timedelta(minutes=10)
    return w


def max_bias(w):
    w = _copy(w)
    if T_MAX in w:
        w[T_MAX]["value"] = w[T_MAX]["value"] - 8.0
    return w


def noise(w):
    w = _copy(w)
    rng = np.random.default_rng(0)
    for p in (T_AVG, T_MAX):
        if p in w:
            w[p]["value"] = w[p]["value"] + rng.normal(0, 1.5, len(w[p]))
    return w


def missing_max(w):
    w = _copy(w); w.pop(T_MAX, None); return w


CORRUPTIONS = {"clean": None, "missing_pump_speed": missing_pump, "frozen_avg_temperature": frozen_temp,
               "current_shifted_10min": unsynced_current, "max_sensor_minus_8C": max_bias,
               "temperature_noise_1_5C": noise, "missing_max_sensor": missing_max}


def predicted(claims_or_hyps, from_hyps=False) -> list[str]:
    if from_hyps:
        return [h["id"] for h in sorted(claims_or_hyps, key=lambda h: -h["score"]) if h["accepted"]]
    return [c["hypothesis"] for c in sorted(claims_or_hyps, key=lambda c: -c["score"])]


# What a corruption makes true: a biased sensor IS a sensor fault; missing or frozen data IS a data problem.
INJECTED = {"max_sensor_minus_8C": {"temperature_sensor_fault"}, "missing_pump_speed": {"stale_or_missing_signal"},
            "frozen_avg_temperature": {"stale_or_missing_signal"}, "missing_max_sensor": {"stale_or_missing_signal"}}
DATA_PROBLEM = {"stale_or_missing_signal"}


def judge(expected: set, pred: list[str], corruption: str = "clean") -> dict:
    """Correct = the original causes, optionally plus what the corruption made true.
    Only reporting a data problem counts as abstaining (\"cannot judge\"), not as a wrong diagnosis."""
    p = set(pred)
    if expected == {"none"} and p and p <= {"none", "pump_not_commanded"}:
        p = {"none"}
    extra = INJECTED.get(corruption, set())
    if p and (p - extra) == expected:  # e.g. S5, where reporting the data problem IS the right answer
        return {"exact": True, "abstained": False, "wrong": False, "confidently_wrong": False,
                "fault_claimed": bool(p & FAULTS)}
    substantive = p - DATA_PROBLEM - extra
    abstained = not substantive and not (p & extra - DATA_PROBLEM)
    exact = (p - extra) == expected or (substantive == expected - DATA_PROBLEM and bool(substantive))
    return {"exact": exact and not abstained, "abstained": abstained, "wrong": not abstained and not exact,
            "confidently_wrong": bool(substantive) and not (substantive & expected), "fault_claimed": bool(p & FAULTS)}


def main():
    store = Store(include_episodes=True)
    d = Diagnoser(store)
    rules = load_rules()
    splits = json.loads((LABELS / "splits.json").read_text())
    cases = []
    for split in ("dev", "test", "blind"):
        for run in splits.get(split, []):
            cases += cases_from(json.loads((LABELS / "episodes" / f"{run}.json").read_text()), run, split, store)
    hist = json.loads(next(LABELS.glob("history_*.json")).read_text())
    cases += cases_from(hist, hist["run_id"], "history", store)

    rows, stored = [], []
    for c in cases:
        clean_flags = None
        for name, fn in CORRUPTIONS.items():
            rec = d.diagnose(c["source"], c["anchor"], transform=fn)
            flags = set(rec["gate"]["flags"])
            if name == "clean":
                clean_flags = flags
            pred = predicted(rec["claims"])
            j = judge(c["expected"], pred, name)
            detected = name != "clean" and (bool(flags - clean_flags) or rec["gate"]["status"] != "SUFFICIENT")
            rows.append({"corruption": name, "scenario": c["scenario"], "split": c["split"], "source": c["source"],
                         "expected": sorted(c["expected"]), "predicted": pred, "level": rec["conclusion"]["level"],
                         "gate": rec["gate"]["status"], "new_flags": sorted(flags - (clean_flags or set())),
                         "detected": detected, **j})
            stored.append((c["expected"], rec["features"], flags, rec["gate"]["status"], name))

    by = defaultdict(list)
    for r in rows:
        by[r["corruption"]].append(r)
    corr_summary = {k: {"n": len(v), "detected_by_gate": sum(r["detected"] for r in v),
                        **{m: sum(r[m] for r in v) for m in ("exact", "abstained", "wrong", "confidently_wrong")}}
                    for k, v in by.items()}
    blind = defaultdict(list)
    for r in rows:
        if r["split"] == "blind":
            blind[r["corruption"]].append(r)
    blind_summary = {k: {"n": len(v), "detected_by_gate": sum(r["detected"] for r in v),
                         **{m: sum(r[m] for r in v) for m in ("exact", "abstained", "wrong", "confidently_wrong")}}
                     for k, v in blind.items()}

    rc = []
    for tau in THRESHOLDS:
        r2 = {**rules, "accept": tau}
        js = []
        for exp, sig, flags, status, corruption in stored:
            hyps = evaluate(r2, sig, flags)
            if status == "INSUFFICIENT":
                for h in hyps:
                    h["accepted"] = h["accepted"] and h["id"] == "stale_or_missing_signal"
            js.append(judge(exp, predicted(hyps, from_hyps=True), corruption))
        covered = [j for j in js if not j["abstained"]]
        rc.append({"threshold": tau, "coverage": round(len(covered) / len(js), 3),
                   "risk_wrong": round(sum(j["wrong"] for j in covered) / max(1, len(covered)), 3),
                   "risk_confidently_wrong": round(sum(j["confidently_wrong"] for j in covered) / max(1, len(covered)), 3),
                   "n": len(js)})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"header": header(), "corruptions": corr_summary, "blind_only": blind_summary, "risk_coverage": rc, "rows": rows},
                              indent=2, default=str), encoding="utf-8")
    print(f"{'corruption':<26} {'n':>3} {'detected':>9} {'exact':>6} {'abstain':>8} {'wrong':>6} {'conf.wrong':>10}")
    for k, s in corr_summary.items():
        print(f"{k:<26} {s['n']:>3} {s['detected_by_gate']:>9} {s['exact']:>6} {s['abstained']:>8} {s['wrong']:>6} {s['confidently_wrong']:>10}")
    print("\nblind split only (never used for tuning):")
    for k, s2 in blind_summary.items():
        print(f"{k:<26} {s2['n']:>3} {s2['detected_by_gate']:>9} {s2['exact']:>6} {s2['abstained']:>8} "
              f"{s2['wrong']:>6} {s2['confidently_wrong']:>10}")
    print("\nthreshold  coverage  risk(wrong)  risk(confidently wrong)")
    for r in rc:
        print(f"{r['threshold']:>9}  {r['coverage']:>8}  {r['risk_wrong']:>11}  {r['risk_confidently_wrong']:>23}")


if __name__ == "__main__":
    main()
