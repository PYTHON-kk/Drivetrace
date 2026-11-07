"""Loader and scorer for tests/data/query_set.yaml (shared by tests and bench/routing_eval.py)."""

from pathlib import Path

import yaml

from query.vocabulary import KEYS

PATH = Path(__file__).with_name("data") / "query_set.yaml"
INDEPENDENT_PATH = Path(__file__).with_name("data") / "independent_set.yaml"


def load(split: str | None = None) -> list[dict]:
    items = yaml.safe_load(PATH.read_text(encoding="utf-8"))["items"]
    for i, it in enumerate(items):
        it["split"] = "dev" if i % 2 == 0 else "test"
        it["index"] = i
    return [it for it in items if split is None or it["split"] == split]


def score(item: dict, plan: dict | None) -> list[str]:
    """Fields that disagree with the expectation ([] = correct). Unspecified fields are not scored."""
    if plan is None:
        return ["no_plan"]
    return [k for k in KEYS if k in item and plan.get(k) != item[k]]


def load_independent() -> list[dict]:
    items = yaml.safe_load(INDEPENDENT_PATH.read_text(encoding="utf-8"))["items"]
    for i, it in enumerate(items):
        it["split"] = "independent"
        it["index"] = i
    return items
