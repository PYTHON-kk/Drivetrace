# Decision gate G0 - latency budget for the SLM query planner

**Date:** 2026-10-29
**Status:** Decided on an x86 proxy; must be re-measured on the 2-core ARM target before results are reported.

## Setup

| Item | Value |
|---|---|
| Host | AMD Ryzen 7 5800HS (x86-64), Windows 11, 15.4 GB RAM. **Proxy only**: faster per core than the brief's Cortex-A5x class. |
| Threads | 2 (`-t 2 -tb 2`); the OS was not pinned to 2 cores |
| Runtime | llama.cpp b11606, prebuilt `win-cpu-x64` (Clang 20.1.8), sha256 `8c0f4f2a...f1b4` |
| Models | Qwen2.5-0.5B-Instruct Q4_K_M (485 MB), SmolLM2-360M-Instruct Q4_K_M (269 MB); hashes in `models/MODELS.md` |
| Scripts | `bench/plan_latency.py`; raw results `bench/results/plan_latency.json`, `bench/results/llama_bench_*.json` |

## Measurements

Raw model speed (`llama-bench`, 3 repetitions):

| Model | Prompt processing | Token generation |
|---|---|---|
| Qwen2.5-0.5B Q4_K_M | 109-115 tok/s | 27.2 tok/s |
| SmolLM2-360M Q4_K_M | 120-122 tok/s | 35.1-35.6 tok/s |

One grammar-constrained query plan through a resident `llama-server` (10 questions, temperature 0, end-to-end HTTP time):

| Model | Plan format | Prompt cache | Prompt tokens | Generated tokens | p50 | max | Valid plans | Intent correct |
|---|---|---|---|---|---|---|---|---|
| Qwen2.5-0.5B | JSON | off | 499 | 36 | 5.85 s | 6.26 s | 10/10 | 7/10 |
| Qwen2.5-0.5B | JSON | on | 14 | 36 | 1.71 s | 1.76 s | 10/10 | 7/10 |
| Qwen2.5-0.5B | compact | on | 14 | 7 | 0.55 s | 0.66 s | 10/10 | 2/10 |
| SmolLM2-360M | JSON | off | 566 | 39 | 6.76 s | 7.30 s | 10/10 | 7/10 |
| SmolLM2-360M | JSON | on | 14 | 39 | 1.63 s | 1.77 s | 10/10 | 7/10 |
| SmolLM2-360M | compact | on | 14 | 9 | 0.44 s | 0.64 s | 10/10 | 6/10 |

Peak working set of `llama-server`: Qwen 597 MB, SmolLM2 478 MB. Model load: 1.1 s and 0.5 s.

"JSON" emits `{"intent": ..., "event_type": ...}`; "compact" emits only the five enum values and code adds the keys. Both use the same closed vocabulary (`query/plan_grammar*.gbnf`). Ten questions is a sanity check, not an accuracy result: one question is 10 percentage points.

## Findings

1. **The grammar works:** 40/40 plans per model were valid. The actuation request ("Brake hard now") and the prompt-injection attempt mapped to `unsupported` in all 8 runs. Whatever the model chooses, the grammar cannot express SQL or a vehicle command.
2. **Prompt caching is mandatory.** Re-reading the ~500-token few-shot prefix costs 4-5 s; with a warm cache only the ~14 question tokens are processed.
3. **Tokens forced by the grammar are not free.** In the JSON format most of the 36-39 generated tokens are fixed keys and punctuation, yet each still costs a full decode step (about 1.4 s in total). Removing them (compact format) cuts decode from ~1.4 s to ~0.3 s.
4. **Fewer tokens can cost accuracy.** Without keys as context, Qwen collapsed to `unsupported` (2/10); SmolLM2 held at 6/10.
5. **Even the best case is too slow for the 2 s budget on the target.** 0.44-0.55 s on this x86 proxy, plus SQL, analytics and rendering, will likely exceed 2 s on Cortex-A5x-class cores, which are several times slower per core. This is an expectation, not a measurement.

## Decision

Following the G0 rule in the build plan (Part 3, P0):

1. **Tier 0 deterministic rules must answer the whole declared common-query set (Q1-Q6)** without calling the SLM. Their latency is SQL plus template rendering only.
2. **The Tier 1 SLM planner is a fallback** for paraphrases Tier 0 cannot route. It runs in a resident `llama-server` with a warm prompt cache, and its answers are validated like any other plan. It is reported as its own latency class; it is not part of the "under 2 s for common queries" claim until measured on ARM.
3. **Plan format is open until the P2 test set exists.** SmolLM2 with the compact format is the current leading candidate (fastest and least memory). A middle format (short key tags) is to be tested on the ~150-question P2 set.
4. **Consequence for ECD (solution doc §8.11.6):** the "forced tokens are cheap" hypothesis does not hold for this llama.cpp server path. ECD should let the model emit only *choices* (slot IDs, licensed phrase IDs), with fixed text added by the deterministic renderer, rather than having the model decode fixed phrases. Contracts C1-C3 are unaffected.

## Still to do

- Re-run `bench/plan_latency.py` and `llama-bench` on the ARM target (AWS `t4g.small` or a Pi pinned to 2 cores).
- Measure whole-stack memory (llama-server + DuckDB + app) under one 2 GB limit (Experiment D).
