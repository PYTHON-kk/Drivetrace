"""Experiment D - resources: storage, peak memory, latency under concurrent ingest (solution_journey §9.3).

    python -m bench.experiment_d

Phases (the real assistant server, 127.0.0.1, sampled every 100 ms):
  A  idle -> query load: every test-set question x 3 vehicles x 3 roles over HTTP
  B  the same load while an ingest-style replay streams 7 days of data at full speed in another process
     (the replay reads the store and fills a ring buffer; it does not write, to avoid racing the reader)
  C  the same load plus llama-server (SmolLM2) answering ECD choice requests in parallel
Reports peak memory (resident set / Windows working set) of every process and of the whole stack.
"""

import json
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

import psutil

from bench.env import header
from memory.store import ROOT, Store
from tests.query_set import load

OUT = ROOT / "bench" / "results" / "experiment_d.json"
PORT, SLM_PORT = 8766, 8089
SOURCES = ["hist6w_s42", "ved_455", "zenodo_can_vehicle"]
PY = sys.executable


def dir_mb(p) -> float:
    return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6, 2)


def storage() -> dict:
    store = Store()
    out = {}
    for key, s in store.sources.items():
        days = store.query(f"SELECT count(DISTINCT date) FROM {store.signals(key)}")[0][0]
        raw, derived = dir_mb(s.root), dir_mb(store.derived(key))
        out[key] = {"days_with_data": days, "raw_mb": raw, "derived_mb": derived,
                    "mb_per_day": round((raw + derived) / max(1, days), 3),
                    "six_weeks_mb": round((raw + derived) / max(1, days) * 42, 1)}
    return out


class Sampler:
    def __init__(self, procs: dict):
        self.procs, self.peak, self.stop = procs, {k: 0 for k in procs}, threading.Event()
        self.peak_total = 0
        self.t = threading.Thread(target=self.run, daemon=True)

    def rss(self, p: psutil.Process) -> int:
        try:  # current resident set (working set on Windows) incl. child processes; max taken per phase
            return p.memory_info().rss + sum(c.memory_info().rss for c in p.children(recursive=True))
        except psutil.Error:
            return 0

    def run(self):
        while not self.stop.is_set():
            total = 0
            for k, p in self.procs.items():
                r = self.rss(p)
                self.peak[k] = max(self.peak[k], r)
                total += r
            self.peak_total = max(self.peak_total, total)
            time.sleep(0.1)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.t.join()


def wait_http(url: str, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except OSError:
            time.sleep(0.3)
    raise TimeoutError(url)


def ask(q: str, source: str, role: str) -> tuple[float, dict]:
    body = json.dumps({"question": q, "source": source, "role": role}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/ask", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        a = json.load(r)
    return (time.perf_counter() - t0) * 1000, a


def load_run() -> dict:
    items = load()
    lat = {"common": [], "diagnostic": [], "refusal": []}
    for src in SOURCES:
        for it in items:
            for role in ("driver", "service", "fleet"):
                ms, _ = ask(it["q"], src, role)
                cls = {"diagnose_event": "diagnostic", "unsupported": "refusal"}.get(it["intent"], "common")
                lat[cls].append(ms)

    def stats(xs):
        xs = sorted(xs)
        return {"n": len(xs), "p50_ms": round(statistics.median(xs), 1), "p95_ms": round(xs[int(0.95 * (len(xs) - 1))], 1),
                "max_ms": round(xs[-1], 1)}
    return {k: stats(v) for k, v in lat.items()}


def ecd_load(stop: threading.Event):
    """Keep llama-server busy with ECD choice requests built from real diagnosis records."""
    from diagnosis.engine import Diagnoser
    from ecd.answer import slm_choice
    from ecd.compiler import compile_record

    store = Store()
    d = Diagnoser(store)
    ids = [r[0] for r in store.query(f"SELECT event_id FROM {store.event_index('hist6w_s42')} "
                                      "WHERE event_type = 'BATT_OVERTEMP_WARNING_SET'")]
    recs = [d.diagnose_event("hist6w_s42", e) for e in ids]
    n = 0
    while not stop.is_set():
        rec = recs[n % len(recs)]
        slm_choice(rec, compile_record(rec, "service"), "What happened before the battery overheating warning?", port=SLM_PORT)
        n += 1


def main():
    results = {"header": header(), "storage": storage(), "phases": {}}
    server = subprocess.Popen([PY, "-m", "uvicorn", "ui.app:app", "--host", "127.0.0.1", "--port", str(PORT)],
                              cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_http(f"http://127.0.0.1:{PORT}/")
        sp = psutil.Process(server.pid)
        idle = sp.memory_info().rss
        results["assistant_idle_rss_mb"] = round(idle / 1e6, 1)

        with Sampler({"assistant": sp}) as s:
            results["phases"]["A_queries_only"] = {"latency": load_run(), "peak_mb": {k: round(v / 1e6) for k, v in s.peak.items()}}
        print("A done", results["phases"]["A_queries_only"])
        save(results)

        replay = subprocess.Popen([PY, "-m", "bench.replay_loop"], cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2)
            rp = psutil.Process(replay.pid)
            with Sampler({"assistant": sp, "ingest_replay": rp}) as s:
                lat = load_run()
            results["phases"]["B_queries_with_ingest"] = {"latency": lat, "peak_mb": {k: round(v / 1e6) for k, v in s.peak.items()}}
        finally:
            replay.kill()
        print("B done", results["phases"]["B_queries_with_ingest"])
        save(results)

        from query.planner import LocalServer
        with LocalServer(ROOT / "models" / "SmolLM2-360M-Instruct-Q4_K_M.gguf", port=SLM_PORT) as slm:
            stop = threading.Event()
            t = threading.Thread(target=ecd_load, args=(stop,), daemon=True)
            t.start()
            time.sleep(3)
            with Sampler({"assistant": sp, "llama_server": psutil.Process(slm.proc.pid)}) as s:
                lat = load_run()
            stop.set()
            t.join(timeout=60)
            results["phases"]["C_queries_with_slm"] = {"latency": lat, "peak_mb": {k: round(v / 1e6) for k, v in s.peak.items()},
                                                        "peak_total_mb": round(s.peak_total / 1e6)}
        print("C done", results["phases"]["C_queries_with_slm"])
    finally:
        server.terminate()
        server.wait(timeout=30)
    save(results)


def save(results: dict):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
