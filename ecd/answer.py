"""ECD answers for the diagnostic path: choose items (deterministically or with the SLM), then render.

The deterministic choice is always available and is the fallback whenever the SLM is unavailable or returns
anything the compiled grammar does not license. Either way the output satisfies C1-C3.
"""

import json
import urllib.request

from ecd.compiler import LEXICON, compile_record
from ecd.renderer import ChoiceError, parse, render

EVIDENCE_PRIORITY = {  # most informative first, per role
    "service": ["model_cooling", "heating", "pump_speed", "coolant", "load", "sensor_gap", "peak_temp", "outside_temp"],
    "fleet": ["load", "pump_speed", "peak_temp", "outside_temp"],
    "driver": ["peak_temp", "outside_temp"],
}
LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEXICON["levels"])}

CHOICE_SYSTEM = (
    "You explain a vehicle battery diagnosis. Choose items from the lists: first one conclusion, then up to four "
    "more conclusions or evidence items that best answer the question, most important first. Write only the item "
    "names, separated by spaces.\n\n"
    "Example\nQuestion: Why did the warning come on?\nConclusions:\n  pump:consistent = the data is consistent with a "
    "pump fault\nEvidence:\n  flow = coolant flow 2 vs 5 in comparable sessions\n  outside = outside temperature 30\n"
    "Answer: pump:consistent flow\n\n"
)


def option_lines(rec: dict, compiled) -> str:
    claims = "\n".join(f"  {o} = {render(rec, compiled, [o])['text'].split(' (')[0]}" for o in sorted(compiled.claims))
    ev = "\n".join(f"  {o} = {render(rec, compiled, [o])['text']}" for o in sorted(compiled.evidence))
    return f"Conclusions:\n{claims}\nEvidence:\n{ev or '  (none)'}\n"


def default_choice(record: dict, compiled) -> list[str]:
    claims = sorted(record["claims"], key=lambda c: -c["score"]) or [{"hypothesis": "insufficient"}]
    picked = []
    for c in claims:
        opts = [(o, m) for o, m in compiled.claims.items() if m["hypothesis"] == c["hypothesis"]]
        if opts:  # strongest licensed phrase for this claim
            best = max(opts, key=lambda om: (LEVEL_RANK[LEXICON["phrases"][om[1]["kind"]][om[1]["phrase"]]["level"]],
                                             om[1]["phrase"] != "possible"))
            picked.append(best[0])
    room = LEXICON["max_items"] - len(picked)
    evidence = [e for e in EVIDENCE_PRIORITY[compiled.role] if e in compiled.evidence][:max(0, min(room, 3))]
    return picked[:LEXICON["max_items"]] + evidence


def slm_choice(record: dict, compiled, question: str, port: int = 8089, timeout_s: float = 20) -> list[str] | None:
    prompt = f"{CHOICE_SYSTEM}Question: {question[:300]}\n{option_lines(record, compiled)}Answer: "
    body = {"prompt": prompt, "grammar": compiled.grammar, "n_predict": 48, "temperature": 0, "cache_prompt": True}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/completion", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = json.load(r)["content"]
        return parse(raw, compiled)
    except (OSError, ValueError, ChoiceError, KeyError):
        return None


def answer(record: dict, role: str = "service", question: str | None = None, use_slm: bool = False) -> dict:
    compiled = compile_record(record, role)
    items, chooser = None, "deterministic"
    if use_slm and question:
        items = slm_choice(record, compiled, question)
        chooser = "slm" if items else "deterministic (slm unavailable or invalid)"
    if not items:
        items = default_choice(record, compiled)
    out = render(record, compiled, items)
    return {**out, "role": role, "chooser": chooser, "grammar": compiled.grammar}
