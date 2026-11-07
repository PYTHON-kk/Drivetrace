"""Routing accuracy on tests/data/query_set.yaml (P2): Tier 0 rules, and the Tier 1 SLM on its own.

    python -m bench.routing_eval            # Tier 0 only
    python -m bench.routing_eval --slm      # also SmolLM2 and Qwen2.5 planners (starts llama-server)

The SLM is scored on every question, not just the ones Tier 0 cannot route, so its accuracy as a
fallback is measured on the full spread of phrasings.
"""

import argparse
import json
import statistics
import time
from pathlib import Path

from query.plan import PlanError
from query.planner import ROOT, LocalServer, SlmPlanner
from query.router import route
from tests.query_set import load, load_independent, score

OUT = ROOT / "bench" / "results" / "routing_eval.json"
SLM_RUNS = [("SmolLM2-360M-Instruct-Q4_K_M.gguf", "json"), ("SmolLM2-360M-Instruct-Q4_K_M.gguf", "compact"),
            ("qwen2.5-0.5b-instruct-q4_k_m.gguf", "json")]


def _evaluate_items(name: str, split: str, items: list[dict], plan_fn) -> dict:
    rows, times = [], []
    for it in items:
        t0 = time.perf_counter()
        try:
            plan = plan_fn(it["q"])
        except PlanError:
            plan = None
        times.append((time.perf_counter() - t0) * 1000)
        bad = score(it, plan)
        rows.append({"q": it["q"], "expected": {k: it[k] for k in it if k in ("intent", "signal", "period", "group_by", "event_type", "aggregate")},
                     "got": plan, "wrong": bad})
    correct = sum(not r["wrong"] for r in rows)
    intent_ok = sum("intent" not in r["wrong"] and "no_plan" not in r["wrong"] for r in rows)
    out = {"n": len(rows), "correct": correct, "intent_correct": intent_ok,
           "no_plan": sum("no_plan" in r["wrong"] for r in rows),
           "p50_ms": round(statistics.median(times), 2), "max_ms": round(max(times), 2),
           "errors": [r for r in rows if r["wrong"]]}
    print(f"  {name:<34} {split:<11} fully correct {correct:>3}/{len(rows)}  intent {intent_ok:>3}/{len(rows)}  "
          f"no plan {out['no_plan']:>2}  p50 {out['p50_ms']:.1f} ms")
    return out


def evaluate(name: str, plan_fn) -> dict:
    out = {}
    for split in ("dev", "test"):
        out[split] = _evaluate_items(name, split, load(split), plan_fn)
    out["independent"] = _evaluate_items(name, "independent", load_independent(), plan_fn)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slm", action="store_true")
    args = ap.parse_args()
    results = {"tier0": evaluate("tier0 rules", lambda q: (r.plan.as_dict() if (r := route(q)) else None))}
    if args.slm:
        for model, fmt in SLM_RUNS:
            with LocalServer(ROOT / "models" / model):
                planner = SlmPlanner(fmt=fmt)
                results[f"{model}:{fmt}"] = evaluate(f"tier1 {model[:12]} {fmt}", lambda q: planner.plan(q).as_dict())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
