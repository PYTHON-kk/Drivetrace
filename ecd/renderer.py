"""ECD renderer and contract verifier.

parse()   checks a choice string against a compiled record (the same language as the grammar);
render()  expands choices into text - fixed wording from the lexicon, numbers only from record slots,
          required caveats always appended;
verify()  measures contract violations on structured choices (C1-C3) and, for any text (including free
          generation, Experiment F1), on the text itself.
"""

import re

from ecd.compiler import LEVELS, LEXICON, Compiled

ENUMERATOR = re.compile(r"(^|\n)\s*\d{1,2}[.)]\s")
NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")
CAUSAL = re.compile(r"\b(caused|causing|because|due to|led to|leads to|resulted|result of|the reason|responsible)\b", re.I)
HYPOTHESIS_WORDS = {  # crude keyword detectors used only to score free text (F1)
    "cooling_degradation": r"\bcool(ing|ant)?\b.*\b(degrad|reduc|poor|fail|weak|less)|\b(degrad|reduc|poor|fail|weak)\w* cool",
    "elevated_electrical_load": r"\b(high|heavy|elevated|excess)\w* (electrical )?(load|current|power)\b",
    "temperature_sensor_fault": r"\bsensor\b.*\b(fault|wrong|error|fail|bias)|\b(faulty|failed|biased) (temperature )?sensor",
}


class ChoiceError(ValueError):
    pass


def parse(text: str, compiled: Compiled, strict: bool = True) -> list[str]:
    items = text.strip().split()
    if not items:
        raise ChoiceError("empty choice")
    if len(items) > LEXICON["max_items"]:
        raise ChoiceError("too many items")
    if strict:
        if items[0] not in compiled.claims:
            raise ChoiceError(f"first item must be a licensed claim, got {items[0]!r}")
        bad = [i for i in items if i not in compiled.options]
        if bad:
            raise ChoiceError(f"not licensed for this record: {bad}")
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _fmt(v: float, unit: str) -> str:
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:.2f}" if abs(v) < 10 else f"{v:.1f}"
    return f"{s} {unit}".strip() if unit else s


def render(record: dict, compiled: Compiled, items: list[str]) -> dict:
    """Returns text plus the provenance of every number it contains."""
    role = compiled.role
    numbers, sentences = [], []
    by_word = {spec["word"]: (hid, spec) for hid, spec in LEXICON["claims"].items()}
    for item in items:
        if ":" in item:
            word, phrase = item.split(":", 1)
            hid, spec = by_word[word]
            kind = spec["kind"]
            label = spec.get(role, spec["label"]) if role == "driver" else spec["label"]
            tmpl = LEXICON["phrases"][kind][phrase]
            text = (tmpl["driver"] if role == "driver" else tmpl["text"]).format(label=label)
            sentences.append(text[0].upper() + text[1:] + ".")
            continue
        fspec = LEXICON["features"][item]
        f = record["features"].get(fspec["feature"])
        if not f or f.get("obs") is None:
            continue  # unreachable for compiled grammars; F2 may choose a feature the record lacks
        slots = f.get("slots", {})
        unit = record["slots"][slots["obs"]]["unit"] if "obs" in slots else ""
        label = fspec.get(role, fspec["label"]) if role == "driver" else fspec["label"]
        obs = _fmt(f["obs"], unit)
        numbers.append((obs, slots.get("obs")))
        if f.get("base") is not None and "base" in slots:
            base = _fmt(f["base"], unit)
            numbers.append((base, slots["base"]))
            if role == "driver":
                sentences.append(f"The {label} was {obs} (usually about {base}).")
            else:
                rel = f" ({f['rel'] * 100:+.0f}%)" if f.get("rel") is not None else ""
                if rel:
                    numbers.append((f"{f['rel'] * 100:+.0f}%", slots.get("rel")))
                sentences.append(f"{label[0].upper() + label[1:]}: {obs} vs {base} in comparable sessions{rel}.")
        else:
            sentences.append(f"{label[0].upper() + label[1:]}: {obs}.")
    caveats = [LEXICON["caveats"][c]["driver" if role == "driver" else "text"] for c in compiled.caveats]
    text = " ".join(sentences + ([f"({' '.join(caveats)})"] if caveats else []))
    return {"text": text, "numbers": numbers, "items": items}


def allowed_numbers(record: dict) -> list[float]:
    vals = []
    for s in record["slots"].values():
        v = s["value"]
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return vals


def _traceable(token: str, allowed: list[float]) -> bool:
    try:
        x = float(token.replace(",", "").replace("+", ""))
    except ValueError:
        return True
    for v in allowed:
        for d in (0, 1, 2, 3):
            if abs(round(v, d) - x) < 10 ** -d / 2 + 1e-9 or abs(round(abs(v), d) - abs(x)) < 10 ** -d / 2 + 1e-9:
                return True
    return False


def verify_text(record: dict, text: str) -> dict:
    """Contract checks that work on any text (used for F1 free generation and on rendered output)."""
    allowed = allowed_numbers(record)
    text = ENUMERATOR.sub(r"\1", text)  # "1. ", "2) " list markers are not claims about the vehicle
    nums = NUMBER.findall(text)
    untraceable = [n for n in nums if not _traceable(n, allowed)]
    claimed = {c["hypothesis"] for c in record["claims"]}
    out_of_record = [h for h, rx in HYPOTHESIS_WORDS.items() if h not in claimed and re.search(rx, text, re.I)]
    attributed = any(c["level"] == "ATTRIBUTED" for c in record["claims"])
    overclaim = bool(CAUSAL.search(text)) and not attributed
    return {"numbers": len(nums), "untraceable_numbers": untraceable, "out_of_record_hypotheses": out_of_record,
            "overclaim": overclaim}


def verify_choices(record: dict, compiled_for_record: Compiled, items: list[str], raw: str) -> dict:
    """C1-C3 on the structured choices, judged against what this record licenses."""
    lic = compiled_for_record
    record_levels = {c["hypothesis"]: c["level"] for c in record["claims"]} or {"insufficient": "INSUFFICIENT"}
    c2, c3 = [], []
    by_word = {spec["word"]: hid for hid, spec in LEXICON["claims"].items()}
    for i in items:
        if ":" in i:
            word, phrase = i.split(":", 1)
            hid = by_word.get(word)
            if hid not in record_levels:
                c2.append(i)
                continue
            kind = LEXICON["claims"][hid]["kind"]
            p_level = LEXICON["phrases"][kind].get(phrase, {}).get("level", "ATTRIBUTED")
            if LEVELS.index(p_level) > LEVELS.index(record_levels[hid]):
                c3.append(i)
        elif i not in lic.evidence:
            c2.append(i)
    return {"c1_digits_in_model_output": sum(ch.isdigit() for ch in raw), "c2_out_of_record": c2, "c3_overclaim": c3}
