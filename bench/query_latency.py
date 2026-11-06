"""Experiment C - end-to-end query latency (solution_journey §9.3; NFR-01/02).

    python -m bench.query_latency

For each vehicle source, every question in tests/data/query_set.yaml goes through the full Tier 0 path
(route -> validate -> execute -> render). Classes are reported separately:
    common      list / count / max_drop / trend / compare / point lookups (the "< 2 s" claim, NFR-01)
    diagnostic  diagnose_event (NFR-02, reported separately)
    refusal     control / injection / off-topic requests
`cold` = the first question on a fresh engine (opens DuckDB, loads the vehicle context);
`warm` = every question repeated 5 times on a running engine. DuckDB is limited to 2 threads and
512 MB as in config/drivetrace.yaml. Python process memory is sampled after each run.
"""

import json
import platform
import statistics
import time

import psutil

from memory.store import ROOT, Store
from query.engine import QueryEngine
from tests.query_set import load

SOURCES = ["hist6w_s42", "ved_455", "zenodo_can_vehicle"]
REPEATS = 5
OUT = ROOT / "bench" / "results" / "query_latency.json"


def cls(intent: str) -> str:
    return {"diagnose_event": "diagnostic", "unsupported": "refusal"}.get(intent, "common")


def pctl(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def summarise(rows: list[dict]) -> dict:
    out = {}
    for c in ("common", "diagnostic", "refusal"):
        r = [x for x in rows if x["class"] == c]
        if not r:
            continue
        tot = [x["total_ms"] for x in r]
        stages = {}
        for st in ("route", "validate", "execute", "render"):
            v = [x["timings"][st] for x in r if st in x["timings"]]
            if v:
                stages[st] = {"p50": round(statistics.median(v), 2), "p95": round(pctl(v, 95), 2)}
        out[c] = {"n": len(r), "answered": sum(x["ok"] for x in r), "p50_ms": round(statistics.median(tot), 1),
                  "p95_ms": round(pctl(tot, 95), 1), "max_ms": round(max(tot), 1), "stages": stages}
    return out


def main():
    items = load()
    proc = psutil.Process()
    results = {"cpu": platform.processor(), "note": "x86 proxy, DuckDB 2 threads / 512 MB", "sources": {}}
    for key in SOURCES:
        t0 = time.perf_counter()
        engine = QueryEngine(Store())
        first = engine.ask(items[0]["q"], key)
        cold_ms = (time.perf_counter() - t0) * 1000
        rows = []
        for it in items:
            for _ in range(REPEATS):
                a = engine.ask(it["q"], key)
                rows.append({"q": it["q"], "class": cls(it["intent"]), "ok": a.ok, "tier": a.tier,
                             "total_ms": a.total_ms, "timings": a.timings_ms})
        rss = proc.memory_info().rss / 1e6
        s = summarise(rows)
        results["sources"][key] = {"cold_first_question_ms": round(cold_ms, 1), "cold_answer_ok": first.ok,
                                   "python_rss_mb": round(rss), "classes": s}
        print(f"\n{key}: cold first question {cold_ms:.0f} ms, process RSS {rss:.0f} MB")
        for c, v in s.items():
            print(f"  {c:<10} n={v['n']:<4} answered {v['answered']:<4} p50 {v['p50_ms']:>6.1f} ms  "
                  f"p95 {v['p95_ms']:>6.1f} ms  max {v['max_ms']:>6.1f} ms  execute p95 {v['stages'].get('execute', {}).get('p95', 0)} ms")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
