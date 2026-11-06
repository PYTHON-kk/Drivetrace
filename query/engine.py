"""End-to-end question answering for one vehicle source (P2 common-query path).

    python -m query.ask --source hist6w_s42 "How many overheating warnings this month?"

question -> Tier 0 rules (or Tier 1 SLM fallback) -> plan check -> allowlisted executor -> template.
Every stage is timed (FR-UI-03, Experiment C).
"""

import time
from dataclasses import dataclass, field

from memory.store import Store
from query.answers import render, render_refusal
from query.executor import Executor
from query.plan import PlanError, check
from query.planner import PlannerUnavailable, SlmPlanner
from query.router import route


@dataclass
class Answer:
    text: str
    ok: bool
    tier: str                 # tier0 | tier1 | none
    plan: dict | None = None
    rule: str = ""
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    timings_ms: dict = field(default_factory=dict)
    result: object = None     # executor Result, for charts and the evidence panel

    @property
    def total_ms(self) -> float:
        return sum(self.timings_ms.values())


class QueryEngine:
    def __init__(self, store: Store | None = None, planner: SlmPlanner | None = None):
        self.store = store or Store()
        self.executor = Executor(self.store)
        self.planner = planner

    def ask(self, question: str, source: str, role: str = "service") -> Answer:
        t: dict[str, float] = {}

        def lap(name: str, t0: float):
            t[name] = round((time.perf_counter() - t0) * 1000, 2)

        t0 = time.perf_counter()
        routed = route(question)
        lap("route", t0)
        tier, rule, plan = "tier0", "", None
        if routed:
            plan, rule = routed.plan, routed.rule
            if plan.intent == "unsupported":
                return Answer(render_refusal(routed.category), True, tier, plan.as_dict(), rule, timings_ms=t)
        elif self.planner is not None:
            tier = "tier1"
            t0 = time.perf_counter()
            try:
                plan = self.planner.plan(question)
            except (PlanError, PlannerUnavailable) as e:
                lap("plan", t0)
                return Answer(render_refusal("not_understood"), False, "none", notes=[str(e)], timings_ms=t)
            lap("plan", t0)
            if plan.intent == "unsupported":
                return Answer(render_refusal("off_topic"), True, tier, plan.as_dict(), timings_ms=t)
        else:
            return Answer(render_refusal("not_understood"), False, "none", timings_ms=t)

        t0 = time.perf_counter()
        try:
            plan, notes = check(plan)
        except PlanError as e:
            lap("validate", t0)
            return Answer(f"I can't answer that yet: {e}.", False, tier, plan.as_dict(), rule, notes=[str(e)], timings_ms=t)
        lap("validate", t0)

        t0 = time.perf_counter()
        try:
            result = self.executor.run(source, plan)
        except PlanError as e:
            lap("execute", t0)
            return Answer(f"I can't answer that for this vehicle: {e}.", False, tier, plan.as_dict(), rule,
                          notes=notes + [str(e)], timings_ms=t)
        lap("execute", t0)

        t0 = time.perf_counter()
        text = render(plan, result, role)
        lap("render", t0)
        all_notes = notes + result.notes
        if all_notes:
            text += "\n(Note: " + "; ".join(all_notes) + ".)"
        return Answer(text, True, tier, plan.as_dict(), rule, result.evidence, all_notes, t, result)
