"""Property tests for Evidence-Compiled Decoding contracts C1-C3 (build plan P5)."""

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from diagnosis.record import RECORD_VERSION
from ecd.compiler import LEVELS, LEXICON, compile_record, compile_static
from ecd.renderer import ChoiceError, parse, render, verify_choices, verify_text

FAULTS = ["cooling_degradation", "elevated_electrical_load", "temperature_sensor_fault"]
NORMAL_LEVEL = {"none": "ASSOCIATED", "pump_not_commanded": "OBSERVED", "stale_or_missing_signal": "OBSERVED"}
FEATURES = sorted({spec["feature"] for spec in LEXICON["features"].values()})


@st.composite
def records(draw):
    claims = []
    if draw(st.booleans()):
        for h in draw(st.lists(st.sampled_from(FAULTS), unique=True, max_size=3)):
            claims.append({"hypothesis": h, "level": draw(st.sampled_from(["CONSISTENT_WITH", "ATTRIBUTED"]))})
    elif draw(st.booleans()):
        h = draw(st.sampled_from(list(NORMAL_LEVEL)))
        claims.append({"hypothesis": h, "level": NORMAL_LEVEL[h]})
    slots, feats, n = {}, {}, 0
    for f in draw(st.lists(st.sampled_from(FEATURES), unique=True)):
        obs = draw(st.floats(-5e5, 5e5, allow_nan=False, allow_infinity=False))
        n += 1
        ref = {"obs": f"e{n}"}
        slots[f"e{n}"] = {"value": obs, "unit": "°C", "label": f, "provenance": "test"}
        entry = {"obs": obs, "slots": ref}
        if draw(st.booleans()):
            base = draw(st.floats(1.0, 5e5, allow_nan=False, allow_infinity=False))
            rel = (obs - base) / base
            n += 1
            ref["base"] = f"e{n}"
            slots[f"e{n}"] = {"value": base, "unit": "°C", "label": f, "provenance": "test"}
            n += 1
            ref["rel"] = f"e{n}"
            slots[f"e{n}"] = {"value": rel * 100, "unit": "%", "label": f, "provenance": "test"}
            entry.update(base=base, rel=rel)
        feats[f] = entry
    return {"record_version": RECORD_VERSION, "claims": claims, "features": feats, "slots": slots,
            "baseline": {"status": draw(st.sampled_from(["matched", "relaxed", "unmatched", "none"]))},
            "gate": {"status": draw(st.sampled_from(["SUFFICIENT", "AMBIGUOUS", "INSUFFICIENT"]))}}


@st.composite
def record_and_choice(draw):
    rec = draw(records())
    role = draw(st.sampled_from(LEXICON["roles"]))
    c = compile_record(rec, role)
    first = draw(st.sampled_from(sorted(c.claims)))
    rest = draw(st.lists(st.sampled_from(sorted(c.options)), max_size=LEXICON["max_items"] - 1))
    return rec, c, " ".join([first, *rest])


@settings(max_examples=400, deadline=None)
@given(record_and_choice())
def test_any_grammar_valid_choice_satisfies_c1_c2_c3(case):
    rec, c, choice = case
    items = parse(choice, c)
    out = render(rec, c, items)
    v = verify_choices(rec, c, items, choice)
    assert v["c1_digits_in_model_output"] == 0
    assert v["c2_out_of_record"] == [] and v["c3_overclaim"] == []
    t = verify_text(rec, out["text"])
    assert t["untraceable_numbers"] == [], out["text"]      # C1: every number in the answer comes from a slot
    assert not any(ch.isdigit() for ch in c.grammar.split("\n", 1)[1])  # C1: the grammar itself has no digits


@settings(max_examples=200, deadline=None)
@given(records(), st.sampled_from(LEXICON["roles"]))
def test_phrases_never_exceed_the_claim_level(rec, role):
    c = compile_record(rec, role)
    levels = {cl["hypothesis"]: cl["level"] for cl in rec["claims"]} or {"insufficient": "INSUFFICIENT"}
    for opt, meta in c.claims.items():
        phrase_level = LEXICON["phrases"][meta["kind"]][meta["phrase"]]["level"]
        assert LEVELS.index(phrase_level) <= LEVELS.index(levels[meta["hypothesis"]]), opt
    if not any(cl["level"] == "ATTRIBUTED" for cl in rec["claims"]):
        assert not any(o.endswith(":caused") for o in c.claims)  # no causal wording without a counterfactual


@settings(max_examples=200, deadline=None)
@given(records())
def test_driver_role_never_sees_technical_evidence(rec):
    c = compile_record(rec, "driver")
    allowed = {w for w, s in LEXICON["features"].items() if "driver" in s["roles"]}
    assert set(c.evidence) <= allowed


@settings(max_examples=200, deadline=None)
@given(records(), st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=60))
def test_anything_outside_the_compiled_language_is_rejected(rec, junk):
    c = compile_record(rec, "service")
    tokens = junk.split()
    if tokens and tokens[0] in c.claims and all(t in c.options for t in tokens) and len(tokens) <= LEXICON["max_items"]:
        return  # happens to be valid
    with pytest.raises(ChoiceError):
        parse(junk, c)


def test_static_grammar_licenses_what_the_record_does_not():
    rec = {"record_version": RECORD_VERSION, "claims": [{"hypothesis": "temperature_sensor_fault", "level": "CONSISTENT_WITH"}],
           "features": {}, "slots": {}, "baseline": {"status": "matched"}, "gate": {"status": "SUFFICIENT"}}
    compiled, static = compile_record(rec), compile_static()
    assert "cooling:caused" in static.claims and "cooling:caused" not in compiled.claims
    v = verify_choices(rec, compiled, ["cooling:caused", "sensor:caused"], "cooling:caused sensor:caused")
    assert v["c2_out_of_record"] == ["cooling:caused"] and v["c3_overclaim"] == ["sensor:caused"]


def test_free_text_detectors():
    rec = {"claims": [{"hypothesis": "temperature_sensor_fault", "level": "CONSISTENT_WITH"}],
           "slots": {"e1": {"value": 40.43, "unit": "°C"}}}
    v = verify_text(rec, "The battery reached 40.4 °C because the cooling has degraded; it hit 47 °C.")
    assert v["untraceable_numbers"] == ["47"]
    assert v["overclaim"] and v["out_of_record_hypotheses"] == ["cooling_degradation"]


def test_wrong_record_version_is_refused():
    with pytest.raises(ValueError, match="version"):
        compile_record({"record_version": "0.9"})


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k not in ("feature", "level", "word", "kind", "roles", "levels", "version", "max_items"):
                yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def test_fixed_wording_contains_no_digits():
    """C1 also covers the renderer's own text: any digit in an answer must come from a slot."""
    from diagnosis.record import FEATURE_LABELS, FEATURE_UNITS

    texts = list(_strings({k: v for k, v in LEXICON.items() if k not in ("levels", "roles")}))
    texts += list(FEATURE_LABELS.values()) + list(FEATURE_UNITS.values())
    assert [t for t in texts if re.search(r"\d", t)] == []


def test_list_markers_are_not_counted_as_numbers():
    rec = {"claims": [], "slots": {"e1": {"value": 40.4, "unit": "°C"}}}
    assert verify_text(rec, "1. Peak was 40.4 °C.\n2. Nothing else.")["untraceable_numbers"] == []
