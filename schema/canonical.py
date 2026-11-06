"""Canonical record schemas (solution_journey §8.3) and the signal catalogue loader.

Signals are stored long-format: one row per (timestamp, signal) sample.
`provenance_ref` is not stored per row; it is derived as f"{source_id}#{seq}".
`scenario_id` identifies the generation run, never the injected fault, so labels do not leak.
"""

from pathlib import Path

import pyarrow as pa
import yaml

MAPPING_PATH = Path(__file__).with_name("vss_mapping.yaml")

SIGNAL_SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("ms", tz="UTC")),
        ("signal_path", pa.dictionary(pa.int32(), pa.string())),
        ("value", pa.float64()),
        ("unit", pa.dictionary(pa.int32(), pa.string())),
        ("quality", pa.dictionary(pa.int32(), pa.string())),
        ("source_id", pa.dictionary(pa.int32(), pa.string())),
        ("scenario_id", pa.dictionary(pa.int32(), pa.string())),
        ("source_type", pa.dictionary(pa.int32(), pa.string())),
        ("seq", pa.int64()),
    ]
)

EVENT_SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("ms", tz="UTC")),
        ("event_id", pa.string()),
        ("event_type", pa.string()),
        ("subsystem", pa.string()),
        ("severity", pa.string()),
        ("code", pa.string()),
        ("attrs", pa.string()),  # JSON object
        ("source_id", pa.string()),
        ("scenario_id", pa.string()),
        ("source_type", pa.string()),
        ("seq", pa.int64()),
    ]
)


def load_catalogue(path: Path = MAPPING_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
