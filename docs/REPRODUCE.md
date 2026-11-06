# Reproducing DriveTrace results

Every number in `docs/p2_results.md` … `docs/p7_results.md` comes from a script in `bench/` writing to `bench/results/`. This guide goes from a clean checkout to those files. Commands run from the `drivetrace/` directory. The examples use the Windows venv path `.venv/Scripts/python`; on Linux or macOS use `.venv/bin/python`.

## 1. Environment

| Item | Version used |
|---|---|
| Python | 3.14.6 (container image: 3.13-slim) |
| Packages | pinned in `requirements.txt` (runtime subset: `deploy/requirements-runtime.txt`) |
| llama.cpp | release `b11606` (13 Oct 2026), CPU build, commit `1b70350c9` |
| Models | `models/MODELS.md` (sha256 and pinned Hugging Face revisions) |
| Docker | 29.x with Compose v2 (only for the container run) |

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

llama.cpp and the two models are downloaded once (about 780 MB). Hashes to verify are in `models/MODELS.md`. Put the extracted llama.cpp build in `runtime/llama-b11606/` and the `.gguf` files in `models/`.

## 2. Data

```bash
.venv/Scripts/python -m sim.generate all
.venv/Scripts/python -m sim.generate blind --seeds 6,7
.venv/Scripts/python -m scripts.fetch_public --yes vedzenodo
.venv/Scripts/python -m scripts.fetch_public --extract-ved
.venv/Scripts/python -m adapters.ved
.venv/Scripts/python -m adapters.zenodo_can
.venv/Scripts/python -m memory.build
.venv/Scripts/python -m memory.build --episodes
.venv/Scripts/python -m tools.validate_dataset
```

- **Synthetic data:** generation is deterministic. Seeds are in `sim/scenarios.yaml` (history 42) and `sim/generate.py` (episodes `10000 + 97*seed + index`). Labels go to `data/sim/labels/`, which no assistant code reads (enforced by a test).
- **Public data:** VED (Apache 2.0) and Zenodo record 2661316 (CC BY 4.0). Checksums are in `data/MANIFEST.md`. One Zenodo file is truncated at the source and is skipped.
- **Validation:** `tools.validate_dataset` writes `docs/dataset_report.md`.

## 3. Tests

```bash
.venv/Scripts/python -m pytest -q
```

Expected: 81 passed, 1 skipped (the skipped test needs `DRIVETRACE_SLM=1` and a running llama-server).

## 4. Experiments

| Result | Command | Output | Needs the SLM |
|---|---|---|---|
| G0 plan latency | `python -m bench.plan_latency` | `bench/results/plan_latency.json` | yes |
| Routing accuracy | `python -m bench.routing_eval --slm` | `routing_eval.json` | yes |
| Diagnosis accuracy (all splits) | `python -m bench.diagnosis_eval` | `diagnosis_eval.json` | no |
| B - ablation | `python -m bench.experiment_b` | `experiment_b.json` | yes |
| C - latency | `python -m bench.query_latency` | `query_latency.json` | no |
| D - resources | `python -m bench.experiment_d` | `experiment_d.json` | yes |
| E - data quality | `python -m bench.experiment_e` | `experiment_e.json` | no |
| F - ECD | `python -m bench.experiment_f --split test` | `experiment_f_test.json` | yes |
| Report | `python -m bench.report` | `docs/p7_results.md` | no |

The scripts that need the SLM start `llama-server` themselves on 127.0.0.1:8089 at 2 threads. Run the latency and memory experiments (C, D, F) on an otherwise idle machine.

**Rules version:** results use diagnosis rules v1.1 (`diagnosis/rules.yaml`). The v1.0 corruption results are kept in `bench/results/experiment_e_v1_0.json`.

## 5. Run the assistant

**Development (native):**

```bash
.venv/Scripts/python -m uvicorn ui.app:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765. To try a question from the command line:

```bash
.venv/Scripts/python -m query.ask --role driver "What happened before the battery overheating warning?"
```

**Isolated deployment (Linux or Docker Desktop):**

```bash
docker compose -f deploy/compose.yaml up -d --build
sh deploy/check_isolation.sh
docker compose -f deploy/compose.yaml down
```

`check_isolation.sh` must print `all isolation checks passed`. Measured runs on Docker Desktop are in `bench/results/isolation_check.log`.

## 6. Moving to the ARM target (not yet done)

1. Build llama.cpp natively on the board.
2. Copy `models/` and `data/` across.
3. Run `bench.plan_latency`, `bench.query_latency`, `bench.experiment_d` and `bench.experiment_f --split test` with the board pinned to 2 cores (`taskset -c 0,1` or `--cpus=2`) and the whole stack in one 2 GB cgroup.
4. Build the container image natively on the board and rerun `deploy/check_isolation.sh`.

All latency and memory numbers in the reports were measured on an x86-64 laptop and must be replaced by these runs.
