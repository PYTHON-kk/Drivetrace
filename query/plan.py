"""Query plans: construction, normalisation and validation (FR-QRY-02/03).

Every plan, whether from Tier 0 rules or the Tier 1 SLM, passes through `check()` before execution.
Only values from query/vocabulary.py are accepted; anything else is rejected, never coerced.
"""

from dataclasses import asdict, dataclass

from query.vocabulary import CONSUMPTION_SIGNALS, KEYS, VALUES


class PlanError(ValueError):
    """The plan is malformed or asks for something the executor does not support."""


@dataclass(frozen=True)
class Plan:
    intent: str
    signal: str = "none"
    period: str = "all"
    group_by: str = "none"
    event_type: str = "none"

    def as_dict(self) -> dict:
        return asdict(self)


def from_dict(d) -> Plan:
    if not isinstance(d, dict):
        raise PlanError("plan must be an object")
    unknown = set(d) - set(KEYS)
    if unknown:
        raise PlanError(f"unknown plan fields: {sorted(unknown)}")
    if "intent" not in d:
        raise PlanError("plan has no intent")
    for k, v in d.items():
        if not isinstance(v, str) or v not in VALUES[k]:
            raise PlanError(f"invalid {k}: {v!r}")
    return Plan(**d)


def check(plan: Plan) -> tuple[Plan, list[str]]:
    """Validate intent/field combinations; apply documented defaults. Returns (plan, notes)."""
    for k in KEYS:
        if getattr(plan, k) not in VALUES[k]:
            raise PlanError(f"invalid {k}: {getattr(plan, k)!r}")
    notes: list[str] = []
    p = plan.as_dict()
    i = plan.intent

    if i in ("list_events", "count_events", "diagnose_event") and plan.event_type == "none":
        raise PlanError(f"{i} needs an event type")
    if i in ("trend", "compare_periods", "compare_groups", "point_lookup", "max_drop") and plan.signal == "none":
        raise PlanError(f"{i} needs a signal")
    if i == "max_drop" and plan.signal not in CONSUMPTION_SIGNALS | {"range"}:
        raise PlanError("max_drop is defined for consumption and range only")
    if i == "compare_groups":
        if plan.group_by not in ("none", "road_type"):
            raise PlanError("compare_groups supports road_type only")
        if plan.group_by == "none":
            p["group_by"] = "road_type"
            notes.append("compared city and highway trips")
    if i == "compare_periods" and plan.period in ("all", "today", "yesterday"):
        p["period"] = "last_14_days"
        notes.append("compared the last 14 days with the 14 days before")
    if i == "unsupported":
        p.update(signal="none", period="all", group_by="none", event_type="none")
    return Plan(**p), notes
