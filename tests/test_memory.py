import json
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ingest.replay import CountingSink, Replayer, RingBufferSink
from memory.build import build_source
from memory.ring_buffer import RingBuffer
from memory.store import ROOT, Store

KEY = "hist6w_s42"
LABELS = ROOT / "data" / "sim" / "labels" / f"history_{KEY}.json"
AVG = "Vehicle.Powertrain.TractionBattery.Temperature.Average"


# ---------------------------------------------------------------- ring buffer
def test_ring_buffer_is_bounded_and_keeps_newest_in_order():
    rb = RingBuffer(5)
    size0 = rb.nbytes
    rb.append([1, 2, 3], ["a", "b", "a"], [1.0, 2.0, 3.0])
    rb.append([4, 5, 6, 7], ["a", "b", "a", "b"], [4.0, 5.0, 6.0, 7.0])
    ts, paths, vals = rb.window()
    assert ts.tolist() == [3, 4, 5, 6, 7]
    assert paths == ["a", "a", "b", "a", "b"]
    assert rb.latest("a") == (6, 6.0) and rb.latest("zzz") is None
    assert rb.overwritten == 2 and rb.nbytes == size0


def test_ring_buffer_batch_larger_than_capacity():
    rb = RingBuffer(3)
    rb.append(np.arange(10), ["x"] * 10, np.arange(10.0))
    assert rb.window()[0].tolist() == [7, 8, 9]
    assert rb.window(since_ms=9)[2].tolist() == [9.0]


# ---------------------------------------------------------------- built memory layers
@pytest.fixture(scope="module")
def built(tmp_path_factory):
    store = Store(derived_root=tmp_path_factory.mktemp("derived"))
    if KEY not in store.sources or not LABELS.exists():
        pytest.skip("synthetic history not generated (python -m sim.generate history)")
    report = build_source(store, KEY)
    return store, report, json.loads(LABELS.read_text())


def test_hourly_aggregates_match_pandas(built):
    store, _, _ = built
    hour = pd.Timestamp("2026-09-25 08:00", tz="UTC")
    raw = store.con.execute(f"""SELECT value FROM {store.signals(KEY)}
                                WHERE signal_path = '{AVG}' AND ts >= ? AND ts < ?""",
                            [hour.to_pydatetime(), (hour + timedelta(hours=1)).to_pydatetime()]).df().value
    agg = store.con.execute(f"""SELECT n, min, max, mean, p50 FROM {store.aggregates(KEY)}
                                WHERE signal_path = '{AVG}' AND window_start = ?""", [hour.to_pydatetime()]).fetchone()
    assert agg[0] == len(raw) > 0
    assert agg[1] == pytest.approx(raw.min()) and agg[2] == pytest.approx(raw.max())
    assert agg[3] == pytest.approx(raw.mean()) and agg[4] == pytest.approx(raw.median())


def test_trips_match_ground_truth(built):
    store, report, labels = built
    trips = store.con.execute(f"SELECT km FROM {store.trips(KEY)} ORDER BY trip_no").df().km
    truth = pd.Series([t["distance_km"] for t in labels["trips"]])
    assert report["trips"] == len(labels["trips"])
    assert np.allclose(trips.to_numpy(), truth.to_numpy(), rtol=0.01, atol=0.05)


def test_event_index_and_capsules_are_complete(built):
    store, report, labels = built
    raw_events = store.query(f"SELECT count(*) FROM {store.events(KEY)}")[0][0]
    assert report["events"] == raw_events
    assert report["capsules"] == len(labels["warnings"]) == 6
    for (event_id,) in store.query(f"SELECT event_id FROM {store.capsule_index(KEY)}"):
        assert store.query(f"SELECT count(*) FROM {store.capsule(KEY, event_id)}")[0][0] > 0


def test_every_record_has_provenance(built):
    store, _, _ = built
    nulls, dup = store.query(f"""SELECT count(*) FILTER (WHERE source_id IS NULL OR seq IS NULL OR source_type IS NULL),
                                        count(*) - count(DISTINCT seq) FROM {store.signals(KEY)}""")[0]
    assert nulls == 0 and dup == 0


# ---------------------------------------------------------------- P1 exit: Q5 and Q6 answered with SQL
def test_q6_overheating_warnings_this_month(built):
    store, _, labels = built
    tz = store.source(KEY).timezone
    (now,) = store.query(f"SELECT max(ts) FROM {store.signals(KEY)}")[0]  # vehicle clock
    month = now.astimezone(timezone.utc).strftime("%Y-%m")
    (n,) = store.query(f"""SELECT count(*) FROM {store.event_index(KEY)}
                           WHERE event_type = 'BATT_OVERTEMP_WARNING_SET'
                             AND strftime(timezone('{tz}', ts), '%Y-%m') = '{month}'""")[0]
    assert n == sum(w["ts_utc"].startswith(month) for w in labels["warnings"]) == 5


def test_q5_battery_temperature_at_last_warning(built):
    store, _, _ = built
    event_id, ts, attrs = store.query(f"""SELECT event_id, ts, attrs FROM {store.event_index(KEY)}
                                          WHERE event_type = 'BATT_OVERTEMP_WARNING_SET' ORDER BY ts DESC LIMIT 1""")[0]
    (temp,) = store.query(f"""SELECT arg_max(value, ts) FROM {store.capsule(KEY, event_id)}
                              WHERE signal_path = '{AVG}' AND ts <= ?""", [ts])[0]
    assert temp == pytest.approx(json.loads(attrs)["measured_avg_c"], abs=1.0)


# ---------------------------------------------------------------- replay
def test_replay_delivers_every_row_and_ring_stays_bounded(built):
    store, _, _ = built
    start = pd.Timestamp("2026-09-25 06:00", tz="UTC").to_pydatetime()
    end = start + timedelta(hours=1)
    ring, count = RingBuffer(50_000), CountingSink()
    stats = Replayer(store, KEY, [count, RingBufferSink(ring)], speed=float("inf")).run(start, end)
    (expected,) = store.query(f"SELECT count(*) FROM {store.signals(KEY)} WHERE ts >= ? AND ts < ?", [start, end])[0]
    assert stats["rows"] == count.rows == expected > 0
    assert ring.size == min(expected, ring.capacity)


def test_replay_is_paced_against_the_wall_clock(built):
    store, _, _ = built
    start = pd.Timestamp("2026-09-25 07:00", tz="UTC").to_pydatetime()
    stats = Replayer(store, KEY, [CountingSink()], speed=240).run(start, start + timedelta(minutes=2))
    assert 0.3 < stats["wall_s"] < 1.5  # 120 s of data at 240x is ~0.5 s
    assert stats["max_lateness_s"] < 0.25
