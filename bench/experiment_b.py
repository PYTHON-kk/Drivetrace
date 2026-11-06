"""Experiment B - diagnostic ablation (solution_journey §9.3).

    python -m bench.experiment_b            # deterministic variants + SLM variants (starts llama-server)
    python -m bench.experiment_b --no-slm

Variants on every labelled case (45 episodes + 8 history cases, reported per split):
  full          baselines + evidence gate + hypothesis rules + physical model / counterfactual
  no_model      without the physical-model feature and counterfactual
  no_gate       without data-quality checks (no stale/missing/contradiction flags)
  no_baseline   without matched baselines (only absolute features, flags and the model)
  slm_raw       the SLM picks the explanation from raw window summaries ("retrieval + SLM")
  slm_delta     the SLM picks the explanation from the Δ-Signature and gate flags
The SLM output is grammar-constrained to explanation names, so it is always a valid set.
"""

import argparse
import json
import urllib.request
from collections import defaultdict

from bench.diagnosis_eval import FAULTS, LABELS, cases_from
from bench.env import header
from diagnosis.engine import Diagnoser
from diagnosis.record import FEATURE_LABELS
from diagnosis.window import CORE
from memory.store import ROOT, Store
from query.planner import LocalServer

OUT = ROOT / "bench" / "results" / "experiment_b.json"
PORT = 8089
MODELS = ["qwen2.5-0.5b-instruct-q4_k_m.gguf", "SmolLM2-360M-Instruct-Q4_K_M.gguf"]
HYPS = ["none", "pump_not_commanded", "cooling_degradation", "elevated_electrical_load",
        "temperature_sensor_fault", "stale_or_missing_signal"]
GRAMMAR = 'root ::= h (" " h)?\nh ::= ' + " | ".join(f'"{h}"' for h in HYPS) + "\n"
PROMPT = ("You diagnose a vehicle battery temperature situation. Choose one or two explanations that fit the data, "
          "most likely first. Explanations: none (no anomaly), pump_not_commanded (pump off by design), "
          "cooling_degradation, elevated_electrical_load, temperature_sensor_fault, stale_or_missing_signal "
          "(a reading is frozen or missing).\n\n")
DETERMINISTIC = {"full": frozenset(), "no_model": frozenset({"no_model"}), "no_gate": frozenset({"no_gate"}),
                 "no_baseline": frozenset({"no_baseline"})}


def raw_summary(rec: dict) -> str:
    lines = [f"Warning raised: {'yes' if 'warning_present' in rec['gate']['flags'] or rec['anchor']['event'] else 'no'}."]
    for p in CORE:
        s = rec["signals"].get(p, {})
        name = p.rsplit(".", 2)[-2] + "." + p.rsplit(".", 1)[-1]
        if s.get("n"):
            lines.append(f"{name}: {s['first']:.1f} -> {s['last']:.1f} (min {s['min']:.1f}, max {s['max']:.1f}, {s['n']} samples)")
        else:
            lines.append(f"{name}: no samples")
    return "\n".join(lines)


def delta_summary(rec: dict) -> str:
    lines = [f"Data checks: {', '.join(rec['gate']['flags']) or 'none flagged'}."]
    for name, f in rec["features"].items():
        if f.get("obs") is None:
            continue
        label = FEATURE_LABELS.get(name, name)
        if f.get("base") is not None and f.get("rel") is not None:
            lines.append(f"{label}: {f['obs']:.3g} vs baseline {f['base']:.3g} ({f['rel'] * 100:+.0f}%)")
        else:
            lines.append(f"{label}: {f['obs']:.3g}")
    return "\n".join(lines)


def slm_choose(text: str) -> list[str]:
    body = {"prompt": PROMPT + f"Data:\n{text}\nExplanations: ", "grammar": GRAMMAR, "n_predict": 24,
            "temperature": 0, "cache_prompt": True}
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/completion", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["content"].split()


def score(expected: set, predicted: list[str]) -> dict:
    pred = set(predicted)
    if expected == {"none"} and pred and pred <= {"none", "pump_not_commanded"}:
        pred = {"none"}
    exact = pred == expected
    return {"exact": exact, "top1": bool(predicted) and (predicted[0] in expected or (expected == {"none"} and exact)),
            "abstained": not pred, "confidently_wrong": bool(pred) and not (pred & expected),
            "fault_claimed": bool(pred & FAULTS)}


def summarise(rows: list[dict]) -> dict:
    out = defaultdict(dict)
    for variant in sorted({r["variant"] for r in rows}):
        for split in ("train", "dev", "test", "history"):
            rs = [r for r in rows if r["variant"] == variant and r["split"] == split]
            if rs:
                normal = [r for r in rs if r["scenario"] in ("S0", "S4", "S5")]
                out[variant][split] = {"n": len(rs), **{m: sum(r[m] for r in rs) for m in ("exact", "top1", "abstained", "confidently_wrong")},
                                       "false_fault_on_normal_or_bad_data": sum(r["fault_claimed"] for r in normal)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-slm", action="store_true")
    args = ap.parse_args()
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

    rows, full_records = [], []
    for c in cases:
        for variant, abl in DETERMINISTIC.items():
            rec = d.diagnose(c["source"], c["anchor"], ablate=abl)
            ranked = sorted(rec["claims"], key=lambda x: -x["score"])
            pred = [x["hypothesis"] for x in ranked]
            rows.append({"variant": variant, "split": c["split"], "scenario": c["scenario"], "source": c["source"],
                         "expected": sorted(c["expected"]), "predicted": pred, **score(c["expected"], pred)})
            if variant == "full":
                full_records.append((c, rec))
    if not args.no_slm:
        for model in MODELS:
            short = model.split("-")[0].split(".")[0].lower()
            with LocalServer(ROOT / "models" / model, port=PORT):
                for name, fn in (("slm_raw", raw_summary), ("slm_delta", delta_summary)):
                    for c, rec in full_records:
                        pred = slm_choose(fn(rec))
                        rows.append({"variant": f"{name}_{short}", "split": c["split"], "scenario": c["scenario"],
                                     "source": c["source"], "expected": sorted(c["expected"]), "predicted": pred,
                                     **score(c["expected"], pred)})
    summary = summarise(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"header": header(), "summary": summary, "rows": rows}, indent=2, default=str), encoding="utf-8")
    print(f"{'variant':<16} {'split':<8} {'n':>3} {'exact':>6} {'top1':>5} {'abstain':>8} {'conf.wrong':>10} {'false fault (S0/S4/S5)':>22}")
    for v, by in summary.items():
        for split, s in by.items():
            print(f"{v:<16} {split:<8} {s['n']:>3} {s['exact']:>6} {s['top1']:>5} {s['abstained']:>8} {s['confidently_wrong']:>10} "
                  f"{s['false_fault_on_normal_or_bad_data']:>22}")


if __name__ == "__main__":
    main()
