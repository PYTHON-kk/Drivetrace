"""Report header for every P7 result (build plan §2.9, NFR-09): what ran, where, with which models and seeds."""

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]


@lru_cache
def _sha256_prefix(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()[:16]


def header() -> dict:
    import duckdb
    import yaml

    llama = ROOT / "runtime" / "llama-b11606" / "llama-server.exe"
    version = None
    if llama.exists():
        try:
            version = subprocess.run([str(llama), "--version"], capture_output=True, text=True, timeout=30).stderr.strip().splitlines()[:2]
        except (OSError, subprocess.SubprocessError):
            pass
    scen = yaml.safe_load((ROOT / "sim" / "scenarios.yaml").read_text(encoding="utf-8"))
    vss = yaml.safe_load((ROOT / "schema" / "vss_mapping.yaml").read_text(encoding="utf-8"))["vss_release"]
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "device": {"cpu": platform.processor(), "machine": platform.machine(), "os": platform.platform(),
                   "logical_cpus": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
                   "note": "x86-64 development laptop used as a PROXY, not the 2-core ARM target"},
        "python": sys.version.split()[0], "duckdb": duckdb.__version__,
        "llama_cpp": {"build": "b11606 win-cpu-x64", "version": version},
        "models": {m.name: _sha256_prefix(m) for m in sorted((ROOT / "models").glob("*.gguf"))},
        "threads": 2, "duckdb_limits": "2 threads, 512 MB (config/drivetrace.yaml)",
        "vss_release": vss,
        "seeds": {"history": scen["history"]["seed"], "episodes": "10000 + 97*seed + index (sim/generate.py)",
                  "splits": json.loads((ROOT / "data" / "sim" / "labels" / "splits.json").read_text())},
    }


if __name__ == "__main__":
    print(json.dumps(header(), indent=2))
