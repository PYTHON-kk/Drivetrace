# Data manifest

Everything under `data/` except this file is git-ignored. Regenerate or re-download it with the commands below.

## Generated (synthetic) - ready

| Dataset | Path | How to regenerate | Size | Notes |
|---|---|---|---|---|
| Six-week single-vehicle history `hist6w_s42` | `data/sim/history/hist6w_s42/` | `python -m sim.generate history` | ~26 MB Parquet, ~2.2 M signal rows, 636 events | 2026-09-16 to 2026-10-28 (Europe/Berlin), 18 signals, scenarios S1-S6 injected |
| Labelled episodes S0-S7 | `data/sim/episodes/ep_<S>_<seed>/` | `python -m sim.generate episodes` | ~168 MB, 45 episodes, ~14.6 M rows | 3 days each; days 0-1 healthy baselines, day 2 trigger |
| Ground-truth labels | `data/sim/labels/` | written by the generator | <1 MB | `history_*.json`, `episodes/*.json`, `splits.json` (train 21 / dev 7 / test 17). **Never read by the assistant.** |

All generated rows carry `source_type = simulated`; `scenario_id` is the generation run ID, never the fault label.
Model and scenario parameters: `sim/vehicle.py`, `sim/scenarios.yaml`. Validation report: `docs/dataset_report.md`.

## Processed (adapter output, canonical schema) - ready

| Dataset | Path | How to regenerate | Size | Notes |
|---|---|---|---|---|
| VED sample | `data/processed/ved/<source_id>/{signals,events}/date=*/` | `python -m adapters.ved` (default `--vehicles sample`) | 145 MB, 38.7 M rows | 16 vehicles, one source each: EVs `ved_10`, `ved_455`, `ved_541`; 5 ICE, 5 HEV, 3 PHEV with the most trips. `--vehicles all` converts the whole fleet. Trip boundaries become derived `IGNITION_ON/OFF` events. Vehicle metadata: `data/processed/ved/_vehicles/vehicles.parquet`. |
| Zenodo CAN | `data/processed/zenodo_can/zenodo_can_vehicle/{signals,events}/date=*/` | `python -m adapters.zenodo_can` | 10.5 MB, 2.15 M rows | 5 trips, 28 signals decimated to 1-10 Hz; 19 cruise-control `ADAS_*` events, 149 `LOGGER_TIMECODE_*` notices, no warning lamps active. |

Assumptions in the adapters (also in the mapping files):
- VED local time zone is America/Detroit (the README gives none).
- All Zenodo trips are from one vehicle; Zenodo local time is Europe/Vienna (from GPS position).
- Zenodo steering angle and CAN yaw rate are magnitudes only; the signed yaw rate comes from the IMU gyroscope.
- Zenodo IMU axes: X forward, Y lateral, Z vertical.
- Recorded files store strings as plain VARCHAR and timestamps in microseconds; the simulator uses dictionary strings and milliseconds. DuckDB reads both the same way.

Ingest summaries: `data/processed/ved/ingest_report.json`, `data/processed/zenodo_can/ingest_report.json`.

## Public (real) - downloaded 2026-10-29

### VED - Vehicle Energy Dataset (Oh, LeBlanc, Peng), github.com/gsoh/VED, Apache 2.0

Fetch: `python -m scripts.fetch_public --yes ved`, then `python -m scripts.fetch_public --extract-ved`.

| File | Size | md5 (computed locally; GitHub publishes none) |
|---|---|---|
| `data/raw/ved/VED_DynamicData_Part1.7z` | 82,723,769 B | 364ef10d5bcd77499d3352a0afd8cc66 |
| `data/raw/ved/VED_DynamicData_Part2.7z` | 93,662,910 B | 495a79196ed29561e0900bcebf96c5dc |
| `data/raw/ved/VED_Static_Data_ICE&HEV.xlsx` | 21,193 B | d1c5bfa0697f4fba62b309e6faa1593a |
| `data/raw/ved/VED_Static_Data_PHEV&EV.xlsx` | 9,763 B | 93a8f788c72288308d149cb8d07ad12a |
| `data/raw/ved/csv/` (54 weekly CSVs, extracted) | 3.0 GB | - |
| `data/raw/ved/ved_dynamic.parquet` (all CSVs, typed, NaN to NULL) | 239 MB | - |

Profile: 22.4 M rows, 384 vehicles, 32,552 trips, DayNum 1-375 (Nov 2017 - Nov 2018), median sample interval 0.5 s (irregular, OBD-II).

| Type | Vehicles | Trips | Rows | Notes |
|---|---|---|---|---|
| ICE | 264 | 18,936 | 13.1 M | `Fuel Rate` is empty: estimate fuel from `MAF` (79 % coverage) |
| HEV | 93 | 9,507 | 5.6 M | MAF 99 % coverage, no HV battery signals |
| PHEV | 24 | 3,605 | 3.2 M | HV battery current/SoC/voltage present |
| EV | 3 | 504 | 0.48 M | VehId 10 (201 trips, 373 days) and 455 (293 trips, 365 days): **a real year of EV data** with HV current, SoC, voltage, heater power, AC power and outside air temperature; VehId 541 is short (10 trips) |

### Zenodo "Vehicle CAN bus data (with GPS)" (Kaiser, Stocker, Festl), record 2661316, CC BY 4.0

Fetch: `python -m scripts.fetch_public --yes zenodo` (md5 checked against Zenodo).

| File | Size | md5 | Status |
|---|---|---|---|
| `20181113_Driver1_Trip1.hdf` | 187,342,284 B | 2bc58f55f737378259f30f71d98a2859 | **Unreadable: truncated at the source** (HDF5 header expects 187,342,988 B; md5 matches Zenodo, so re-downloading will not help) |
| `20181113_Driver1_Trip2.hdf` | 64,809,476 B | 155453490146b4e0acfb407670ba6d8f | OK, 54,835 samples |
| `20181114_Driver2_Trip1.hdf` | 29,356,364 B | 5e7f60f7a382ccb938c4c88dbeda05d3 | OK, 24,783 samples (20.6 min) |
| `20181114_Driver2_Trip2.hdf` | 32,418,816 B | 0571729c23a92fddf6054a22bb71b990 | OK, 27,386 samples |
| `20181114_Driver2_Trip3.hdf` | 160,766,584 B | a498262b5db34c68a9c68a09b20c0b27 | OK, 136,154 samples |
| `20181116_Driver1_Trip3.hdf` | 36,385,576 B | 1d5d0eae6fdc50acd31ac77dbe7d7157 | OK, 30,746 samples |

Profile: petrol car, 20 Hz logger (Dewesoft). Groups `CAN` (115 decoded signals: vehicle and wheel speeds, yaw rate, steering angle, accelerator, engine/oil/gearbox temperatures, fuel consumption, warning lamps such as `ENG_Hotlamp`, `ENG_OBD2_Lamp`, `WIV_Oilpr_Warn_Engine`), `GPS` (10), `Plugins` (IMU, 24), `Math` (5), `AI` (1).
Gotchas for the adapter:
- Each dataset is `(N, 2)` with **column 0 = value, column 1 = time in seconds**, the reverse of the files' own "Column 0: Time" description.
- Start time is in the file attribute `Start time` (local, `dd.mm.yyyy HH:MM:SS`).
- File attributes contain dozens of logger notices ("Time code lost/restored"): real data-quality events, useful for evidence-gate testing.

## Public (real) - manual download still needed

| Dataset | Use in DriveTrace | Access | Licence | Size | Path |
|---|---|---|---|---|---|
| Mendeley "Real-world EV data: driving and charging" v2 (Allam et al.), DOI 10.17632/7vdkzpnjgj.2 | Real single-EV long-term battery timeline | Manual download from data.mendeley.com | CC BY-NC 3.0 (non-commercial) | check on page | `data/raw/mendeley_ev/` |
| TUM "Battery and Heating Data in Real Driving Cycles" (BMW i3, 72 trips), DOI 10.21227/6jr9-5235 | Real cause of range loss (heating, ambient) | Manual download, free IEEE DataPort account | see DataPort page | check on page | `data/raw/tum_i3/` |
| comma2k19 (optional) | Raw CAN + camera for the "camera feeds" story | Manual, one or two segments only | MIT per mirrors; confirm LICENSE | very large; subset only | `data/raw/comma2k19/` |

Record download date and checksum for each file here once fetched.
