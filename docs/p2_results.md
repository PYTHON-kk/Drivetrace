# P2 results - common-query path

**Date:** 2026-10-29
**Host:** AMD Ryzen 7 5800HS (x86-64) used as a **proxy**; DuckDB limited to 2 threads / 512 MB; SLM runs at 2 threads. Not yet measured on the 2-core ARM target.
**Raw results:** `bench/results/routing_eval.json`, `bench/results/query_latency.json`, `bench/results/plan_latency.json`.

## What was built

| Component | File | Requirement |
|---|---|---|
| Single plan vocabulary; grammars generated from it | `query/vocabulary.py`, `query/grammar.py` | FR-QRY-02 |
| Plan parsing, combination rules, documented defaults | `query/plan.py` | FR-QRY-03 |
| Relative time on the vehicle clock and local time zone (DST-safe) | `query/timeparse.py` | FR-QRY-04 |
| Allowlisted read-only executor: 8 intents, fixed parameterised SQL | `query/executor.py` | FR-QRY-03, FR-ANA-01 |
| Tier 0 deterministic router | `query/router.py` | FR-QRY-01 |
| Deterministic answer templates (no causal claims) | `query/answers.py` | P2 task 4 |
| Tier 1 SLM fallback planner (local llama-server, 127.0.0.1) | `query/planner.py` | FR-QRY-02 |
| End-to-end engine with per-stage timing; CLI | `query/engine.py`, `query/ask.py` | FR-UI-03 |
| 142-question test set (written before the rules; dev/test split) | `tests/data/query_set.yaml` | P2 task 6 |

## Routing accuracy

"Fully correct" means every field the test item specifies (intent, signal, period, group, event type) is right.

| Planner | Split | Fully correct | Intent correct | p50 time |
|---|---|---|---|---|
| Tier 0 rules | dev | 71/71 | 71/71 | 0.1 ms |
| Tier 0 rules | test | 71/71 | 71/71 | 0.1 ms |
| Qwen2.5-0.5B, JSON plan | test | 30/71 (42%) | 51/71 (72%) | 1.71 s |
| SmolLM2-360M, compact plan | test | 25/71 (35%) | 38/71 (54%) | 0.48 s |
| SmolLM2-360M, JSON plan | test | 12/71 (17%) | 43/71 (61%) | 1.69 s |

**Read with care:**
- **Tier 0's 100% is optimistic.** The questions were written before the rules and rules were tuned on `dev` only, but the same person wrote both, so the test split shares the author's phrasing. An independently written question set (other people, or the case-study evaluators' style) is needed before claiming FR-QRY-01 (≥ 95%).
- **The SLM planners are not reliable enough to act alone.** The best one gets 42% of plans fully right. Typical errors: "Show when I charged the car" → `point_lookup`; "how often did the lane assist disengage" → `list_events`; compact SmolLM2 often falls back to `unsupported`.
- **Safety held in every run:** all 426 SLM plans were grammar-valid, and all 24 control / injection / off-topic questions were refused by Tier 0 before any model was involved.

## Latency (Experiment C, first pass)

Tier 0 path, 142 questions × 5 repeats per vehicle; "answered" excludes questions the vehicle has no data for.

| Vehicle | Class | Runs | Answered | p50 | p95 | max |
|---|---|---|---|---|---|---|
| `hist6w_s42` (synthetic EV, 6 weeks) | common | 530 | 530 | 4.2 ms | 13.4 ms | 21.2 ms |
| | diagnostic (facts only) | 60 | 60 | 16.4 ms | 17.7 ms | 18.9 ms |
| `ved_455` (real Nissan Leaf, 1 year) | common | 530 | 500 | 3.4 ms | 14.5 ms | 68.7 ms |
| | diagnostic | 60 | 60 | 2.2 ms | 2.5 ms | 2.8 ms |
| `zenodo_can_vehicle` (real petrol car, 5 trips) | common | 530 | 245 | 0.1 ms | 4.0 ms | 43.0 ms |
| | diagnostic | 60 | 60 | 3.0 ms | 44.2 ms | 53.4 ms |

- **Cold start:** the first question on a fresh engine (opening DuckDB, loading the vehicle context) took 51-106 ms. Python start-up and imports are not included.
- **Memory:** the Python process (DuckDB included) used 83-92 MB RSS. The SLM server adds 478 MB (SmolLM2) or 597 MB (Qwen) when the fallback is enabled.
- **Unanswered questions are data gaps, not failures:** the Leaf has no battery-temperature signal (5 questions) and no displayed range (1); the Zenodo car has no battery energy or fuel-flow signal (47), battery temperature (5), state of charge (4) or range (1). Each gets an explicit "this vehicle does not record ..." answer.
- **Margin against NFR-01:** common-query p95 is 13-15 ms on the proxy, over 100 times below the 2 s budget. Even a 10× slower ARM core would leave a large margin; this is an expectation until it is measured on the target.

## Decisions taken from these results

1. **Tier 0 stays the primary path** for every common query (confirms G0).
2. **Tier 1 answers must be confirmed before they run.** Because a grammar-valid plan is often the wrong plan, the UI (P6) should echo the interpreted plan ("I understood: count ADAS disengagements this month - correct?") instead of answering directly. Qwen2.5-0.5B in JSON format is the more accurate fallback; SmolLM2 compact is the faster one. To be re-decided after the ARM measurement.
3. **`diagnose_event` currently returns the factual pre-event timeline only.** Cause ranking is P4.

## Open items

- Independent question set for FR-QRY-01.
- ARM-target run of `bench/query_latency.py`, `bench/routing_eval.py --slm` and `bench/plan_latency.py`.
- Whole-stack memory under one 2 GB limit (Experiment D).
