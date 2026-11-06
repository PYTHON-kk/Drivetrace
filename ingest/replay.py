"""Replay a stored source as a live stream at 1x / Nx speed (FR-ING-03).

    python -m ingest.replay --source hist6w_s42 --start 2026-09-25T06:00:00Z --hours 1 --speed 10
    python -m ingest.replay --source ved_455 --days 7 --speed max

Data is read from the canonical store in slabs and delivered to sinks in 1-second (data time) batches,
paced against the wall clock. Nothing is dropped: if a sink is slow the replay falls behind and the
maximum lateness is reported instead.
"""

import argparse
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from memory.ring_buffer import RingBuffer
from memory.store import Store

SLAB = timedelta(hours=1)
DATE_PRUNE = "date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)"


class CountingSink:
    def __init__(self):
        self.rows = 0
        self.events = 0

    def signals(self, ts_ms, paths, values):
        self.rows += len(ts_ms)

    def on_events(self, rows):
        self.events += len(rows)


class RingBufferSink:
    def __init__(self, ring: RingBuffer):
        self.ring = ring

    def signals(self, ts_ms, paths, values):
        self.ring.append(ts_ms, paths, values)

    def on_events(self, rows):
        pass


class Replayer:
    def __init__(self, store: Store, key: str, sinks: list, speed: float = 1.0, batch_s: float = 1.0):
        self.store, self.key, self.sinks = store, key, sinks
        self.speed = speed  # float('inf') = as fast as possible
        self.batch_ms = int(batch_s * 1000)
        self.stats = {"rows": 0, "events": 0, "batches": 0, "max_lateness_s": 0.0, "wall_s": 0.0}

    def _slab(self, a: datetime, b: datetime):
        sig = self.store.con.execute(
            f"""SELECT epoch_ms(ts), signal_path, value FROM {self.store.signals(self.key)}
                WHERE {DATE_PRUNE} AND ts >= ? AND ts < ? ORDER BY ts, seq""", [a, b, a, b]).fetchnumpy()
        ev = self.store.query(
            f"""SELECT epoch_ms(ts), event_id, event_type, attrs FROM {self.store.events(self.key)}
                WHERE {DATE_PRUNE} AND ts >= ? AND ts < ? ORDER BY ts, seq""", [a, b, a, b])
        return sig, ev

    def run(self, start: datetime, end: datetime) -> dict:
        wall0 = time.perf_counter()
        data0 = int(start.timestamp() * 1000)
        a = start
        while a < end:
            b = min(a + SLAB, end)
            sig, ev = self._slab(a, b)
            ts = np.asarray(sig["epoch_ms(ts)"], dtype=np.int64)
            paths = np.asarray(sig["signal_path"], dtype=object)
            vals = np.asarray(sig["value"], dtype=np.float64)
            ev_ts = np.array([e[0] for e in ev], dtype=np.int64)
            lo = int(a.timestamp() * 1000)
            hi = int(b.timestamp() * 1000)
            for t in range(lo, hi, self.batch_ms):
                i0, i1 = np.searchsorted(ts, [t, t + self.batch_ms])
                j0, j1 = np.searchsorted(ev_ts, [t, t + self.batch_ms])
                if i1 == i0 and j1 == j0:
                    continue
                if self.speed != float("inf"):
                    due = wall0 + (t + self.batch_ms - data0) / 1000 / self.speed
                    now = time.perf_counter()
                    if due > now:
                        time.sleep(due - now)
                    else:
                        self.stats["max_lateness_s"] = max(self.stats["max_lateness_s"], now - due)
                for s in self.sinks:
                    if i1 > i0:
                        s.signals(ts[i0:i1], paths[i0:i1], vals[i0:i1])
                    if j1 > j0:
                        s.on_events(ev[j0:j1])
                self.stats["rows"] += int(i1 - i0)
                self.stats["events"] += int(j1 - j0)
                self.stats["batches"] += 1
            a = b
        if self.speed != float("inf"):  # a paced replay lasts the full scaled interval
            remaining = wall0 + (end - start).total_seconds() / self.speed - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        self.stats["wall_s"] = round(time.perf_counter() - wall0, 3)
        self.stats["max_lateness_s"] = round(self.stats["max_lateness_s"], 3)
        return self.stats


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--start", help="ISO time (UTC if no offset); default: first sample")
    ap.add_argument("--hours", type=float, default=0.0)
    ap.add_argument("--days", type=float, default=0.0)
    ap.add_argument("--speed", default="1", help="replay speed factor, or 'max'")
    args = ap.parse_args()
    store = Store()
    if args.start:
        start = parse_time(args.start)
    else:
        start = store.query(f"SELECT min(ts) FROM {store.signals(args.source)}")[0][0].astimezone(timezone.utc)
    end = start + timedelta(hours=args.hours, days=args.days or (0 if args.hours else 1))
    ring = RingBuffer(int(store.cfg["memory"]["ring_buffer_rows"]))
    count = CountingSink()
    speed = float("inf") if args.speed == "max" else float(args.speed)
    stats = Replayer(store, args.source, [count, RingBufferSink(ring)], speed=speed).run(start, end)
    expected = store.query(f"SELECT count(*) FROM {store.signals(args.source)} WHERE ts >= ? AND ts < ?", [start, end])[0][0]
    print(f"replayed {stats['rows']:,} of {expected:,} signal rows and {stats['events']} events "
          f"from {start:%Y-%m-%d %H:%M} UTC for {end - start} at speed {args.speed}")
    print(f"wall time {stats['wall_s']} s, max lateness {stats['max_lateness_s']} s, "
          f"ring buffer {ring.size:,}/{ring.capacity:,} rows ({ring.nbytes / 1e6:.1f} MB), overwritten {ring.overwritten:,}")


if __name__ == "__main__":
    main()
