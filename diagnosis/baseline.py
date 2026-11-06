"""Matched healthy-baseline selection (FR-ANA-02; solution_journey Defect 7).

Baselines are earlier sessions of the same kind, matched on declared operating conditions, compared over
the same offset into the session as the target window. "Healthy" is an assumption: earlier sessions are
not known to be fault-free, which is why at least two baselines are preferred and the match is recorded.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from diagnosis.window import AMBIENT, T_AVG, Session

# Declared matching features and tolerances. A candidate is "matched" when every available feature is
# within tolerance; "relaxed" when within twice the tolerance; otherwise the nearest candidates are kept as
# "unmatched" so the conditions can still be compared, and the gate treats them as not comparable.
# Sessions that raised a warning or a derate are never baselines (they are not presumed healthy).
TOLERANCES = {
    "charge_dc": {"power_kw": 30.0, "soc_start": 15.0, "ambient": 6.0, "temp_start": 6.0},
    "charge_ac": {"power_kw": 3.0, "soc_start": 20.0, "ambient": 6.0, "temp_start": 6.0},
    "trip": {"ambient": 6.0, "temp_start": 6.0},
    "park": {"ambient": 6.0, "temp_start": 6.0},
}
MAX_BASELINES = 3
MIN_OVERLAP = 0.5


@dataclass
class Candidate:
    session: Session
    start: datetime
    end: datetime
    features: dict
    distance: float
    status: str  # matched | relaxed | unmatched


def start_conditions(store, key, starts: list[datetime]) -> dict[datetime, dict]:
    """Ambient and battery temperature in the first minutes of many sessions, in one query."""
    import pandas as pd

    if not starts:
        return {}
    store.con.register("_starts", pd.DataFrame({"t0": starts}))
    rows = store.query(f"""
        SELECT s.t0, median(x.value) FILTER (WHERE x.signal_path = '{AMBIENT}'),
               arg_min(x.value, x.ts) FILTER (WHERE x.signal_path = '{T_AVG}')
        FROM _starts s JOIN {store.signals(key)} x
          ON x.ts BETWEEN s.t0 - INTERVAL 2 MINUTE AND s.t0 + INTERVAL 5 MINUTE
         AND x.signal_path IN ('{AMBIENT}', '{T_AVG}')
        GROUP BY s.t0""")
    store.con.unregister("_starts")
    return {t0: {"ambient": amb, "temp_start": tmp} for t0, amb, tmp in rows}


def _conditions(s: Session, start_cond: dict) -> dict:
    c = dict(start_cond)
    if s.kind.startswith("charge"):
        c["power_kw"] = s.attrs.get("requested_power_kw")
        c["soc_start"] = s.attrs.get("soc_start_pct")
    return {k: v for k, v in c.items() if v is not None}


def match(store, key: str, all_sessions: list[Session], target: Session, w_start: datetime, w_end: datetime,
          unhealthy: list[datetime] = (), cache: dict | None = None) -> dict:
    """`cache` (per source) keeps start conditions between calls."""
    cache = {} if cache is None else cache
    tol = TOLERANCES[target.kind]
    off0, off1 = w_start - target.start, w_end - target.start
    cands: list[Candidate] = []
    if target.kind == "park":
        pool = [Session("park", target.start - timedelta(days=k), target.end - timedelta(days=k), f"parked -{k}d", {})
                for k in range(1, 8)]
        busy = [(s.start, s.end) for s in all_sessions]
        pool = [p for p in pool if not any(a < w_end - (target.start - p.start) and b > w_start - (target.start - p.start)
                                           for a, b in busy)]
    else:
        pool = [s for s in all_sessions if s.kind == target.kind and s.end <= target.start
                and not any(s.start <= t <= s.end for t in unhealthy)]
    pool = [s for s in pool if (min(s.start + off1, s.end) - (s.start + off0)) >= MIN_OVERLAP * (w_end - w_start)]
    missing = [t for t in {s.start for s in pool} | {target.start} if t not in cache]
    for t in missing:
        cache[t] = {}
    cache.update(start_conditions(store, key, missing))
    tgt = _conditions(target, cache[target.start])
    for s in pool:
        a, b = s.start + off0, min(s.start + off1, s.end)
        feats = _conditions(s, cache[s.start])
        shared = [k for k in tol if k in tgt and k in feats]
        if not shared:
            continue
        ratios = [abs(feats[k] - tgt[k]) / tol[k] for k in shared]
        status = "matched" if max(ratios) <= 1 else "relaxed" if max(ratios) <= 2 else "unmatched"
        cands.append(Candidate(s, a, b, feats, sum(ratios) / len(ratios), status))
    best = ([c for c in cands if c.status == "matched"] or [c for c in cands if c.status == "relaxed"]
            or [c for c in cands if c.status == "unmatched"])
    best = sorted(best, key=lambda c: c.distance)[:MAX_BASELINES]
    return {
        "status": best[0].status if best else "none",
        "criteria": {"kind": target.kind, "tolerances": tol, "target_conditions": tgt},
        "windows": [{"ref": c.session.ref, "start": c.start.isoformat(), "end": c.end.isoformat(),
                     "conditions": c.features, "distance": round(c.distance, 3), "status": c.status} for c in best],
        "_windows": [(c.start, c.end) for c in best],
    }
