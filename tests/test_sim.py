import json

import duckdb
import numpy as np
import pytest

import sim.generate as g
from sim.vehicle import Faults, VehicleParams, VehicleSim, VehicleState


def dc_charge(faults: Faults, ambient=16.0, power=100e3, soc=0.4, t0=26.0):
    sim = VehicleSim(VehicleParams(), VehicleState(soc=soc, temp_c=t0), np.random.default_rng(0))
    sim.f = faults
    peak, events = 0.0, []
    for _ in range(540):
        if sim.s.soc >= 0.8:
            break
        r = sim.step(10, "charge_dc", ambient, charge_power_w=power)
        peak = max(peak, sim.s.temp_c)
        events += [e[0] for e in r.events]
    return peak, events


def test_healthy_dc_charge_stays_cool():
    peak, events = dc_charge(Faults())
    assert peak < 35
    assert "BATT_OVERTEMP_WARNING_SET" not in events


def test_degraded_cooling_overheats_in_warm_weather():
    _, events = dc_charge(Faults(cooling_effectiveness=0.1, pump_speed_factor=0.6), ambient=24)
    assert "BATT_OVERTEMP_WARNING_SET" in events


def test_sensor_bias_raises_false_warning_without_real_heating():
    peak, events = dc_charge(Faults(temp_avg_bias_c=24), ambient=12, t0=15)
    assert peak < 30
    assert "BATT_OVERTEMP_WARNING_SET" in events


def test_missing_pump_signal_is_not_emitted():
    sim = VehicleSim(VehicleParams(), VehicleState(), np.random.default_rng(0))
    sim.f = Faults(pump_signal_missing=True)
    r = sim.step(1, "drive", 15, v=20, a=0)
    assert "Vehicle.VendorExt.TractionBattery.Cooling.PumpSpeed" not in r.measured


@pytest.fixture(scope="module")
def episode(tmp_path_factory):
    out = tmp_path_factory.mktemp("sim")
    g.DATA = out
    cfg = g.load_config()
    r1 = g.generate_episode(cfg, "S3", 0, 0)
    return out, r1, cfg


def test_episode_is_deterministic(episode):
    out, r1, cfg = episode
    first = duckdb.sql(f"SELECT count(*), sum(value) FROM read_parquet('{(out / 'episodes' / r1['run_id']).as_posix()}/signals/*/*.parquet')").fetchall()[0]
    r2 = g.generate_episode(cfg, "S3", 0, 0)
    second = duckdb.sql(f"SELECT count(*), sum(value) FROM read_parquet('{(out / 'episodes' / r2['run_id']).as_posix()}/signals/*/*.parquet')").fetchall()[0]
    assert first[0] == second[0]
    assert first[1] == pytest.approx(second[1])


def test_labels_are_separate_and_scenario_id_does_not_leak(episode):
    out, r1, _ = episode
    labels = json.loads((out / "labels" / "episodes" / f"{r1['run_id']}.json").read_text())
    assert labels["scenario"] == "S3" and labels["warnings"]
    ids = duckdb.sql(f"SELECT DISTINCT scenario_id, source_type FROM read_parquet('{(out / 'episodes' / r1['run_id']).as_posix()}/signals/*/*.parquet')").fetchall()
    assert ids == [(r1["run_id"], "simulated")]
