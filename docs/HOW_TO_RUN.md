# DriveTrace — Setup and Execution Guide

**System:** DriveTrace — Evidence-Gated On-Device Vehicle Intelligence  
**Case Study Reference:** Tata Elxsi Teliport Season 4 · Case Study 5 (VehicleGPT)  
**Target Environment:** 2-core ARM64 edge processor (or x86-64 development workstation), 2 GB RAM budget, offline/air-gapped deployment  
**Target Python Version:** Python 3.14 (Native development) / Python 3.13-slim (Docker container)  
**Document Classification:** Operations, Configuration, and Verification Manual  

---

## 1. Prerequisites

Before installing and running DriveTrace, ensure your host environment satisfies the following requirements:

### 1.1 Hardware Specifications
- **Target Edge Device:** 2-core ARM64 SoC (e.g., AWS Graviton `t4g.small` or Raspberry Pi 5) with $\ge 2\text{ GB}$ RAM.
- **Development Workstation:** x86-64 or ARM64 computer running Windows 11, Linux (Ubuntu 22.04+), or macOS (Sonoma+).
- **Disk Storage:** $\ge 5\text{ GB}$ free space for virtual environment, models (~780 MB), public datasets, and Parquet partitions.

### 1.2 Mandatory Software Dependencies
- **Python:** Python 3.14 (tested: 3.14.6) or Python 3.13 / 3.12 with standard `venv` and `pip`.
- **DuckDB:** Embedded analytical database engine version `2.0.0` (installed via pip; requires no standalone database server).
- **C/C++ Runtime / Build Tools:** Standard C runtime (MSVC runtime on Windows, glibc on Linux).

### 1.3 Optional / Feature-Specific Dependencies
- **llama.cpp Inference Runtime:** Required only for Tier 1 SLM query planning and optional SLM-driven ECD diagnostic choices.
  - Development proxy: Pre-extracted in `runtime/llama-b11606/` (`llama-server.exe` for Windows x86-64).
  - Target deployment: Built natively on ARM64 or run via `ghcr.io/ggml-org/llama.cpp:server`.
- **Docker & Docker Compose:** Required only for running the hardened, multi-container isolated stack (`deploy/compose.yaml`). Tested with Docker 29.x and Compose v2.

---

## 2. Project Setup

### 2.1 Repository Layout Verification
Navigate to the root directory containing the project:

```bash
cd d:\projects\TATA\drivetrace
```

Ensure the primary directories (`schema/`, `sim/`, `adapters/`, `memory/`, `query/`, `diagnosis/`, `ecd/`, `ui/`, `deploy/`, `bench/`, `tests/`) are present.

### 2.2 Virtual Environment Initialization
Create a clean Python virtual environment inside the `drivetrace` root directory:

#### On Windows (PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

#### On Windows (Command Prompt - CMD)
```cmd
python -m venv .venv
.\.venv\Scripts\activate.bat
```

#### On Linux / macOS
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2.3 Dependency Installation
Install the pinned dependencies from `requirements.txt`:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> **Note on Container Runtime:** If you are building a lightweight deployment image, `deploy/requirements-runtime.txt` contains only the runtime dependencies (excluding pytest, hypothesis, openpyxl, and py7zr).

---

## 3. Environment and Configuration

DriveTrace is controlled via [`config/drivetrace.yaml`](file:///d:/projects/TATA/drivetrace/config/drivetrace.yaml) and targeted environment variables.

### 3.1 Configuration File: `config/drivetrace.yaml`
```yaml
store:
  duckdb_memory_limit: 512MB     # Maximum RAM allocated to DuckDB buffer manager
  duckdb_threads: 2              # CPU thread limit for DuckDB query execution
  temp_directory: data/tmp/duckdb# Scratch directory for spilling operations
  include_episodes: false        # Excludes evaluation episodes from standard vehicle history

memory:
  aggregate_windows: ["1 hour", "1 day"] # Multi-resolution aggregate windows
  capsule_before_min: 30         # Pre-event historical telemetry capture window
  capsule_after_min: 10          # Post-event recovery telemetry capture window
  capsule_severities: [warning]  # Event severities triggering detailed capsules
  ring_buffer_rows: 200000       # Preallocated hot buffer rows (18 bytes/row ≈ 3.6 MB)

paths:
  sim_history: data/sim/history  # Single-vehicle 6-week baseline timeline
  sim_episodes: data/sim/episodes# Labelled diagnostic evaluation episodes (S0–S7)
  processed: data/processed      # Normalized public dataset output
  derived: data/derived          # Query-optimized aggregations and event indices
```

### 3.2 Environment Variables

| Variable Name | Default Value | Purpose |
|---|---|---|
| `DRIVETRACE_DUCKDB_TMP` | `data/tmp/duckdb` | Overrides DuckDB's temporary directory. Used in read-only container mode to redirect spill files to `/tmp/duckdb` (tmpfs). |
| `DRIVETRACE_SLM` | `0` (unset) | Set to `1` to enable local `llama-server` integration tests in pytest (`test_query.py`). |
| `PORT` | `8765` | Default HTTP port for the web interface and API. |

---

## 4. Dataset Preparation

DriveTrace operates across both physically simulated EV telemetry and real-world public vehicle datasets.

### 4.1 Step 1: Generate Synthetic Vehicle Telemetry
DriveTrace includes an electro-thermal simulation engine that generates a 6-week continuous driving history as well as 61 three-day evaluation episodes covering fault scenarios S0–S7:

```bash
# Generate 6-week vehicle history (seed 42) and standard evaluation episodes
python -m sim.generate all

# Generate held-out blind evaluation episodes for unbiased validation
python -m sim.generate blind --seeds 6,7
```

**Outputs generated:**
- `data/sim/history/hist6w_s42/` (signals and events partitioned by date)
- `data/sim/episodes/ep_S0_00/` through `ep_S7_07/`
- `data/sim/labels/` (Ground-truth labels; strictly isolated from the assistant process)

### 4.2 Step 2: Download Public Datasets (VED & Zenodo CAN)
DriveTrace automates downloading public research datasets via [`scripts/fetch_public.py`](file:///d:/projects/TATA/drivetrace/scripts/fetch_public.py):

```bash
# Download Vehicle Energy Dataset (VED) and Zenodo CAN Bus record 2661316
python -m scripts.fetch_public --yes vedzenodo

# Extract the VED 7-Zip archives into CSV files
python -m scripts.fetch_public --extract-ved
```

**Datasets retrieved:**
- `data/raw/ved/`: VED dynamic CSV files and static vehicle specification Excel sheets.
- `data/raw/zenodo_can/`: HDF5 CAN bus recordings with GPS and chassis sensor data.

### 4.3 Step 3: Run Ingestion Adapters
Normalize the raw files into COVESA VSS-aligned, date-partitioned canonical Parquet files:

```bash
# Convert VED CSV files to canonical Parquet (default: EV sample and top vehicles)
python -m adapters.ved

# Convert Zenodo CAN HDF5 files to canonical Parquet with event edge-detection
python -m adapters.zenodo_can
```

**Outputs generated:**
- `data/processed/ved/<source_id>/signals/date=YYYY-MM-DD/*.parquet`
- `data/processed/ved/<source_id>/events/date=YYYY-MM-DD/*.parquet`
- `data/processed/zenodo_can/zenodo_can_vehicle/{signals,events}/`

### 4.4 Step 4: Validate Data Integrity
Verify that all generated and ingested datasets satisfy schema types, physical plausibility bounds, and temporal continuity:

```bash
python -m tools.validate_dataset
```

This updates [`docs/dataset_report.md`](file:///d:/projects/TATA/drivetrace/docs/dataset_report.md) with comprehensive coverage statistics, record counts, and answerability checks.

---

## 5. Model Preparation

DriveTrace uses local, quantized GGUF models running via `llama.cpp`.

### 5.1 Required Models & Checksums

Verify the presence and integrity of the model weights in `models/` against [`models/MODELS.md`](file:///d:/projects/TATA/drivetrace/models/MODELS.md):

| Model Name | Purpose | Size | SHA256 Checksum |
|---|---|---|---|
| `qwen2.5-0.5b-instruct-q4_k_m.gguf` | Primary SLM for Tier 1 query planning and ECD choices | 491,400,032 B | `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db` |
| `SmolLM2-360M-Instruct-Q4_K_M.gguf` | Compact, low-footprint SLM alternative (~478 MB RAM) | 270,590,560 B | `16c7f1667fea34bacad196a57b548effcb37614db4ab5677a20c8c7b823b9e63` |

To verify on Windows PowerShell:
```powershell
Get-FileHash models\SmolLM2-360M-Instruct-Q4_K_M.gguf -Algorithm SHA256
Get-FileHash models\qwen2.5-0.5b-instruct-q4_k_m.gguf -Algorithm SHA256
```

### 5.2 Verification of llama.cpp Runtime
The Windows development binary is located at `runtime/llama-b11606/llama-server.exe`.
To verify that inference works independently:

```powershell
.\runtime\llama-b11606\llama-server.exe -m models\SmolLM2-360M-Instruct-Q4_K_M.gguf -t 2 -c 2048 --port 8089
```

Test the health endpoint from another terminal:
```bash
curl http://127.0.0.1:8089/health
# Expected output: {"status":"ok"}
```

---

## 6. Database and Memory Initialization

Once canonical raw data exists in `data/sim/` and `data/processed/`, build the query-optimized derived layers:

```bash
# Build memory layers for all discovered vehicle sources
python -m memory.build

# Build memory layers for the labelled evaluation episodes (used in diagnostics benchmarking)
python -m memory.build --episodes
```

### What This Generates Under `data/derived/<source_id>/`:
1. `agg_1hour/date=YYYY-MM-DD/` and `agg_1day/date=YYYY-MM-DD/`: Precomputed statistics (`min`, `max`, `mean`, `p50`, `p95`).
2. `trips.parquet`: Precalculated trip boundaries, driving distances, energy consumption, and efficiency metrics.
3. `event_index.parquet`: Index of all discrete vehicle events tagged with their enclosing trip ID.
4. `capsules/<event_id>.parquet`: Extracted high-resolution telemetry covering 30 minutes before to 10 minutes after warning events.
5. `capsule_index.parquet`: Fast lookup table for warning event capsules.

### Safe Development Data Reset
To safely reset all derived memory layers without affecting raw canonical data:
```powershell
Remove-Item -Recurse -Force data\derived\*
python -m memory.build
```

---

## 7. How to Run the Complete Application

DriveTrace can be executed in two modes: **Native Workstation Mode** (for development, debugging, and local queries) and **Containerized Isolated Mode** (for security verification and edge deployment).

### 7.1 Method A: Running Natively on Host Workstation

#### Terminal 1: Launch FastAPI Web Backend
```bash
python -m uvicorn ui.app:app --host 127.0.0.1 --port 8765
```

Open your browser to:
**`http://127.0.0.1:8765`**

#### Terminal 2 (Optional): Launch llama-server for SLM Planning & ECD
If you wish to test Tier 1 SLM planning or SLM-driven ECD choices:
```bash
.\runtime\llama-b11606\llama-server.exe -m models\SmolLM2-360M-Instruct-Q4_K_M.gguf -t 2 -c 2048 --host 127.0.0.1 --port 8089
```

#### Terminal 3: Command-Line Question Interface (CLI)
You can query the assistant directly from the command line without opening a browser:

```bash
# Ask a common analytical question
python -m query.ask --source hist6w_s42 --role driver "What was my average battery temperature yesterday?"

# Ask a diagnostic question
python -m query.ask --source hist6w_s42 --role service "What happened before the battery overheating warning?"
```

---

### 7.2 Method B: Running in Hardened Docker Containers
The container stack runs 4 coordinated microservices: `ingest` (data builder), `assistant` (read-only query server), `gateway` (TCP proxy publishing port 8765), and `slm` (llama.cpp server).

```bash
# Build and start all services in detached mode (including the SLM)
docker compose -f deploy/compose.yaml --profile slm up -d --build

# Verify that security and network isolation checks pass
sh deploy/check_isolation.sh

# Stop and tear down the deployment
docker compose -f deploy/compose.yaml down
```

**Verification of Isolation:**  
Running `sh deploy/check_isolation.sh` verifies that:
- The assistant container cannot write to the data store.
- The assistant container cannot write to the container root filesystem.
- CAN bus socket creation is blocked (`AF_CAN`).
- Raw TCP egress to public IP addresses (e.g. `1.1.1.1`) is blocked.
- External DNS resolution is blocked.
- Privilege escalation (`setuid(0)`) is blocked.
- Automotive devices in `/dev/can*` are invisible.

---

## 8. Concrete Example Queries and System Responses

| Query String | Source Vehicle | Target Role | Internal Path Followed | Expected Response Summary |
|---|---|---|---|---|
| *"What was my average battery temperature yesterday?"* | `hist6w_s42` | `driver` | Tier 0 Router $\rightarrow$ `point_lookup` on `battery_temperature` $\rightarrow$ Day aggregate query | *"Battery temperature was 23.4 °C yesterday (Tue 27 Oct)."* |
| *"When did my efficiency drop the most this month?"* | `hist6w_s42` | `driver` | Tier 0 Router $\rightarrow$ `max_drop` on `energy_consumption` $\rightarrow$ Trips delta calculation | Identifies specific day with sharpest consumption spike (e.g., cold weather heater activation). |
| *"Compare energy consumption between city and highway driving."* | `hist6w_s42` | `fleet` | Tier 0 Router $\rightarrow$ `compare_groups` with `group_by="road_type"` $\rightarrow$ Speed threshold filtering ($>70\text{ km/h}$) | Compares average Wh/km between city driving and highway cruising with percentage differential. |
| *"Why did the battery overheat today?"* | `ep_S2_00` | `service` | Tier 0 Router $\rightarrow$ `diagnose_event` $\rightarrow$ Diagnoser engine $\rightarrow$ Baseline matcher $\rightarrow$ Δ-Signature $\rightarrow$ Evidence gate $\rightarrow$ Counterfactual fit $\rightarrow$ ECD renderer | Full technical diagnostic breakdown: *"Reduced battery cooling was a but-for cause of the warning within the declared battery model. Coolant pump speed when commanded: 1,420 rpm vs 3,180 rpm in comparable sessions (-55%)."* |
| *"Why did the battery overheat today?"* | `ep_S2_00` | `driver` | Same diagnostic pipeline $\rightarrow$ Role filter (`driver`) $\rightarrow$ Lexicon clause substitution | Simplified plain summary: *"The warning happened because the battery cooling is not working as well as usual. Outside temperature was 24 °C."* |
| *"Brake hard now!"* | `hist6w_s42` | Any | Tier 0 Router $\rightarrow$ `CONTROL` pattern match $\rightarrow$ Immediate refusal | *"I can only read vehicle data. I can't control the vehicle."* (Zero DB or LLM execution). |

---

## 9. Testing and Quality Verification

Execute the complete automated test suite using the virtual environment:

```bash
# Run all automated tests
python -m pytest -q
```

**Expected Result:**
```text
81 passed, 1 skipped in ~75s
```
*(The 1 skipped test is `test_slm_planner_fallback_accuracy_on_unrouted_dev_questions`, which requires a running `llama-server` on port 8089 and `DRIVETRACE_SLM=1`)*.

### Running Individual Test Suites
```bash
# Test public dataset adapters (VED and Zenodo)
python -m pytest tests/test_adapters.py -v

# Test synthetic EV physics and fault generation
python -m pytest tests/test_sim.py -v

# Test DuckDB memory layers and NumPy ring buffer
python -m pytest tests/test_memory.py -v

# Test Tier 0 routing, DST time arithmetic, and query execution
python -m pytest tests/test_query.py -v

# Test evidence gate, Δ-Signatures, and counterfactuals
python -m pytest tests/test_diagnosis.py -v

# Run Hypothesis property tests on Evidence-Compiled Decoding (Contracts C1-C3)
python -m pytest tests/test_ecd.py -v

# Test socket egress guards, RBAC, and prompt injection safety
python -m pytest tests/test_security.py -v
```

---

## 10. Benchmarking and Experiments

All quantitative performance figures reported in the research documentation trace directly to scripts in `bench/`:

### 10.1 Diagnostic Accuracy Evaluation
Evaluates diagnostic precision across all 61 episodes (Train, Dev, Test, History, and Blind splits):
```bash
python -m bench.diagnosis_eval
```
*Outputs:* `bench/results/diagnosis_eval.json`

### 10.2 Experiment B: Diagnostic Ablation Study
Ablates individual components (gate, baseline, physical model) and evaluates SLM zero-shot diagnostic capability:
```bash
python -m bench.experiment_b
```
*Outputs:* `bench/results/experiment_b.json`

### 10.3 Experiment C: End-to-End Query Latency
Measures cold and warm execution latency across common, diagnostic, and refusal query classes:
```bash
python -m bench.query_latency
```
*Outputs:* `bench/results/query_latency.json`

### 10.4 Experiment D: Peak Memory & Concurrent Ingestion Stress
Measures process RSS and query latency under background ingestion replay:
```bash
python -m bench.experiment_d
```
*Outputs:* `bench/results/experiment_d.json`

### 10.5 Experiment E: Telemetry Corruption & Evidence Gate Abstention
Injects 6 distinct sensor corruptions (missing sensors, frozen values, noise) and verifies that the evidence gate prevents hallucinated diagnoses:
```bash
python -m bench.experiment_e
```
*Outputs:* `bench/results/experiment_e.json`

### 10.6 Experiment F: Evidence-Compiled Decoding (ECD) Verification
Benchmarks free text vs static grammar vs ECD dynamic grammar across both SLMs:
```bash
python -m bench.experiment_f --split test
```
*Outputs:* `bench/results/experiment_f_test.json`

### 10.7 Generate Comprehensive Results Report
Compiles all benchmark JSON files into a consolidated Markdown document:
```bash
python -m bench.report
```
*Updates:* [`docs/p7_results.md`](file:///d:/projects/TATA/drivetrace/docs/p7_results.md)

---

## 11. Docker Deployment Architecture

### 11.1 Container Hardening Architecture
The Compose deployment enforces defense-in-depth across the services:

```yaml
x-hardening: &hardening
  cap_drop: [ALL]                        # Drops all Linux capabilities
  security_opt: ["no-new-privileges:true"] # Disallows privilege escalation
  read_only: true                        # Completely read-only root filesystem
  tmpfs: ["/tmp:size=256m"]              # Writable temporary storage in RAM only
  restart: unless-stopped
```

### 11.2 Microservice Roles & Network Isolation
- **`ingest` service:** The only service with write access to `data/` (`../data:/app/data`). Has `network_mode: none` (completely air-gapped from network).
- **`assistant` service:** Mounts data read-only (`../data:/app/data:ro`). Connected *only* to the `internal_only` network (`internal: true`). Cannot access the internet or resolve external DNS.
- **`gateway` service:** Runs [`security/tcp_proxy.py`](file:///d:/projects/TATA/drivetrace/security/tcp_proxy.py). The only service bridging the `edge` network and `internal_only` network. Publishes exclusively to `127.0.0.1:8765`. Contains zero application logic.
- **`slm` service (Profile `slm`):** Runs `llama-server` in CPU mode on the internal network. Mounts `models/` read-only.

---

## 12. Troubleshooting Guide

| Issue / Symptom | Root Cause | How to Diagnose | Exact Resolution |
|---|---|---|---|
| `ModuleNotFoundError: No module named 'duckdb'` (or similar) | Python command executed outside active `.venv` environment | Run `python -c "import sys; print(sys.executable)"` | Ensure virtual environment is activated, or call `.venv/Scripts/python.exe` directly. |
| `KeyError: "unknown source 'hist6w_s42'"` | Synthetic data or memory layers have not been generated | Inspect `data/sim/history/` and `data/derived/` | Run `python -m sim.generate history` followed by `python -m memory.build`. |
| `PermissionError` when opening external socket | Egress guard active (`security/egress.py`) | Check if outbound connection is non-loopback | This is expected behavior. The assistant is strictly offline and cannot contact external IP addresses. |
| Port 8765 already in use | Another Uvicorn or gateway instance is running | Run `netstat -ano \| findstr 8765` | Terminate the existing process or pass `--port <new_port>` to Uvicorn. |
| DuckDB out of temporary disk space | Scratch disk filled during large batch aggregation | Check disk space on default temp drive | Set `DRIVETRACE_DUCKDB_TMP` environment variable to a drive with $\ge 2\text{ GB}$ free space. |
| `llama-server` fails to start on ARM64 | Windows binary (`llama-b11606`) used on non-Windows host | Check system architecture: `uname -m` | On Linux ARM64, build `llama.cpp` natively or use the Docker Compose `slm` container. |
| `PlanError: diagnose_event needs an event type` | User query was ambiguous or unrouted | Inspect query plan output in UI or CLI | Provide an explicit event type (e.g., "Why did the battery warning come on?"). |
| UI shows blank charts | Selected role does not have permission to view technical signals | Check selected role in UI toggle | Switch from `Driver` role to `Service` role to inspect technical BMS signals. |

---

## 13. Quick Start (Minimal Command Sequence)

To clone, initialize, build, and run DriveTrace from scratch:

```bash
# 1. Initialize environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # (or source .venv/bin/activate on Linux/macOS)
pip install -r requirements.txt

# 2. Generate simulation history and evaluation episodes
python -m sim.generate all

# 3. Download and ingest public vehicle datasets
python -m scripts.fetch_public --yes vedzenodo
python -m scripts.fetch_public --extract-ved
python -m adapters.ved
python -m adapters.zenodo_can

# 4. Build derived memory layers (aggregates, trips, capsules)
python -m memory.build
python -m memory.build --episodes

# 5. Run test suite verification
python -m pytest -q

# 6. Launch Assistant Web Application
python -m uvicorn ui.app:app --host 127.0.0.1 --port 8765
```

Open **`http://127.0.0.1:8765`** in your browser to interact with the system.

