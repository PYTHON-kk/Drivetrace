"""Ingest-style background load for Experiment D: replay 7 days of a source at full speed, forever."""

from datetime import timedelta, timezone

from ingest.replay import CountingSink, Replayer, RingBufferSink
from memory.ring_buffer import RingBuffer
from memory.store import Store


def main(key: str = "hist6w_s42"):
    store = Store()
    start = store.query(f"SELECT min(ts) FROM {store.signals(key)}")[0][0].astimezone(timezone.utc)
    ring = RingBuffer(int(store.cfg["memory"]["ring_buffer_rows"]))
    while True:
        Replayer(store, key, [CountingSink(), RingBufferSink(ring)], speed=float("inf")).run(start, start + timedelta(days=7))


if __name__ == "__main__":
    main()
