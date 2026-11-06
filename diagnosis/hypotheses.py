"""Hypothesis compatibility scoring S(h|E) = Σ w·support − Σ w·contradiction (solution_journey §8.7, FR-DX-02).

Rules come from diagnosis/rules.yaml. A score ranks hypotheses; it is not a probability.
"""

import operator
import re

OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}
COND = re.compile(r"^(\w+)\.(obs|base|rel|z)\s*(>=|<=|>|<)\s*(-?[\d.]+)$")


def holds(expr: str, signature: dict, flags: set) -> bool | None:
    """True / False, or None when the feature needed is not available."""
    if expr.startswith("flag:"):
        return expr[5:] in flags
    m = COND.match(expr.replace(" ", "")) or COND.match(expr)
    if not m:
        raise ValueError(f"bad rule condition: {expr!r}")
    feat, attr, op, val = m.groups()
    v = signature.get(feat, {}).get(attr)
    return None if v is None else OPS[op](v, float(val))


def evaluate(cfg: dict, signature: dict, flags: set) -> list[dict]:
    out = []
    for hid, h in cfg["hypotheses"].items():
        support, against, unknown = [], [], []
        for rule in h.get("supports", []):
            r = holds(rule["when"], signature, flags)
            if r:
                support.append(rule)
            elif r is None:
                unknown.append(rule)
        for rule in h.get("contradicts", []):
            r = holds(rule["when"], signature, flags)
            if r:
                against.append(rule)
        missing = [f for f in h.get("requires", []) if signature.get(f.split(".")[0], {}).get(f.split(".")[1]) is None]
        score = sum(r["weight"] for r in support) - sum(r["weight"] for r in against) - 1.0 * len(missing)
        out.append({"id": hid, "label": h["label"], "score": round(score, 3),
                    "accepted": score >= cfg["accept"] and not missing,
                    "supports": [r["says"] for r in support], "contradictions": [r["says"] for r in against],
                    "unknown": [r["when"] for r in unknown], "missing": missing})
    accepted = {h["id"] for h in out if h["accepted"]}
    normal = set(cfg["normal"])
    if accepted - normal:  # a fault explains the data: drop normal-operation explanations
        for h in out:
            if h["id"] in normal:
                h["accepted"] = False
    elif "pump_not_commanded" in accepted:
        for h in out:
            if h["id"] == "none":
                h["accepted"] = False
    return sorted(out, key=lambda h: -h["score"])
