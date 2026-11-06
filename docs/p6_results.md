# P6 results - UI, isolation and roles

**Date:** 2026-10-29
**Status:** UI, process-level controls, privacy and deletion are built and tested. The container deployment was **built and run on Docker Desktop (Linux VM, x86-64) on the development machine**, and all 7 OS-level isolation checks pass there. The reportable run on the Linux/ARM target is still to do.

## Local web UI (`ui/`)

Run it with:

```bash
drivetrace/.venv/Scripts/python -m uvicorn ui.app:app --app-dir drivetrace --host 127.0.0.1 --port 8765
```

| Requirement | As built |
|---|---|
| FR-UI-01 question, answer, evidence | Question box with the case-study questions as one-click examples. The answer highlights every number (all of them come from stored data). The evidence panel lists claims with epistemic-level badges, the gate status, the baseline sessions and the event IDs. |
| Timeline chart | Inline SVG small multiples around the event (battery temperatures, current, pump speed, coolant), with a marker at the event time. Daily series for trend questions; bars for counts. Axes use the **vehicle's** time zone, the same as the answer text. |
| FR-UI-02 role selector | Driver / Service / Fleet. Driver: plain-language conclusion, temperature chart only, no technical evidence. |
| FR-UI-03 latency breakdown | Per-stage bar on a 0-2 s scale, plus the browser round trip. |
| Offline | No CDN, fonts or third-party scripts; vanilla JS. Works with networking disabled. Checked in light, dark and phone width (375 px, no horizontal scroll). |

A diagnosis question takes about 0.4-0.5 s in the UI on the x86 proxy (the first one per vehicle is slower while it caches); common questions take a few milliseconds.

## Security controls

| Layer | Control | Verified by |
|---|---|---|
| Process | Assistant binds to 127.0.0.1; no `/docs` or OpenAPI; the only POST is `/api/ask`, which reads | `test_api_surface_is_read_only` |
| Process | **Egress guard**: any socket connection to a non-loopback address raises `PermissionError` (installed before the app loads) | `test_egress_guard_*`, `test_assistant_app_installs_the_egress_guard` |
| Code | Assistant packages contain no CAN, serial, `/dev/`, subprocess or external-URL use (one documented dev-only exception: `LocalServer` in `query/planner.py`, which starts llama-server for benches) | `test_assistant_code_has_no_vehicle_or_network_interfaces` |
| Data | Asking all 142 test questions × 3 roles changes no stored file | `test_asking_every_question_changes_no_stored_file` |
| Control | Control requests ("Brake hard now", "Unlock the doors", ...) are refused for every role; the plan vocabulary has no actuation intent | `test_control_requests_are_refused_for_every_role` |
| Injection | An ADAS event whose attribute carries "IGNORE ALL RULES ... BRAKE NOW `<script>`" is reported as data; the plan comes from the question only; the browser inserts all server text with `textContent` | `test_injection_text_in_logs_is_treated_as_data`, `test_client_never_inserts_server_text_as_html` |
| Input | Over-long questions, path-like source names and unknown roles are rejected | `test_bad_requests_are_rejected` |
| OS (run on Docker Desktop) | `deploy/compose.yaml`: ingest is the only writer and the only container that may get CAN access. The assistant has a read-only root filesystem and data mount, `cap_drop: ALL`, `no-new-privileges`, user 10001, a 1 GB / 2 CPU limit, and sits **only on an `internal` network** (no route out). A gateway forwarder (`security/tcp_proxy.py`, no application logic) is the only container on both networks and publishes the UI on 127.0.0.1. The SLM runs in its own read-only container on the internal network. | `deploy/check_isolation.sh`: 7/7 blocked (see below) |

## Privacy

- **Data minimisation:** no supported question needs location or driver identity, so the assistant never serves them to any role.
  - The series API refuses every signal tagged `privacy: location` (403).
  - Identity keys (`driver`, `vin`, `user`, ...) are removed from event attributes in the executor and the diagnoser. The Zenodo data names its drivers, and those names never appear in a response.
- **Deletion:** `python -m ingest.delete_personal --source <key> [--before DATE] [--dry-run]` runs in the ingest process. It:
  - removes location rows and identity attributes from the raw store;
  - rebuilds the aggregates, capsules and event index, which held copies;
  - appends a log line of counts only.

  On the Zenodo car it removes 54,792 location rows and 10 driver names. A test checks that nothing remains in any layer and that other signals are untouched.
- **Roles:** the driver sees plain conclusions and temperatures only; service and fleet see the technical evidence (still without location or identity).

## Container run (Docker Desktop 29.9.2, Linux VM on the x86 laptop)

- **Image:** `drivetrace:local` (python:3.13-slim + pinned runtime wheels), 763 MB. Mostly pandas, pyarrow, DuckDB and NumPy; trimming it is a P7/P8 item.
- **Start-up:** ingest rebuilt the memory layers for 18 sources and exited 0 in 270 s. That is about 4× native, because data is read through Docker Desktop's mount of the Windows disk. The assistant then started only after ingest succeeded.

**Isolation checks:** `bench/results/isolation_check.log`

| Forbidden action from inside the assistant container | First run | After the fix |
|---|---|---|
| write to the data store | blocked | blocked |
| write anywhere in the image | blocked | blocked |
| open a CAN socket | blocked | blocked |
| outbound TCP with a raw socket (bypassing the in-process guard) | **succeeded** | blocked |
| DNS lookup of an external name | **succeeded** | blocked |
| gain privileges (setuid 0) | blocked | blocked |
| see CAN / serial devices | blocked | blocked |

**Finding:** the first design (a bridge with `enable_ip_masquerade=false`) did **not** stop egress on Docker Desktop. The checks caught it, and the assistant was moved to an `internal: true` network behind the gateway. This confirms solution_journey Defect 10: isolation has to be shown by negative tests, not assumed from configuration.

**Through the gateway from the host:**
- the UI loads (200), and answers match the native run;
- the location series is refused (403);
- common questions take about 15 ms; a diagnosis takes 1.7-2.2 s (about 0.4 s natively, slower here because of the mounted Windows disk).

**Memory:** assistant 116 MiB of its 1 GiB limit (without the SLM); gateway 19 MiB.

The gateway itself has outbound network access (it is on the normal bridge). It runs only the byte forwarder, with the same hardening. A stricter option on the target is host firewall rules, or a Unix-socket bind instead of a TCP port.

## Tests

81 passed, 1 skipped (the skip needs a running llama-server): 14 security/privacy tests plus the earlier suites.

## Not done yet

- **Repeat the container run on the Linux/ARM target** (build natively there; `deploy/check_isolation.sh`). The Docker Desktop run is a proxy.
- **Whole-stack memory under one limit** (Experiment D) - the compose file sets the limits but nothing has been measured inside them.
- **A real CAN reader** for the ingest container; the demo ingest step only rebuilds memory layers from stored data.
