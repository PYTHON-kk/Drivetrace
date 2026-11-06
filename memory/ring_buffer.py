"""Bounded in-process hot buffer for recent samples (FR-MEM-01).

Preallocated numpy arrays of (timestamp ms, signal id, value): memory use is fixed at construction
(18 bytes per row plus the signal-name table) no matter how much data streams through.
When full, the oldest rows are overwritten.
"""

import numpy as np


class RingBuffer:
    ROW_BYTES = 8 + 2 + 8  # int64 ts, int16 signal id, float64 value

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.ts = np.zeros(capacity, dtype=np.int64)
        self.sig = np.zeros(capacity, dtype=np.int16)
        self.val = np.zeros(capacity, dtype=np.float64)
        self.head = 0  # next write position
        self.size = 0
        self.total_appended = 0
        self.ids: dict[str, int] = {}
        self.paths: list[str] = []

    @property
    def nbytes(self) -> int:
        return self.ts.nbytes + self.sig.nbytes + self.val.nbytes

    @property
    def overwritten(self) -> int:
        return self.total_appended - self.size

    def signal_id(self, path: str) -> int:
        i = self.ids.get(path)
        if i is None:
            if len(self.paths) >= np.iinfo(np.int16).max:
                raise OverflowError("too many distinct signals")
            i = self.ids[path] = len(self.paths)
            self.paths.append(path)
        return i

    def append(self, ts_ms: np.ndarray, paths, values: np.ndarray):
        ts_ms = np.asarray(ts_ms, dtype=np.int64)
        values = np.asarray(values, dtype=np.float64)
        ids = np.fromiter((self.signal_id(p) for p in paths), dtype=np.int16, count=len(ts_ms))
        n = len(ts_ms)
        self.total_appended += n
        if n >= self.capacity:  # only the newest `capacity` rows survive
            ts_ms, ids, values = ts_ms[-self.capacity:], ids[-self.capacity:], values[-self.capacity:]
            n = self.capacity
        pos = (self.head + np.arange(n)) % self.capacity
        self.ts[pos], self.sig[pos], self.val[pos] = ts_ms, ids, values
        self.head = (self.head + n) % self.capacity
        self.size = min(self.capacity, self.size + n)

    def _order(self) -> np.ndarray:
        """Indices of valid rows, oldest first."""
        start = (self.head - self.size) % self.capacity
        return (start + np.arange(self.size)) % self.capacity

    def window(self, since_ms: int | None = None, path: str | None = None):
        """Rows (oldest first) as (ts_ms, signal_paths, values), optionally filtered."""
        idx = self._order()
        mask = np.ones(len(idx), dtype=bool)
        if since_ms is not None:
            mask &= self.ts[idx] >= since_ms
        if path is not None:
            if path not in self.ids:
                return np.array([], dtype=np.int64), [], np.array([])
            mask &= self.sig[idx] == self.ids[path]
        idx = idx[mask]
        return self.ts[idx], [self.paths[i] for i in self.sig[idx]], self.val[idx]

    def latest(self, path: str) -> tuple[int, float] | None:
        ts, _, v = self.window(path=path)
        return (int(ts[-1]), float(v[-1])) if len(ts) else None
