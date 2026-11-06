# DriveTrace

Offline, evidence-grounded vehicle assistant for Tata Elxsi Teliport Case Study 5 (VehicleGPT).
Design: `../solution_journey (1).md`. Build plan: `../build_plan.md`.

## Status

Phases P0-P7 built and measured on an x86 laptop used as a proxy; P8 (submission) in progress. Results: `docs/p2_results.md` ... `docs/p7_results.md`. Reproduce everything: `docs/REPRODUCE.md`. Demo: `docs/demo_script.md`.

Open: re-measure on the 2-core ARM target; real (non-synthetic) fault data; an independently written question set.

## Quick start (Windows, Python 3.14)

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

```bash
.venv/Scripts/python -m sim.generate all
```

```bash
.venv/Scripts/python -m scripts.fetch_public --yes vedzenodo
```

```bash
.venv/Scripts/python -m scripts.fetch_public --extract-ved
```

```bash
.venv/Scripts/python -m adapters.ved
```

```bash
.venv/Scripts/python -m adapters.zenodo_can
```

```bash
.venv/Scripts/python -m tools.validate_dataset
```

```bash
.venv/Scripts/python -m pytest -q
```

## Layout

| Path | Contents |
|---|---|
| `schema/` | Canonical record schemas (`canonical.py`) and signal catalogue / VSS mapping (`vss_mapping.yaml`) |
| `sim/vehicle.py` | Lumped electro-thermal EV model, BMS logic (pump, derate, over-temperature warning), fault injection |
| `sim/drive_cycles.py` | City and highway speed profiles |
| `sim/engine.py` | Timeline execution, ADAS state machine, Parquet recorder, ground-truth labels |
| `sim/scenarios.yaml` | Six-week history plan and episode templates S0-S7 |
| `sim/generate.py` | CLI: `history`, `episodes`, `all` |
| `adapters/common.py` | Shared writer: catalogue/unit check, date-partitioned canonical Parquet |
| `adapters/ved.py`, `ved_mapping.yaml` | VED weekly CSVs to one source per vehicle; trip boundaries to ignition events |
| `adapters/zenodo_can.py`, `zenodo_mapping.yaml` | Zenodo HDF5 CAN logs to signals (unit-checked, decimated), lamp/cruise/logger events |
| `tools/validate_dataset.py` | Integrity and answerability checks for simulated and recorded data, writes `docs/dataset_report.md` |
| `scripts/fetch_public.py` | Downloads VED and Zenodo CAN (asks for `--yes`) |
| `memory/` | DuckDB store with limits, ring buffer, aggregates, trips, event index, warning capsules |
| `ingest/` | Replay at 1x/Nx; personal-data deletion (ingest process only) |
| `query/` | Plan vocabulary and grammars, Tier 0 router, Tier 1 SLM planner, allowlisted executor, answer templates |
| `diagnosis/` | Baselines, Δ-Signature, evidence gate, hypothesis rules (`rules.yaml`, v1.1), physical-model counterfactual, evidence record v1.0 |
| `ecd/` | Evidence-Compiled Decoding: lexicon, grammar compiler, renderer and contract checks |
| `security/` | Egress guard, privacy policy, gateway forwarder |
| `ui/` | Local web UI (FastAPI + vanilla JS) |
| `deploy/` | Dockerfile, compose (ingest / assistant / gateway / optional SLM), isolation checks |
| `bench/` | Experiments and the report generator |
| `data/MANIFEST.md` | What data exists, where, licences |

## Scenarios

| ID | Injected condition | What the data shows |
|---|---|---|
| S0 | Healthy | No warnings; pump runs during DC charging |
| S1 | Heatwave + towing + 150 kW charge | Warning with healthy cooling: high current, pump at full speed |
| S2 | Cooling degradation | Warning at normal current: pump below nominal speed, coolant outlet stays cool |
| S3 | Average-temperature sensor bias | False warning: Average reads above Max (physically impossible) |
| S4 | Pump not commanded | 0 rpm with command off: normal, not a fault |
| S5 | Pump-speed signal frozen, then missing | Constant value while command toggles; ~23 h gap |
| S6 | Towing + 150 kW + degraded cooling | Compound cause |
| S7 | Degraded cooling + Max sensor under-reads (held out, test only) | Real warning with contradictory sensors |
