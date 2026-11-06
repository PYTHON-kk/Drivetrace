"""Local history store over canonical Parquet (FR-MEM-02).

A *source* is one vehicle timeline, keyed by its directory name:
    data/sim/history/<run_id>/            -> key = run_id      (e.g. hist6w_s42)
    data/processed/<dataset>/<source_id>/ -> key = source_id   (e.g. ved_455, zenodo_can_vehicle)
Each source has signals/ and events/ (raw canonical) and, once built, data/derived/<key>/ (memory layers).
Queries always target one source; sources are never merged.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "drivetrace.yaml"


def load_config(path: Path = CONFIG) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class Source:
    key: str
    dataset: str
    source_type: str  # simulated | recorded
    root: Path
    timezone: str = "UTC"  # vehicle-local time zone, from <root>/source.json

    @property
    def signals_glob(self) -> str:
        return f"{self.root.as_posix()}/signals/*/*.parquet"

    @property
    def events_glob(self) -> str:
        return f"{self.root.as_posix()}/events/*/*.parquet"


def _timezone(d: Path) -> str:
    meta = d / "source.json"
    return json.loads(meta.read_text())["timezone"] if meta.exists() else "UTC"


def write_source_meta(root: Path, **meta):
    """Written by the simulator and adapters next to signals/ and events/."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "source.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def discover_sources(cfg: dict) -> dict[str, Source]:
    paths = cfg["paths"]
    found: dict[str, Source] = {}
    roots = [("sim", "simulated", ROOT / paths["sim_history"])]
    if cfg["store"].get("include_episodes"):
        roots.append(("sim_episodes", "simulated", ROOT / paths["sim_episodes"]))
    for dataset, kind, base in roots:
        for d in sorted(base.glob("*")) if base.exists() else []:
            if (d / "signals").exists():
                found[d.name] = Source(d.name, dataset, kind, d, _timezone(d))
    processed = ROOT / paths["processed"]
    for ds in sorted(processed.glob("*")) if processed.exists() else []:
        for d in sorted(ds.glob("*")):
            if (d / "signals").exists():
                found[d.name] = Source(d.name, ds.name, "recorded", d, _timezone(d))
    return found


class Store:
    """Owns one DuckDB connection with explicit resource limits."""

    def __init__(self, cfg: dict | None = None, derived_root: Path | None = None, include_episodes: bool | None = None):
        self.cfg = cfg or load_config()
        if include_episodes is not None:
            self.cfg = {**self.cfg, "store": {**self.cfg["store"], "include_episodes": include_episodes}}
        st = self.cfg["store"]
        self.derived_root = derived_root or ROOT / self.cfg["paths"]["derived"]
        # the assistant container mounts the store read-only, so DuckDB spills to a tmpfs instead
        tmp = Path(os.environ.get("DRIVETRACE_DUCKDB_TMP") or ROOT / st["temp_directory"])
        tmp.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect()
        self.con.execute(f"SET memory_limit='{st['duckdb_memory_limit']}'")
        self.con.execute(f"SET threads={int(st['duckdb_threads'])}")
        self.con.execute(f"SET temp_directory='{tmp.as_posix()}'")
        self.con.execute("SET TimeZone='UTC'")
        self.sources = discover_sources(self.cfg)

    def source(self, key: str) -> Source:
        if key not in self.sources:
            raise KeyError(f"unknown source {key!r}; known: {', '.join(self.sources)}")
        return self.sources[key]

    def derived(self, key: str) -> Path:
        return self.derived_root / key

    # Raw canonical relations -------------------------------------------------
    # `date` (UTC day) comes from the hive partition path; filtering on it skips whole files.
    def signals(self, key: str) -> str:
        return f"read_parquet('{self.source(key).signals_glob}', hive_partitioning = true)"

    def events(self, key: str) -> str:
        return f"read_parquet('{self.source(key).events_glob}', hive_partitioning = true)"

    # Derived relations (memory/build.py) --------------------------------------
    def aggregates(self, key: str, window: str = "1 hour") -> str:
        return f"read_parquet('{(self.derived(key) / window_dir(window)).as_posix()}/*/*.parquet')"

    def trips(self, key: str) -> str:
        return f"read_parquet('{(self.derived(key) / 'trips.parquet').as_posix()}')"

    def event_index(self, key: str) -> str:
        return f"read_parquet('{(self.derived(key) / 'event_index.parquet').as_posix()}')"

    def capsule_index(self, key: str) -> str:
        return f"read_parquet('{(self.derived(key) / 'capsule_index.parquet').as_posix()}')"

    def capsule(self, key: str, event_id: str) -> str:
        return f"read_parquet('{(self.derived(key) / 'capsules' / f'{event_id}.parquet').as_posix()}')"

    def query(self, sql: str, params: list | None = None) -> list[tuple]:
        return self.con.execute(sql, params or []).fetchall()


def window_dir(window: str) -> str:
    return "agg_" + window.replace(" ", "")
