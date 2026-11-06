"""Shared helpers for recorded-data adapters (FR-ING-04/05).

Adapters write the same canonical long format as the simulator:
    data/processed/<dataset>/<source_id>/{signals,events}/date=YYYY-MM-DD/*.parquet
Every mapped signal must exist in schema/vss_mapping.yaml with the same unit; anything else is rejected.
"""

import json
import shutil
from pathlib import Path

import duckdb
import yaml

from schema.canonical import load_catalogue

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"

SIGNAL_COLUMNS = "ts, signal_path, value, unit, quality, source_id, scenario_id, source_type, seq"
EVENT_COLUMNS = ("ts, event_id, event_type, subsystem, severity, code, attrs, "
                 "source_id, scenario_id, source_type, seq")


def load_mapping(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        mapping = yaml.safe_load(f)
    catalogue = load_catalogue()["signals"]
    for m in mapping["signals"]:
        entry = catalogue.get(m["path"])
        if entry is None:
            raise ValueError(f"{path.name}: {m['path']} is not in schema/vss_mapping.yaml")
        m["unit"] = entry["unit"]
    return mapping


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("SET memory_limit='3GB'")
    return con


def reset_dir(path: Path):
    """Remove a previous adapter output. Refuses anything outside data/processed."""
    if PROCESSED.resolve() not in path.resolve().parents:
        raise ValueError(f"refusing to delete {path}")
    if path.exists():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)


def copy_partitioned(con: duckdb.DuckDBPyConnection, select_sql: str, out_dir: Path):
    """Write a query with a `date` column as date-partitioned Parquet (the column itself is not stored)."""
    reset_dir(out_dir)
    con.execute(f"COPY ({select_sql}) TO '{out_dir.as_posix()}' "
                f"(FORMAT parquet, COMPRESSION zstd, PARTITION_BY (date))")


def write_report(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
