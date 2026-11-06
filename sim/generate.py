"""Generate the synthetic DriveTrace datasets (FR-SIM-02/03).

    python -m sim.generate history            # six-week single-vehicle history with injected scenarios
    python -m sim.generate episodes           # short labelled episodes S0-S7 with train/dev/test splits
    python -m sim.generate all

Outputs (git-ignored):
    data/sim/history/<run_id>/{signals,events}/date=YYYY-MM-DD/part-N.parquet
    data/sim/episodes/<run_id>/{signals,events}/...
    data/sim/labels/...   ground truth - never to be read by the assistant
"""

import argparse
import json
import time as clock
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import yaml

from sim.engine import Climate, Engine

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = Path(__file__).with_name("scenarios.yaml")
DATA = ROOT / "data" / "sim"


def load_config() -> dict:
    with open(SCENARIOS, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _jitter(rng, minutes: float) -> float:
    return float(rng.normal(0, minutes * 60))


def default_day(rng, d: date) -> list[dict]:
    """Routine usage: weekday commutes and errands, weekend highway outings."""
    if d.weekday() < 5:
        trips = [
            {"depart": "07:30", "kind": "city", "distance_km": rng.uniform(10, 12), "dest": "work",
             "jitter_s": _jitter(rng, 15)},
            {"depart": "17:15", "kind": "city", "distance_km": rng.uniform(10, 13), "dest": "home",
             "jitter_s": _jitter(rng, 25)},
        ]
        if rng.random() < 0.3:
            trips += [
                {"depart": "19:00", "kind": "city", "distance_km": rng.uniform(3, 6), "dest": "errand"},
                {"depart": "19:50", "kind": "city", "distance_km": rng.uniform(3, 6), "dest": "home"},
            ]
        return trips
    if rng.random() < 0.55:
        dist = rng.uniform(60, 130)
        out = {"depart": "10:00", "kind": "highway", "distance_km": dist, "dest": "far", "jitter_s": _jitter(rng, 45)}
        if rng.random() < 0.5:
            out["dc_charge"] = {"power_w": 100000, "target_soc": 0.8}
        back = {"depart": "15:30", "kind": "highway", "distance_km": dist * rng.uniform(0.95, 1.05),
                "dest": "home", "jitter_s": _jitter(rng, 45)}
        return [out, back]
    return [
        {"depart": "11:00", "kind": "city", "distance_km": rng.uniform(3, 7), "dest": "errand"},
        {"depart": "12:30", "kind": "city", "distance_km": rng.uniform(3, 7), "dest": "home"},
    ]


def _tagged(trips: list[dict], tag: str) -> list[dict]:
    return [dict(t, tag=tag) for t in trips]


# ---------------------------------------------------------------------------
def generate_history(cfg: dict) -> dict:
    h = cfg["history"]
    vehicle = cfg["vehicle"]
    tz = ZoneInfo(vehicle["timezone"])
    start = datetime.fromisoformat(h["start_local"])
    end = datetime.fromisoformat(h["end_local"])
    n_days = (end.date() - start.date()).days + 1
    rng_climate = np.random.default_rng(h["seed"] + 1000)
    c = h["climate"]
    means, noise = [], 0.0
    for k in range(n_days):
        noise = 0.6 * noise + rng_climate.normal(0, c["day_noise_sd_c"])
        means.append(c["mean_start_c"] + (c["mean_end_c"] - c["mean_start_c"]) * k / (n_days - 1) + noise)
    for inj in h["injections"]:
        if "ambient_offset_c" in inj:
            means[inj["day"]] += inj["ambient_offset_c"]
        for offset, delta in (inj.get("ambient_offset_days") or {}).items():
            means[inj["day"] + int(offset)] += delta
    climate = Climate(tz, start.date(), means, c["diurnal_amp_c"])

    run_id = h["run_id"]
    out = DATA / "history" / run_id
    eng = Engine(vehicle, run_id, h["seed"], out, start, end, climate)

    by_day: dict[int, list[dict]] = {}
    first_trip_tags: dict[int, str] = {}
    for inj in h["injections"]:
        tag, day = inj["id"], inj["day"]
        day0 = start.date() + timedelta(days=day)
        eng.labels["scenario_windows"][tag] = {
            "scenario": inj["scenario"], "expected": inj["expected"], "notes": inj.get("notes", ""),
            "start": eng.local_epoch(day0, "00:00") + 86400, "end": eng.local_epoch(day0, "00:00"),
        }
        for f in inj.get("faults", []):
            eng.add_fault(f, day0, tag, inj["scenario"])
        if "trips" in inj:
            by_day[day] = _tagged(inj["trips"], tag)
        for offset, trips in (inj.get("day_trips") or {}).items():
            by_day[day + int(offset)] = _tagged(trips, tag)
        if inj.get("label_window") == "first_trip":
            first_trip_tags[day] = tag

    def planner(k: int, d: date) -> list[dict]:
        trips = by_day.get(k) or default_day(eng.rng, d)
        if k in first_trip_tags:
            trips = [dict(trips[0], tag=first_trip_tags[k])] + trips[1:]
        return trips

    t0 = clock.time()
    eng.run(planner, n_days)
    eng.write_labels(DATA / "labels" / f"history_{run_id}.json",
                     {"daily_mean_ambient_c": [round(m, 2) for m in means]})
    return {"run_id": run_id, "signal_rows": eng.rec.n_signals, "events": eng.rec.n_events,
            "seconds": round(clock.time() - t0, 1)}


# ---------------------------------------------------------------------------
def generate_episode(cfg: dict, scenario: str, seed: int, index: int) -> dict:
    ep = cfg["episodes"]
    tpl = ep["templates"][scenario]
    vehicle = cfg["vehicle"]
    tz = ZoneInfo(vehicle["timezone"])
    run_id = f"ep_{scenario}_{seed:02d}"
    rng = np.random.default_rng(10_000 + 97 * seed + index)
    start = datetime.fromisoformat(ep["start_date"]) + timedelta(days=4 * index)
    n_days = ep["days"]
    end = start + timedelta(days=n_days) - timedelta(minutes=1)
    base = rng.uniform(*tpl["ambient_c"])
    means = [base + rng.normal(0, 1.0) for _ in range(n_days)]
    climate = Climate(tz, start.date(), means, 5.0)
    vcfg = dict(vehicle, initial=dict(vehicle["initial"], soc=float(rng.uniform(0.8, 0.9)),
                                      temp_c=float(means[0])))
    eng = Engine(vcfg, run_id, 20_000 + 97 * seed + index, DATA / "episodes" / run_id, start, end, climate)

    tag = f"{scenario}_trigger"
    eng.labels["scenario_windows"][tag] = {
        "scenario": scenario, "expected": tpl["expected"], "notes": tpl.get("notes", ""),
        "start": eng.local_epoch(start.date() + timedelta(days=n_days - 1), "00:00") + 86400,
        "end": eng.local_epoch(start.date(), "00:00"),
    }
    for f in tpl.get("faults", []):
        eng.add_fault(f, start.date(), tag, scenario)
    trigger = tpl.get("trigger") or ep["baseline_day"]

    def planner(k: int, d: date) -> list[dict]:
        if k < n_days - 1:
            return [dict(t) for t in ep["baseline_day"]]
        trips = _tagged(trigger, tag)
        if tpl.get("label_window") == "first_trip":
            trips = [trips[0]] + [{k2: v for k2, v in t.items() if k2 != "tag"} for t in trips[1:]]
        return trips

    eng.run(planner, n_days)
    eng.write_labels(DATA / "labels" / "episodes" / f"{run_id}.json",
                     {"scenario": scenario, "base_ambient_c": round(base, 2)})
    return {"run_id": run_id, "signal_rows": eng.rec.n_signals, "events": eng.rec.n_events}


def generate_episodes(cfg: dict, only: list[str] | None = None) -> list[dict]:
    ep = cfg["episodes"]
    splits: dict[str, list[str]] = {"train": [], "dev": [], "test": []}
    results, index = [], 0
    for scenario in ep["templates"]:
        if only and scenario not in only:
            continue
        if scenario in ep["heldout_scenarios"]:
            plan = [("test", s) for s in ep["heldout_seeds"]]
        else:
            plan = [(split, s) for split, seeds in ep["splits"].items() for s in seeds]
        for split, seed in plan:
            r = generate_episode(cfg, scenario, seed, index)
            index += 1
            splits[split].append(r["run_id"])
            results.append(r)
            print(f"  {r['run_id']:<12} {split:<5} rows={r['signal_rows']:>8} events={r['events']}")
    path = DATA / "labels" / "splits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(splits, indent=2), encoding="utf-8")
    return results


def generate_blind(cfg: dict, seeds: list[int], split: str = "blind", index_offset: int = 100) -> list[dict]:
    """Fresh episodes for a split no rule was ever tuned or inspected on (added for P7, after rules v1.1).
    Appends to splits.json instead of rewriting it, so existing splits keep their episodes."""
    path = DATA / "labels" / "splits.json"
    splits = json.loads(path.read_text(encoding="utf-8"))
    splits[split] = []
    results, index = [], index_offset
    for scenario in cfg["episodes"]["templates"]:
        for seed in seeds:
            r = generate_episode(cfg, scenario, seed, index)
            index += 1
            splits[split].append(r["run_id"])
            results.append(r)
            print(f"  {r['run_id']:<12} {split:<5} rows={r['signal_rows']:>8} events={r['events']}")
    path.write_text(json.dumps(splits, indent=2), encoding="utf-8")
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["history", "episodes", "all", "blind"])
    ap.add_argument("--scenarios", help="comma-separated subset for episodes, e.g. S1,S2")
    ap.add_argument("--seeds", default="6,7", help="seeds for the blind split")
    args = ap.parse_args()
    cfg = load_config()
    if args.what in ("history", "all"):
        print("history:", generate_history(cfg))
    if args.what in ("episodes", "all"):
        only = args.scenarios.split(",") if args.scenarios else None
        rs = generate_episodes(cfg, only)
        print(f"episodes: {len(rs)} runs, {sum(r['signal_rows'] for r in rs)} signal rows")
    if args.what == "blind":
        rs = generate_blind(cfg, [int(x) for x in args.seeds.split(",")])
        print(f"blind episodes: {len(rs)} runs")


if __name__ == "__main__":
    main()
