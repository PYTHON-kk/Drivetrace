# P4 results - diagnosis core

**Date:** 2026-10-29
**Data:** synthetic only (45 labelled episodes S0-S7 + the six-week history). No real vehicle has a labelled thermal fault, so none of these numbers say anything about real-world diagnostic accuracy.
**Raw results:** `bench/results/diagnosis_eval.json`. Re-run: `python -m bench.diagnosis_eval`.

## What was built (`diagnosis/`)

| Step | File | Requirement |
|---|---|---|
| Sessions (trips, DC/AC charges, parked gaps), signal windows, derived thermal features | `window.py` | - |
| Matched baseline: same session kind, declared tolerances (power, start SoC, ambient, start temperature), same offset into the session; sessions with warnings/derates excluded; `matched` → `relaxed` → `unmatched` fallback | `baseline.py` | FR-ANA-02 |
| Δ-Signature: observed vs baseline median, absolute/relative change, robust z with declared noise floors | `signature.py` | FR-ANA-03 |
| Evidence gate: coverage, gaps, physical range, frozen values, Max < Avg contradiction, pump command state, baseline comparability → `SUFFICIENT` / `AMBIGUOUS` / `INSUFFICIENT` | `signature.py` | FR-DX-01 |
| Hypothesis library and compatibility score S(h\|E) - all rules declared in YAML | `rules.yaml`, `hypotheses.py` | FR-DX-02 |
| Physical-model fit (baseline-free cooling effectiveness) and counterfactual check (remove one fault, keep observed inputs) | `counterfactual.py` | FR-DX-03 |
| Evidence record **v1.0, frozen for P5**: slots (`e1`...), claims with epistemic levels, provenance | `record.py` | P4 → P5 |
| Wired into answers: "What happened before the battery overheating warning?" now includes the diagnosis | `query/executor.py`, `query/answers.py` | - |

## Results

Hypothesis sets are scored exactly: S6 and S7 need both causes. For S0, "pump not commanded" counts as the expected no-fault answer.

| Split | Cases | Exact | Top-1 in truth | Truth within top 3 | Abstained | Confidently wrong | S4/S5 claimed no fault |
|---|---|---|---|---|---|---|---|
| train | 21 | 21 | 21 | 21 | 0 | 0 | 6/6 |
| dev | 7 | 7 | 7 | 7 | 0 | 0 | 2/2 |
| test (incl. held-out S7) | 17 | 17 | 17 | 17 | 0 | 0 | 4/4 |
| six-week history | 8 | 8 | 8 | 8 | 0 | 0 | 2/2 |

Claim levels (episodes):

| Scenario | Conclusion | Level |
|---|---|---|
| S0 healthy | pump not commanded / no anomaly | OBSERVED |
| S1 heat + towing + 150 kW | elevated electrical load | ATTRIBUTED (6/6) |
| S2 cooling degradation | reduced cooling | ATTRIBUTED (6/6) |
| S3 average-sensor bias | temperature sensor fault | CONSISTENT_WITH (no model check for sensors) |
| S4 pump not commanded | normal operation | OBSERVED |
| S5 frozen / missing pump signal | stale or missing data, no fault claimed | OBSERVED |
| S6 load + degraded cooling | both | ATTRIBUTED (each is a but-for cause in the model) |
| S7 degraded cooling + Max sensor under-reading (held out) | both | cooling ATTRIBUTED, sensor CONSISTENT_WITH |

**Latency (x86 proxy):** diagnosis p50 118 ms, p95 261 ms. The first diagnosis per vehicle takes about 1-1.6 s while session start conditions are cached. The common-query path is unchanged (p95 17 ms).

## Why these numbers are optimistic - read before quoting them

1. **The counterfactual model is the simulator.** The physical model used to fit and to test "what if this fault were absent" is the same model that generated the data. ATTRIBUTED results therefore show the method works mechanically, not that it would be right on a real battery pack. On a real vehicle the model has to be calibrated first, and the code refuses attribution when the fit is poor (RMSE > 1 °C) or the parameter cannot be identified.
2. **The scenarios are clean and separable by design.** One fault family per scenario, fixed magnitude ranges, a known BMS warning rule. Real faults overlap, drift and interact with sensor noise far more.
3. **Indirect tuning risk.** Rules were tuned on the train episodes and the history, but the dev and test totals were printed during development. Every change was a declared, physically motivated rule, not a per-case patch, but the test result is not fully blind.
4. **Fixes made during development** (all recorded in `rules.yaml` / code comments):
   - noise floors are now required for every z-scored feature; without them, z exploded to ~10¹⁰;
   - stale detection was fixed: DuckDB microsecond timestamps were being read as nanoseconds;
   - windows no longer cross a session boundary, and expected signals depend on the session kind (a parked car logs little);
   - sessions that raised warnings are excluded as baselines;
   - a well-fitting physical model outweighs comparisons with possibly degraded baselines;
   - high load alone is not a fault when temperatures are normal;
   - "0 rpm while commanded, yet cooling works normally" is treated as a wrong reading, not a dead pump.

## Known limits and next steps

- **Baselines are only presumed healthy.** In the history, all DC-charge baselines after the slow cooling degradation began were already degraded; the physical-model reference is what recovers the compound case. A fixed reference period (e.g. the first weeks after delivery) is a candidate improvement.
- **Sensor faults never reach ATTRIBUTED,** because no model of sensor behaviour is declared.
- **Diagnosis covers battery over-temperature only.** ADAS and warning-lamp events still get the factual timeline only.
- **Real data:** the VED and Zenodo vehicles have no thermal faults, so only the "no deviation" and "insufficient data" paths can be exercised there.
- **ECD (P5)** starts from the frozen record: `slots`, `claims[].level`, `claims[].supports`, `baseline`, `gate`.
