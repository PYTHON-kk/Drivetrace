"""Build the query-aware memory layers for one or all sources (FR-MEM-03..05).

    python -m memory.build                 # every discovered source
    python -m memory.build --source ved_455

Writes data/derived/<key>/:
    agg_1hour/date=*/, agg_1day/date=*/  periodic aggregates per signal (FR-MEM-03)
    trips.parquet                         one row per ignition cycle: distance, energy, fuel, conditions
    event_index.parquet                   all events with their trip number (FR-MEM-05)
    capsules/<event_id>.parquet           raw signals around warning events (FR-MEM-04)
    capsule_index.parquet, build_report.json
"""

import argparse
import json
import shutil
import time

from memory.store import Store, window_dir

P = "Vehicle.Powertrain.TractionBattery."
WIDE_SIGNALS = {
    "kmh": "Vehicle.Speed",
    "power_w": P + "CurrentPower",
    "amp": P + "CurrentCurrent",
    "volt": P + "CurrentVoltage",
    "soc": P + "StateOfCharge.Current",
    "odo_km": "Vehicle.TraveledDistance",
    "tripm_km": "Vehicle.TripMeterReading",
    "maf": "Vehicle.OBD.MAF",
    "oat": "Vehicle.Exterior.AirTemperature",
    "heater_w": "Vehicle.VendorExt.Cabin.HeaterPower",
    "ac_w": "Vehicle.VendorExt.Cabin.AirConditioningPower",
}
MAX_DT_S = 5.0  # gaps longer than this do not count as driving time
AFR, PETROL_G_PER_L = 14.7, 745.0


def build_aggregates(store: Store, key: str, window: str) -> int:
    out = store.derived(key) / window_dir(window)
    if out.exists():
        shutil.rmtree(out)
    store.con.execute(f"""
        COPY (
            SELECT time_bucket(INTERVAL '{window}', ts) AS window_start, signal_path, any_value(unit) AS unit,
                   count(*) AS n, min(value) AS min, max(value) AS max, avg(value) AS mean,
                   quantile_cont(value, 0.5) AS p50, quantile_cont(value, 0.95) AS p95,
                   min(ts) AS first_ts, max(ts) AS last_ts,
                   CAST(time_bucket(INTERVAL '{window}', ts) AS DATE) AS date
            FROM {store.signals(key)} GROUP BY ALL
        ) TO '{out.as_posix()}' (FORMAT parquet, COMPRESSION zstd, PARTITION_BY (date))""")
    return store.query(f"SELECT count(*) FROM {store.aggregates(key, window)}")[0][0]


def build_trips(store: Store, key: str) -> int:
    con = store.con
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE trip_bounds AS
        WITH ign AS (
            SELECT ts, event_type, lead(ts) OVER w AS next_ts, lead(event_type) OVER w AS next_type
            FROM {store.events(key)} WHERE event_type IN ('IGNITION_ON', 'IGNITION_OFF')
            WINDOW w AS (ORDER BY ts, seq))
        SELECT CAST(row_number() OVER (ORDER BY ts) AS INTEGER) AS trip_no, ts AS t_on, next_ts AS t_off
        FROM ign WHERE event_type = 'IGNITION_ON' AND next_type = 'IGNITION_OFF'""")
    cols = ",\n".join(f"max(value) FILTER (WHERE signal_path = '{p}') AS {c}" for c, p in WIDE_SIGNALS.items())
    paths = ", ".join(f"'{p}'" for p in WIDE_SIGNALS.values())
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE trip_samples AS
        WITH wide AS (SELECT ts, {cols} FROM {store.signals(key)} WHERE signal_path IN ({paths}) GROUP BY ts),
             tagged AS (SELECT w.*, b.trip_no, b.t_off FROM wide w ASOF JOIN trip_bounds b ON w.ts >= b.t_on)
        SELECT *, coalesce(least(epoch(lead(ts) OVER (PARTITION BY trip_no ORDER BY ts) - ts), {MAX_DT_S}), 0) AS dt
        FROM tagged WHERE ts <= t_off""")
    out = store.derived(key) / "trips.parquet"
    con.execute(f"""
        COPY (
            SELECT b.trip_no, b.t_on, b.t_off, epoch(b.t_off - b.t_on) AS duration_s,
                   coalesce(max(odo_km) - min(odo_km), max(tripm_km) - min(tripm_km),
                            sum(kmh * dt) / 3600) AS km,
                   sum(kmh * dt) / nullif(sum(dt) FILTER (WHERE kmh > 0.5), 0) AS avg_moving_kmh,
                   max(kmh) AS max_kmh,
                   coalesce(sum(dt) FILTER (WHERE kmh > 70), 0)
                       / nullif(sum(dt) FILTER (WHERE kmh > 0.5), 0) AS highway_share,
                   -sum(coalesce(power_w, amp * volt) * dt) / 3600 AS battery_wh,
                   sum(maf * dt) / ({AFR} * {PETROL_G_PER_L}) AS fuel_l_est,
                   avg(oat) AS ambient_c, avg(heater_w) AS heater_w, avg(ac_w) AS ac_w,
                   arg_min(soc, ts) FILTER (WHERE soc IS NOT NULL) AS soc_start,
                   arg_max(soc, ts) FILTER (WHERE soc IS NOT NULL) AS soc_end,
                   count(*) AS samples
            FROM trip_bounds b LEFT JOIN trip_samples s USING (trip_no)
            GROUP BY b.trip_no, b.t_on, b.t_off ORDER BY b.trip_no
        ) TO '{out.as_posix()}' (FORMAT parquet)""")
    return store.query(f"SELECT count(*) FROM {store.trips(key)}")[0][0]


def build_event_index(store: Store, key: str) -> int:
    out = store.derived(key) / "event_index.parquet"
    store.con.execute(f"""
        COPY (
            SELECT e.*, CASE WHEN e.ts < b.t_off OR (e.ts = b.t_off AND e.event_type = 'IGNITION_OFF')
                             THEN b.trip_no END AS trip_no
            FROM {store.events(key)} e ASOF LEFT JOIN trip_bounds b ON e.ts >= b.t_on
            ORDER BY e.ts, e.seq
        ) TO '{out.as_posix()}' (FORMAT parquet)""")
    return store.query(f"SELECT count(*) FROM {store.event_index(key)}")[0][0]


def build_capsules(store: Store, key: str) -> int:
    mc = store.cfg["memory"]
    sev = ", ".join(f"'{s}'" for s in mc["capsule_severities"])
    caps = store.derived(key) / "capsules"
    if caps.exists():
        shutil.rmtree(caps)
    caps.mkdir(parents=True)
    events = store.query(f"""SELECT event_id, ts, event_type FROM {store.event_index(key)}
                             WHERE severity IN ({sev}) ORDER BY ts""")
    rows = []
    for event_id, ts, event_type in events:
        path = caps / f"{event_id}.parquet"
        store.con.execute(f"""
            COPY (SELECT * FROM {store.signals(key)}
                  WHERE ts BETWEEN TIMESTAMPTZ '{ts.isoformat()}' - INTERVAL {int(mc['capsule_before_min'])} MINUTE
                               AND TIMESTAMPTZ '{ts.isoformat()}' + INTERVAL {int(mc['capsule_after_min'])} MINUTE
                  ORDER BY ts, signal_path) TO '{path.as_posix()}' (FORMAT parquet)""")
        n, n_sig = store.query(f"SELECT count(*), count(DISTINCT signal_path) FROM read_parquet('{path.as_posix()}')")[0]
        rows.append((event_id, ts, event_type, n, n_sig))
    import pandas as pd

    df = pd.DataFrame(rows, columns=["event_id", "ts", "event_type", "rows", "signals"])
    df["before_min"], df["after_min"] = mc["capsule_before_min"], mc["capsule_after_min"]
    store.con.register("capsule_df", df)
    store.con.execute(f"COPY capsule_df TO '{(store.derived(key) / 'capsule_index.parquet').as_posix()}' (FORMAT parquet)")
    store.con.unregister("capsule_df")
    return len(rows)


def build_source(store: Store, key: str) -> dict:
    store.derived(key).mkdir(parents=True, exist_ok=True)
    report, t0 = {"source": key}, time.perf_counter()
    for window in store.cfg["memory"]["aggregate_windows"]:
        report[f"aggregates_{window_dir(window)}"] = build_aggregates(store, key, window)
    report["trips"] = build_trips(store, key)
    report["events"] = build_event_index(store, key)
    report["capsules"] = build_capsules(store, key)
    report["seconds"] = round(time.perf_counter() - t0, 1)
    (store.derived(key) / "build_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="source key; default: all discovered sources")
    ap.add_argument("--episodes", action="store_true", help="build the labelled evaluation episodes only")
    args = ap.parse_args()
    store = Store(include_episodes=args.episodes or None)
    if args.source:
        keys = [args.source]
    elif args.episodes:
        keys = [k for k, s in store.sources.items() if s.dataset == "sim_episodes"]
    else:
        keys = [k for k, s in store.sources.items() if s.dataset != "sim_episodes"]
    for key in keys:
        r = build_source(store, key)
        print(f"  {key:<20} trips={r['trips']:<5} events={r['events']:<6} capsules={r['capsules']:<3} "
              f"agg_1h={r['aggregates_agg_1hour']:<7} {r['seconds']} s")


if __name__ == "__main__":
    main()
