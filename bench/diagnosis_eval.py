"""Diagnostic evaluation on the labelled scenarios (P4 exit; Experiment B, first pass).

    python -m bench.diagnosis_eval

Cases:
  * every labelled episode (data/sim/labels/episodes) - anchor = first over-temperature warning in the
    trigger window, or the end of the trigger activity if no warning was raised (S0, S4, S5);
  * the six-week history - each warning, plus the S4 and S5 windows.
Labels are read here only to define anchors and score results; the diagnoser never sees them.
Metrics: exact match of the hypothesis set, top-1 in expected, expected within top-3 scores, abstention,
confidently wrong (a conclusion that shares nothing with the truth), and "no fault claimed" for S4/S5.
"""

import json
from collections import defaultdict
from datetime import datetime

from diagnosis.engine import Diagnoser
from memory.store import ROOT, Store

LABELS = ROOT / "data" / "sim" / "labels"
OUT = ROOT / "bench" / "results" / "diagnosis_eval.json"
FAULTS = {"elevated_electrical_load", "cooling_degradation", "temperature_sensor_fault"}


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def peak_time(store, key: str, start: datetime, end: datetime) -> datetime:
    """No warning: anchor at the battery's hottest moment in the window (what a user would ask about)."""
    return store.query(f"""SELECT arg_max(ts, value) FROM {store.signals(key)} WHERE signal_path =
        'Vehicle.Powertrain.TractionBattery.Temperature.Average' AND ts BETWEEN ? AND ?""", [start, end])[0][0]


def cases_from(labels: dict, key: str, split: str, store) -> list[dict]:
    out = []
    windows = {t: (parse(w["start_utc"]), parse(w["end_utc"])) for t, w in labels["scenario_windows"].items()
               if w["scenario"] not in (None, "none")}
    for tag, w in labels["scenario_windows"].items():
        if tag not in windows:
            continue
        start, end = windows[tag]
        # each warning belongs to the most specific (shortest) labelled window that contains it
        warns = [parse(x["ts_utc"]) for x in labels["warnings"] if start <= parse(x["ts_utc"]) <= end and
                 min((b - a, t) for t, (a, b) in windows.items() if a <= parse(x["ts_utc"]) <= b)[1] == tag]
        anchors = warns if (warns and key.startswith("hist")) else warns[:1] or [peak_time(store, key, start, end)]
        for a in anchors:
            out.append({"source": key, "split": split, "scenario": w["scenario"], "tag": tag, "anchor": a,
                        "expected": set(w["expected"]), "warning": bool(warns)})
    return out


def score(case: dict, rec: dict) -> dict:
    claims = rec["claims"]
    predicted = {c["hypothesis"] for c in claims}
    ranked = [h["id"] for h in rec["hypotheses"]]
    exp = case["expected"]
    if exp == {"none"} and predicted and predicted <= {"none", "pump_not_commanded"}:
        predicted = {"none"}  # both are no-fault, normal-operation conclusions
    abstained = not predicted or predicted <= {"stale_or_missing_signal"} and "stale_or_missing_signal" not in exp
    top1 = max(claims, key=lambda c: c["score"])["hypothesis"] if claims else None
    if predicted == {"none"} and exp == {"none"}:
        top1 = "none"
    return {
        "predicted": sorted(predicted), "level": rec["conclusion"]["level"], "gate": rec["gate"]["status"],
        "exact": predicted == exp,
        "top1": top1 in exp if top1 else False,
        "top3": exp <= set(ranked[:3]),
        "abstained": abstained,
        "confidently_wrong": bool(predicted) and not (predicted & exp) and rec["conclusion"]["level"] in ("CONSISTENT_WITH", "ATTRIBUTED"),
        "no_fault_claimed": not (predicted & FAULTS),
        "levels": {c["hypothesis"]: c["level"] for c in claims},
    }


def main():
    store = Store(include_episodes=True)
    d = Diagnoser(store)
    splits = json.loads((LABELS / "splits.json").read_text())
    split_of = {run: s for s, runs in splits.items() for run in runs}
    cases = []
    for f in sorted((LABELS / "episodes").glob("*.json")):
        lab = json.loads(f.read_text())
        cases += cases_from(lab, lab["run_id"], split_of[lab["run_id"]], store)
    hist = json.loads(next(LABELS.glob("history_*.json")).read_text())
    cases += cases_from(hist, hist["run_id"], "history", store)

    rows = []
    for c in cases:
        rec = d.diagnose(c["source"], c["anchor"])
        r = {**{k: (sorted(v) if isinstance(v, set) else v.isoformat() if isinstance(v, datetime) else v)
                for k, v in c.items()}, **score(c, rec)}
        rows.append(r)

    by = defaultdict(list)
    for r in rows:
        by[r["split"]].append(r)
    summary = {}
    print(f"{'split':<8} {'n':>3} {'exact':>6} {'top1':>5} {'top3':>5} {'abstain':>8} {'conf.wrong':>10}")
    for split in ("train", "dev", "test", "history", "blind"):
        rs = by.get(split, [])
        if not rs:
            continue
        s = {m: sum(r[m] for r in rs) for m in ("exact", "top1", "top3", "abstained", "confidently_wrong")}
        s["n"] = len(rs)
        s["S4_S5_no_fault"] = f"{sum(r['no_fault_claimed'] for r in rs if r['scenario'] in ('S4', 'S5'))}/" \
                              f"{sum(r['scenario'] in ('S4', 'S5') for r in rs)}"
        summary[split] = s
        print(f"{split:<8} {s['n']:>3} {s['exact']:>6} {s['top1']:>5} {s['top3']:>5} {s['abstained']:>8} "
              f"{s['confidently_wrong']:>10}   S4/S5 no fault claimed: {s['S4_S5_no_fault']}")
    print()
    for r in rows:
        if not r["exact"]:
            print(f"  MISS {r['split']:<7} {r['source']:<11} {r['scenario']} expected {r['expected']} got {r['predicted']} "
                  f"({r['level']}, gate {r['gate']})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary, "cases": rows}, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
