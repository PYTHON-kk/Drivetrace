import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from diagnosis import counterfactual
from diagnosis.engine import Diagnoser, load_rules
from diagnosis.hypotheses import holds
from diagnosis.record import LEVELS, RECORD_VERSION
from diagnosis.signature import delta_signature, gate
from diagnosis.window import CORE, PUMP_CMD, PUMP_RPM, T_AVG, T_MAX, features
from memory.store import ROOT, Store

LABELS = ROOT / "data" / "sim" / "labels"
T0 = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)


def series(values, step_s=10, start=T0):
    return pd.DataFrame({"ts": pd.to_datetime([start + timedelta(seconds=i * step_s) for i in range(len(values))], utc=True),
                         "value": np.asarray(values, dtype=float)})


def window(**overrides):
    n = 60  # 10 minutes at 10 s
    w = {p: series(np.full(n, 20.0) + np.random.default_rng(i).normal(0, 0.2, n)) for i, p in enumerate(CORE)}
    w[PUMP_CMD] = series(np.ones(n))
    w[PUMP_RPM] = series(3200 + np.random.default_rng(9).normal(0, 15, n))
    w[T_MAX] = series(w[T_AVG]["value"] + 2.0)
    w.update({k: series(v) for k, v in overrides.items()})
    return w, T0, T0 + timedelta(minutes=10)


# ---------------------------------------------------------------- unit level
def test_max_below_average_is_flagged_as_contradiction():
    w, a, b = window()
    w[T_MAX] = series(w[T_AVG]["value"] - 5)
    f = features(w, a, b)
    g = gate(f, delta_signature(f, [f], load_rules()["scale_floor"]),
             {"status": "matched", "windows": [{}]}, load_rules(), warning_present=True, kind="charge_dc")
    assert "max_below_avg" in g["flags"]


def test_frozen_pump_speed_is_stale_but_a_resting_pump_is_not():
    rules = load_rules()
    w, a, b = window()
    w[PUMP_RPM] = series(np.full(60, 2450.0))  # identical non-zero value for 10 minutes: frozen
    f = features(w, a, b)
    g = gate(f, {}, {"status": "matched", "windows": [{}]}, rules, False, "charge_dc")
    assert "pump_speed_stale" in g["flags"]
    w, a, b = window()
    w[PUMP_CMD], w[PUMP_RPM] = series(np.zeros(60)), series(np.zeros(60))  # not commanded, 0 rpm: normal
    f = features(w, a, b)
    g = gate(f, {}, {"status": "matched", "windows": [{}]}, rules, False, "trip")
    assert "pump_speed_stale" not in g["flags"] and "pump_off_not_commanded" in g["flags"]


def test_robust_z_uses_declared_floor_and_skips_undeclared_features():
    tgt = {"derived": {"rise_rate": 0.5, "mystery": 5.0}}
    base = [{"derived": {"rise_rate": 0.1, "mystery": 1.0}}, {"derived": {"rise_rate": 0.1, "mystery": 1.0}}]
    s = delta_signature(tgt, base, {"rise_rate": 0.08})
    assert s["rise_rate"]["z"] == pytest.approx(0.4 / 0.08)
    assert "z" not in s["mystery"]


def test_rule_conditions():
    sig = {"load_i2": {"rel": 0.6, "z": 3.0}}
    assert holds("load_i2.rel > 0.5", sig, set()) is True
    assert holds("pump_speed_cmd.rel < -0.15", sig, set()) is None  # feature unavailable
    assert holds("flag:max_below_avg", sig, {"max_below_avg"}) is True
    with pytest.raises(ValueError):
        holds("load_i2.rel >> 1", sig, set())


def test_physical_model_fit_recovers_effectiveness():
    from sim.vehicle import VehicleParams

    n = 120
    m = counterfactual.ModelInputs(i_dis=np.full(n, 260.0), soc=np.full(n, 50.0), amb=np.full(n, 20.0),
                                   sink=np.full(n, 20.0), t_avg=np.zeros(n), flow_obs=np.ones(n),
                                   flow_healthy=np.ones(n), params=VehicleParams())
    m.t_avg = np.full(n, 25.0)
    m.t_avg = counterfactual.simulate(m, 0.3)
    fit = counterfactual.fit(m, [0.05, 1.5, 0.05])
    assert fit["effectiveness"] == pytest.approx(0.3, abs=0.051) and fit["usable"]


def test_diagnosis_code_never_reads_labels():
    import re

    ref = re.compile(r"labels[/\\\"']|\bLABELS\b|splits\.json|scenarios\.yaml")  # a path or constant, not prose
    for f in (ROOT / "diagnosis").glob("*.py"):
        assert not ref.search(f.read_text(encoding="utf-8")), f.name


# ---------------------------------------------------------------- scenario level (test split only)
@pytest.fixture(scope="module")
def results():
    if not (LABELS / "splits.json").exists():
        pytest.skip("run python -m sim.generate episodes && python -m memory.build --episodes")
    from bench import diagnosis_eval as ev

    store = Store(include_episodes=True)
    if not (store.derived("ep_S0_04") / "trips.parquet").exists():
        pytest.skip("run python -m memory.build --episodes")
    d = Diagnoser(store)
    test_runs = json.loads((LABELS / "splits.json").read_text())["test"]
    out = []
    for run in test_runs:
        lab = json.loads((LABELS / "episodes" / f"{run}.json").read_text())
        for c in ev.cases_from(lab, run, "test", store):
            rec = d.diagnose(c["source"], c["anchor"])
            out.append((c, rec, ev.score(c, rec)))
    return out


def test_no_confidently_wrong_diagnosis_on_test_split(results):
    assert not [c["source"] for c, _, s in results if s["confidently_wrong"]]


def test_normal_operation_and_bad_data_never_claim_a_fault(results):
    for c, _, s in results:
        if c["scenario"] in ("S0", "S4", "S5"):
            assert s["no_fault_claimed"], c["source"]


def test_test_split_exact_match_rate(results):
    exact = sum(s["exact"] for _, _, s in results)
    assert exact / len(results) >= 0.9


def test_evidence_record_format_is_frozen(results):
    _, rec, _ = results[0]
    assert rec["record_version"] == RECORD_VERSION == "1.0"
    for k in ("source", "anchor", "target_window", "baseline", "signals", "features", "gate", "hypotheses",
              "model_fit", "counterfactual", "claims", "conclusion", "slots"):
        assert k in rec
    for _, r, _ in results:
        assert r["conclusion"]["level"] in LEVELS
        for c in r["claims"]:
            assert c["level"] in LEVELS
        for sid, s in r["slots"].items():
            assert set(s) == {"value", "unit", "label", "provenance"} and sid.startswith("e")
        json.dumps(r, default=str)  # serialisable


def test_attribution_only_with_a_usable_counterfactual(results):
    for _, r, _ in results:
        for c in r["claims"]:
            if c["level"] == "ATTRIBUTED":
                assert c["counterfactual"]["verdict"] == "but_for_cause" and c["counterfactual"]["fit_usable"]
