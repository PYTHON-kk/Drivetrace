"""VED adapter: data/raw/ved -> data/processed/ved/<source_id>/{signals,events}.

    python -m adapters.ved                    # default: the 3 EVs plus a small ICE/HEV/PHEV sample
    python -m adapters.ved --vehicles ev      # EVs only
    python -m adapters.ved --vehicles 10,455  # explicit VehIds
    python -m adapters.ved --vehicles all     # whole fleet (large)

Each VED vehicle becomes its own source (`ved_<VehId>`); vehicles are never merged.
Trip boundaries become derived IGNITION_ON/IGNITION_OFF events.
"""

import argparse
from pathlib import Path

import pandas as pd

from adapters.common import (EVENT_COLUMNS, PROCESSED, ROOT, SIGNAL_COLUMNS, connect, copy_partitioned,
                             load_mapping, reset_dir, write_report)
from memory.store import write_source_meta

RAW = ROOT / "data" / "raw" / "ved"
TYPED = RAW / "ved_dynamic.parquet"
OUT = PROCESSED / "ved"
MAPPING = Path(__file__).with_name("ved_mapping.yaml")
SAMPLE_PER_TYPE = {"ICE": 5, "HEV": 5, "PHEV": 3}


def build_typed_parquet(con):
    """One-time conversion of the 54 weekly CSVs into a typed Parquet file (NaN -> NULL)."""
    src = (f"read_csv('{(RAW / 'csv').as_posix()}/*.csv', delim=',', header=true, union_by_name=true, "
           "all_varchar=true, strict_mode=false)")
    cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]

    def cast(c: str) -> str:
        q = f'"{c}"'
        if c in ("VehId", "Trip"):
            return f"TRY_CAST({q} AS INTEGER) AS {q}"
        if c.startswith("Timestamp"):
            return f"TRY_CAST({q} AS BIGINT) AS {q}"
        return f"TRY_CAST(NULLIF({q}, 'NaN') AS DOUBLE) AS {q}"

    con.execute(f"COPY (SELECT {', '.join(cast(c) for c in cols)} FROM {src}) TO '{TYPED.as_posix()}' "
                "(FORMAT parquet, COMPRESSION zstd)")


def load_static() -> pd.DataFrame:
    ev = pd.read_excel(RAW / "VED_Static_Data_PHEV&EV.xlsx")
    ice = pd.read_excel(RAW / "VED_Static_Data_ICE&HEV.xlsx").rename(columns={"Vehicle Type": "EngineType"})
    static = pd.concat([ev, ice], ignore_index=True)
    static.columns = [c.replace(" & ", "_").replace(" ", "_") for c in static.columns]
    static = static.replace("NO DATA", None)
    static["Generalized_Weight"] = pd.to_numeric(static["Generalized_Weight"], errors="coerce")
    return static.astype({c: "string" for c in static.columns if static[c].dtype == object})


def select_vehicles(con, spec: str, static: pd.DataFrame) -> list[int]:
    if spec == "all":
        return sorted(int(v) for (v,) in con.execute(f"SELECT DISTINCT VehId FROM '{TYPED.as_posix()}'").fetchall())
    if spec not in ("ev", "sample"):
        return [int(v) for v in spec.split(",")]
    con.register("static", static)
    chosen = [int(v) for v in static.loc[static.EngineType == "EV", "VehId"]]
    if spec == "sample":
        for kind, n in SAMPLE_PER_TYPE.items():
            rows = con.execute(f"""SELECT d.VehId FROM '{TYPED.as_posix()}' d JOIN static s USING (VehId)
                                   WHERE s.EngineType = '{kind}' GROUP BY 1 ORDER BY count(DISTINCT Trip) DESC, 1
                                   LIMIT {n}""").fetchall()
            chosen += [int(v) for (v,) in rows]
    return chosen


def convert(vehicles: list[int]) -> dict:
    mapping = load_mapping(MAPPING)
    con = connect()
    if not TYPED.exists():
        print("building typed Parquet from CSVs ...")
        build_typed_parquet(con)
    static = load_static()
    if vehicles is None:
        vehicles = select_vehicles(con, "sample", static)

    units = pd.DataFrame([(m["path"], m["unit"]) for m in mapping["signals"]], columns=["signal_path", "unit"])
    con.register("units", units)
    cols = ",\n".join(f'{m.get("expr") or chr(34) + m["raw"] + chr(34)} * {float(m.get("scale", 1.0))} AS "{m["path"]}"'
                      for m in mapping["signals"])
    ts_expr = (f"timezone('{mapping['timezone']}', TIMESTAMP '{mapping['day0_local']}' + "
               "to_milliseconds(CAST(round((DayNum - 1) * 86400000) AS BIGINT) + \"Timestamp(ms)\"))")
    report = {"dataset": "ved", "scenario_id": mapping["scenario_id"], "timezone_assumed": mapping["timezone"],
              "sources": {}}
    for veh in vehicles:
        source_id = f"ved_{veh}"
        base = f"""SELECT Trip, {ts_expr} AS ts,
                   {cols}
                   FROM '{TYPED.as_posix()}' WHERE VehId = {veh}"""
        con.execute(f"CREATE OR REPLACE TEMP TABLE base AS {base}")
        long = f"""SELECT l.ts, l.signal_path, l.value, u.unit, 'valid' AS quality, '{source_id}' AS source_id,
                          '{mapping['scenario_id']}' AS scenario_id, '{mapping['source_type']}' AS source_type,
                          row_number() OVER (ORDER BY l.ts, l.signal_path) AS seq, CAST(l.ts AS DATE) AS date
                   FROM (UNPIVOT (SELECT * EXCLUDE (Trip) FROM base) ON COLUMNS(* EXCLUDE (ts))
                         INTO NAME signal_path VALUE value) l
                   JOIN units u USING (signal_path)
                   WHERE l.value IS NOT NULL AND NOT isnan(l.value)"""
        copy_partitioned(con, f"SELECT {SIGNAL_COLUMNS}, date FROM ({long})", OUT / source_id / "signals")

        events = f"""WITH trips AS (SELECT Trip, min(ts) AS t_on, max(ts) AS t_off FROM base GROUP BY Trip),
                     e AS (SELECT t_on AS ts, Trip, 'IGNITION_ON' AS event_type FROM trips
                           UNION ALL SELECT t_off, Trip, 'IGNITION_OFF' FROM trips)
                     SELECT ts, '{source_id}-t' || Trip || '-' || lower(event_type) AS event_id, event_type,
                            'body' AS subsystem, 'info' AS severity, '' AS code,
                            json_object('trip', Trip, 'derived', 'VED trip boundary') AS attrs,
                            '{source_id}' AS source_id, '{mapping['scenario_id']}' AS scenario_id,
                            '{mapping['source_type']}' AS source_type,
                            row_number() OVER (ORDER BY ts, event_type DESC) AS seq, CAST(ts AS DATE) AS date
                     FROM e"""
        copy_partitioned(con, f"SELECT {EVENT_COLUMNS}, date FROM ({events})", OUT / source_id / "events")

        write_source_meta(OUT / source_id, source_id=source_id, dataset="ved", source_type="recorded",
                          timezone=mapping["timezone"], vehicle_id=veh,
                          description="Vehicle Energy Dataset (gsoh/VED), one vehicle")
        n_rows, n_sig, t0, t1 = con.execute(
            f"SELECT count(*), count(DISTINCT signal_path), min(ts), max(ts) "
            f"FROM read_parquet('{(OUT / source_id / 'signals').as_posix()}/*/*.parquet')").fetchone()
        n_trips = con.execute("SELECT count(DISTINCT Trip) FROM base").fetchone()[0]
        kind = static.loc[static.VehId == veh, "EngineType"]
        report["sources"][source_id] = {"VehId": veh, "engine_type": kind.iloc[0] if len(kind) else None,
                                        "trips": n_trips, "signal_rows": n_rows, "signals": n_sig,
                                        "start_utc": t0, "end_utc": t1}
        print(f"  {source_id:<9} {report['sources'][source_id]['engine_type']:<5} trips={n_trips:<5} rows={n_rows:>10,} signals={n_sig}")

    reset_dir(OUT / "_vehicles")
    (OUT / "_vehicles").mkdir()
    static.to_parquet(OUT / "_vehicles" / "vehicles.parquet", index=False)
    write_report(OUT / "ingest_report.json", report)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vehicles", default="sample", help="sample | ev | all | comma-separated VehIds")
    args = ap.parse_args()
    con = connect()
    if not TYPED.exists():
        build_typed_parquet(con)
    vehicles = select_vehicles(con, args.vehicles, load_static())
    con.close()
    r = convert(vehicles)
    print(f"ved: {len(r['sources'])} sources, {sum(s['signal_rows'] for s in r['sources'].values()):,} signal rows")


if __name__ == "__main__":
    main()
