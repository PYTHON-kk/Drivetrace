"""Evidence-Compiled Decoding: compile one evidence record (diagnosis/record.py v1.0) into
(a) a GBNF grammar over choice words, (b) the option list shown to the model, (c) the required caveats.

Contracts enforced by construction:
  C1  the grammar contains no digits, so the model cannot emit a number; numbers come only from slots;
  C2  only claims and features present in the record are choices;
  C3  a claim can only be combined with phrases at or below its epistemic level.
`compile_static` builds the non-compiled baseline grammar (Experiment F2): every claim x phrase x feature.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from diagnosis.record import RECORD_VERSION

LEXICON = yaml.safe_load((Path(__file__).with_name("lexicon.yaml")).read_text(encoding="utf-8"))
LEVELS = LEXICON["levels"]


@dataclass
class Compiled:
    role: str
    claims: dict = field(default_factory=dict)    # "cooling:consistent" -> {"hypothesis", "phrase", "level"}
    evidence: dict = field(default_factory=dict)  # "load" -> {"feature", "label"}
    caveats: list = field(default_factory=list)
    grammar: str = ""

    @property
    def options(self) -> set:
        return set(self.claims) | set(self.evidence)


def _claim_entries(record: dict) -> list[tuple[str, str]]:
    """(hypothesis id, level) for every claim; an empty record yields the 'insufficient' pseudo-claim."""
    entries = [(c["hypothesis"], c["level"]) for c in record["claims"]]
    return entries or [("insufficient", "INSUFFICIENT")]


def _phrases_for(kind: str, level: str) -> list[str]:
    return [p for p, spec in LEXICON["phrases"][kind].items() if LEVELS.index(spec["level"]) <= LEVELS.index(level)]


def _grammar(claim_opts: list[str], evidence_opts: list[str], max_items: int) -> str:
    alt = lambda xs: " | ".join(f'"{x}"' for x in xs)  # noqa: E731
    item = "claim | evidence" if evidence_opts else "claim"
    rest = "".join(f' (" " item' for _ in range(max_items - 1)) + ")?" * (max_items - 1)
    lines = [
        "# GENERATED per evidence record by ecd/compiler.py - choice words only, no digits.",
        f"root ::= claim{rest}",
        f"item ::= {item}",
        f"claim ::= {alt(claim_opts)}",
    ]
    if evidence_opts:
        lines.append(f"evidence ::= {alt(evidence_opts)}")
    return "\n".join(lines) + "\n"


def compile_record(record: dict, role: str = "service") -> Compiled:
    if record.get("record_version") != RECORD_VERSION:
        raise ValueError(f"evidence record version {record.get('record_version')!r} != {RECORD_VERSION}")
    if role not in LEXICON["roles"]:
        raise ValueError(f"unknown role {role!r}")
    c = Compiled(role)
    for hyp, level in _claim_entries(record):
        spec = LEXICON["claims"][hyp]
        for phrase in _phrases_for(spec["kind"], level):
            c.claims[f"{spec['word']}:{phrase}"] = {"hypothesis": hyp, "phrase": phrase, "level": level, "kind": spec["kind"]}
    for word, spec in LEXICON["features"].items():
        f = record["features"].get(spec["feature"])
        if role in spec["roles"] and f is not None and f.get("obs") is not None:
            c.evidence[word] = {"feature": spec["feature"], "label": spec.get(role, spec["label"])}
    c.caveats = required_caveats(record)
    c.grammar = _grammar(sorted(c.claims), sorted(c.evidence), LEXICON["max_items"])
    assert not any(ch.isdigit() for ch in "".join(c.options)), "C1: choice words must not contain digits"
    return c


def compile_static(role: str = "service") -> Compiled:
    """Experiment F2: one grammar for every record - any claim with any phrase, any feature."""
    c = Compiled(role)
    for hyp, spec in LEXICON["claims"].items():
        for phrase, p in LEXICON["phrases"][spec["kind"]].items():
            c.claims[f"{spec['word']}:{phrase}"] = {"hypothesis": hyp, "phrase": phrase, "level": p["level"], "kind": spec["kind"]}
    for word, spec in LEXICON["features"].items():
        c.evidence[word] = {"feature": spec["feature"], "label": spec["label"]}
    c.grammar = _grammar(sorted(c.claims), sorted(c.evidence), LEXICON["max_items"])
    return c


def required_caveats(record: dict) -> list[str]:
    out = []
    if any(cl["level"] == "ATTRIBUTED" for cl in record["claims"]):
        out.append("model")
    status = record["baseline"]["status"]
    if status == "unmatched":
        out.append("unmatched")
    elif status == "none":
        out.append("no_base")
    if record["gate"]["status"] != "SUFFICIENT":
        out.append("ambiguous")
    return out
