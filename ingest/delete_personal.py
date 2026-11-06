"""Delete driver-related personal data from one vehicle source (NFR-08). Runs in the INGEST process,
which is the only process with write access to the store; the assistant cannot call it.

    python -m ingest.delete_personal --source zenodo_can_vehicle --dry-run
    python -m ingest.delete_personal --source zenodo_can_vehicle [--before 2018-11-15]

Removes every location signal row (tags in schema/vss_mapping.yaml) and identity keys from event
attributes, then rebuilds the derived memory layers (aggregates, capsules and the event index contain
copies). Each run appends a line to <source>/deletion_log.jsonl with counts only (no personal data).
"""

import argparse
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from memory.build import build_source
from memory.store import Store
from security.privacy import DENIED_SIGNALS, scrub_attrs


def _partition_date(f: Path) -> date | None:
    for part in f.parts:
        if part.startswith("date="):
            return date.fromisoformat(part[5:])
    return None


def _rewrite(f: Path, table: pa.Table):
    tmp = f.with_suffix(".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, f)  # atomic on the same file system


def delete(store: Store, key: str, before: date | None = None, dry_run: bool = False) -> dict:
    src = store.source(key)
    counts = {"signal_rows_removed": 0, "event_attrs_scrubbed": 0, "files_rewritten": 0}
    denied = pa.array(sorted(DENIED_SIGNALS))
    for f in sorted((src.root / "signals").rglob("*.parquet")):
        d = _partition_date(f)
        if before and d and d >= before:
            continue
        t = pq.read_table(f)
        keep = pc.invert(pc.is_in(pc.cast(t["signal_path"], pa.string()), value_set=denied))
        removed = t.num_rows - pc.sum(pc.cast(keep, pa.int64())).as_py()
        if removed:
            counts["signal_rows_removed"] += removed
            counts["files_rewritten"] += 1
            if not dry_run:
                _rewrite(f, t.filter(keep))
    for f in sorted((src.root / "events").rglob("*.parquet")):
        d = _partition_date(f)
        if before and d and d >= before:
            continue
        t = pq.read_table(f)
        attrs = t["attrs"].to_pylist()
        cleaned = [json.dumps(scrub_attrs(json.loads(a or "{}")), sort_keys=True) for a in attrs]
        changed = sum(a != c for a, c in zip(attrs, cleaned) if a)
        if changed:
            counts["event_attrs_scrubbed"] += changed
            counts["files_rewritten"] += 1
            if not dry_run:
                i = t.schema.get_field_index("attrs")
                _rewrite(f, t.set_column(i, "attrs", pa.array(cleaned, type=t.schema.field("attrs").type)))
    if not dry_run:
        build_source(store, key)  # aggregates, capsules and the event index held copies
        counts["derived_rebuilt"] = True
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": key,
                 "before": before.isoformat() if before else None, **counts}
        with open(src.root / "deletion_log.jsonl", "a", encoding="utf-8") as log:
            log.write(json.dumps(entry) + "\n")
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--before", type=date.fromisoformat, help="only partitions before this UTC date")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    counts = delete(Store(), args.source, args.before, args.dry_run)
    print(("would remove: " if args.dry_run else "removed: ") + json.dumps(counts))


if __name__ == "__main__":
    main()
