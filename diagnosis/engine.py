"""Diagnostic path (P4): anchor -> target window -> matched baselines -> Δ-Signature -> evidence gate ->
hypothesis scores -> counterfactual check -> evidence record (diagnosis/record.py, frozen format v1.0).

    python -m diagnosis.engine --source hist6w_s42 --event hist6w_s42-ev000546
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from diagnosis import counterfactual
from diagnosis.baseline import match
from diagnosis.hypotheses import evaluate
from diagnosis.record import build_record
from diagnosis.signature import delta_signature, gate
from diagnosis.window import features, load_window, session_at, sessions
from memory.store import Store
from security.privacy import scrub_attrs

RULES = Path(__file__).with_name("rules.yaml")
WINDOW = timedelta(minutes=30)
MIN_WINDOW = timedelta(minutes=1)


def load_rules() -> dict:
    return yaml.safe_load(RULES.read_text(encoding="utf-8"))


class Diagnoser:
    def __init__(self, store: Store | None = None, rules: dict | None = None):
        self.store = store or Store()
        self.rules = rules or load_rules()
        self._sessions: dict[str, list] = {}
        self._conditions: dict[str, dict] = {}  # per-source cache of session start conditions

    def sessions(self, key: str):
        if key not in self._sessions:
            self._sessions[key] = sessions(self.store, key)
        return self._sessions[key]

    def diagnose(self, key: str, anchor: datetime, event: dict | None = None,
                 ablate: frozenset = frozenset(), transform=None) -> dict:
        """`ablate` and `transform` exist for experiments only (bench/experiment_b.py, experiment_e.py):
        ablate ⊆ {"no_baseline", "no_gate", "no_model"}; transform(window) corrupts the target window's data."""
        all_s = self.sessions(key)
        target = session_at(all_s, anchor)
        start = max(target.start, anchor - WINDOW)  # never mix two sessions in one window
        if anchor - start < MIN_WINDOW:
            start = anchor - MIN_WINDOW
        w = load_window(self.store, key, start, anchor)
        if transform is not None:
            w = transform(w)
        tgt = features(w, start, anchor)
        unhealthy = [r[0] for r in self.store.query(
            f"SELECT ts FROM {self.store.event_index(key)} "
            "WHERE event_type IN ('BATT_OVERTEMP_WARNING_SET', 'BMS_POWER_DERATE_SET')")]
        base = match(self.store, key, all_s, target, start, anchor, unhealthy, self._conditions.setdefault(key, {}))
        if "no_baseline" in ablate:
            base = {**base, "status": "none", "windows": [], "_windows": []}
        base_feats = [features(load_window(self.store, key, a, b), a, b) for a, b in base["_windows"]]
        sig = delta_signature(tgt, base_feats, self.rules["scale_floor"])
        warnings = self.store.query(f"""SELECT count(*) FROM {self.store.event_index(key)}
                                        WHERE event_type = 'BATT_OVERTEMP_WARNING_SET' AND ts BETWEEN ? AND ?""",
                                    [start, anchor + timedelta(seconds=1)])[0][0]
        g = gate(tgt, sig, base, self.rules, warning_present=warnings > 0, kind=target.kind)
        if "no_gate" in ablate:  # no data-quality checks: only the warning itself is known
            g = {**g, "status": "SUFFICIENT", "flags": ["warning_present"] if warnings else [], "failed": []}

        # Baseline-free reference from the declared physical model (only when cooling can be judged).
        pump_ok = not ({"pump_speed_stale", "pump_speed_missing"} & set(g["flags"]))
        model_in = (counterfactual.prepare(w, start, anchor, pump_ok)
                    if target.kind != "park" and "no_model" not in ablate else None)
        model_fit = counterfactual.fit(model_in, self.rules["counterfactual"]["effectiveness_grid"]) if model_in else None
        if model_fit and model_fit["usable"]:
            sig["model_effectiveness"] = {"obs": model_fit["effectiveness"], "source": "physical model fit"}

        hyps = evaluate(self.rules, sig, set(g["flags"]))
        if g["status"] == "INSUFFICIENT":
            for h in hyps:
                h["accepted"] = h["accepted"] and h["id"] == "stale_or_missing_signal"
        accepted = {h["id"] for h in hyps if h["accepted"]}
        cf = counterfactual.check(model_in, model_fit, accepted & {"cooling_degradation", "elevated_electrical_load"},
                                  sig.get("load_i2", {}).get("base"), self.rules,
                                  observed_peak_max=sig.get("peak_max", {}).get("obs"))
        # A fault whose removal does not prevent the warning stays CONSISTENT_WITH (contributing, not necessary);
        # only a but-for result raises a claim to ATTRIBUTED (diagnosis/record.py).
        return build_record(key, anchor, event, target, start, tgt, base, sig, g, hyps, cf, model_fit)

    def diagnose_event(self, key: str, event_id: str) -> dict:
        row = self.store.query(f"SELECT ts, event_type, attrs FROM {self.store.event_index(key)} WHERE event_id = ?",
                               [event_id])
        if not row:
            raise KeyError(event_id)
        ts, etype, attrs = row[0]
        return self.diagnose(key, ts, {"event_id": event_id, "event_type": etype,
                                          "attrs": scrub_attrs(json.loads(attrs or "{}"))})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--event", help="event_id to diagnose")
    ap.add_argument("--at", help="ISO time to diagnose instead of an event")
    ap.add_argument("--episodes", action="store_true")
    args = ap.parse_args()
    d = Diagnoser(Store(include_episodes=args.episodes or None))
    rec = d.diagnose_event(args.source, args.event) if args.event else d.diagnose(args.source, datetime.fromisoformat(args.at))
    print(json.dumps(rec, indent=2, default=str))


if __name__ == "__main__":
    main()
