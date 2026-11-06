"""Experiment F dry run - Evidence-Compiled Decoding vs free generation (solution_journey §9.3, build plan P5).

    python -m bench.experiment_f            # dev episodes + six-week history, both models

Conditions (same model, same facts in the prompt, temperature 0):
  F1  free generation from the facts (no grammar)
  F2  choice words under ONE static grammar (any claim, any phrase, any feature)
  F3  ECD: choice words under the grammar compiled from this evidence record (service role)
  F4  ECD for the driver role (role-scoped vocabulary)
Measured on the final answer text: numbers not traceable to a record slot (C1), hypotheses the record does
not contain (C2), causal wording without an ATTRIBUTED claim (C3); plus whether every concluded
hypothesis is mentioned, and latency. Dry run: 15 records, so read rates as rough.
"""

import json
import statistics
import time
import urllib.request

from bench.diagnosis_eval import LABELS, cases_from
from diagnosis.engine import Diagnoser
from ecd.compiler import LEXICON, compile_record, compile_static
from ecd.renderer import ChoiceError, parse, render, verify_choices, verify_text
from memory.store import ROOT, Store
from query.planner import LocalServer

OUT = ROOT / "bench" / "results" / "experiment_f.json"
PORT = 8089
MODELS = ["SmolLM2-360M-Instruct-Q4_K_M.gguf", "qwen2.5-0.5b-instruct-q4_k_m.gguf"]

CHOICE_SYSTEM = (
    "You explain a vehicle battery diagnosis. Choose items from the lists: first one conclusion, then up to four "
    "more conclusions or evidence items that best answer the question, most important first. Write only the item "
    "names, separated by spaces.\n\n"
    "Example\nQuestion: Why did the warning come on?\nConclusions:\n  pump:consistent = the data is consistent with a "
    "pump fault\nEvidence:\n  flow = coolant flow 2 vs 5 in comparable sessions\n  outside = outside temperature 30\n"
    "Answer: pump:consistent flow\n\n"
)
FREE_SYSTEM = ("You explain a vehicle battery diagnosis to a service technician. Use only the facts given. "
               "Answer in at most three sentences.\n\n")


def complete(prompt: str, grammar: str | None, n_predict: int) -> tuple[str, dict, float]:
    body = {"prompt": prompt, "n_predict": n_predict, "temperature": 0, "cache_prompt": True}
    if grammar:
        body["grammar"] = grammar
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/completion", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return data["content"].strip(), data.get("timings", {}), (time.perf_counter() - t0) * 1000


def option_lines(rec: dict, compiled) -> str:
    claims = "\n".join(f"  {o} = {render(rec, compiled, [o])['text'].split(' (')[0]}" for o in sorted(compiled.claims))
    ev = "\n".join(f"  {o} = {render(rec, compiled, [o])['text']}" for o in sorted(compiled.evidence))
    return f"Conclusions:\n{claims}\nEvidence:\n{ev or '  (none)'}\n"


def facts(rec: dict, compiled) -> str:
    """Same information as the option list, as sentences: strongest licensed phrase per claim + all evidence."""
    strongest = {}
    for o, m in compiled.claims.items():
        lvl = LEXICON["levels"].index(LEXICON["phrases"][m["kind"]][m["phrase"]]["level"])
        if m["hypothesis"] not in strongest or lvl > strongest[m["hypothesis"]][0]:
            strongest[m["hypothesis"]] = (lvl, o)
    items = [o for _, o in strongest.values()] + sorted(compiled.evidence)
    return "\n".join(f"- {render(rec, compiled, [o])['text']}" for o in items) + "\n"


def covers(rec: dict, chosen_or_text, is_text: bool) -> bool:
    hyps = {c["hypothesis"] for c in rec["claims"]}
    if not hyps:
        return True
    if is_text:
        words = {"cooling_degradation": "cool", "elevated_electrical_load": "load", "temperature_sensor_fault": "sensor",
                 "pump_not_commanded": "pump", "none": "normal", "stale_or_missing_signal": "pump"}
        return all(words[h] in chosen_or_text.lower() for h in hyps)
    chosen = {LEXICON_WORD[i.split(":")[0]] for i in chosen_or_text if ":" in i}
    return hyps <= chosen


LEXICON_WORD = {spec["word"]: h for h, spec in LEXICON["claims"].items()}


def run_condition(cond: str, rec: dict, question: str) -> dict:
    role = "driver" if cond == "F4" else "service"
    compiled = compile_record(rec, role)
    if cond == "F1":
        prompt = f"{FREE_SYSTEM}Question: {question}\nFacts:\n{facts(rec, compiled)}Answer: "
        text, t, wall = complete(prompt, None, 120)
        v = verify_text(rec, text)
        return {"cond": cond, "output": text, "answer": text, **v, "c2": v["out_of_record_hypotheses"],
                "c3": v["overclaim"], "covers_conclusions": covers(rec, text, True), "valid": True,
                "gen_tokens": t.get("predicted_n"), "prompt_tokens": t.get("prompt_n"), "wall_ms": round(wall)}
    grammar = compile_static(role).grammar if cond == "F2" else compiled.grammar
    prompt = f"{CHOICE_SYSTEM}Question: {question}\n{option_lines(rec, compiled)}Answer: "
    raw, t, wall = complete(prompt, grammar, 48)
    try:
        items = parse(raw, compiled, strict=(cond != "F2"))
        valid = True
    except ChoiceError:
        items, valid = parse(raw, compiled, strict=False), False
    vc = verify_choices(rec, compiled, items, raw)
    rendered = render(rec, compiled, [i for i in items if i in compiled.options] if cond == "F2" else items)
    # F2: what the model *chose* is judged, even though the renderer can only print licensed items
    vt = verify_text(rec, rendered["text"])
    return {"cond": cond, "output": raw, "answer": rendered["text"], **vt, "c1_digits": vc["c1_digits_in_model_output"],
            "c2": vc["c2_out_of_record"] + vt["out_of_record_hypotheses"], "c3": bool(vc["c3_overclaim"]) or vt["overclaim"],
            "covers_conclusions": covers(rec, items, False), "valid": valid,
            "gen_tokens": t.get("predicted_n"), "prompt_tokens": t.get("prompt_n"), "wall_ms": round(wall)}


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "test"],
                    help="dev = 7 dev episodes + 8 history cases (the dry run); test = 17 held-out episodes")
    args = ap.parse_args()
    out = OUT if args.split == "dev" else OUT.with_name("experiment_f_test.json")
    store = Store(include_episodes=True)
    d = Diagnoser(store)
    splits = json.loads((LABELS / "splits.json").read_text())
    cases = []
    for run in splits[args.split]:
        cases += cases_from(json.loads((LABELS / "episodes" / f"{run}.json").read_text()), run, args.split, store)
    if args.split == "dev":
        hist = json.loads(next(LABELS.glob("history_*.json")).read_text())
        cases += cases_from(hist, hist["run_id"], "history", store)
    records = []
    for c in cases:
        rec = d.diagnose(c["source"], c["anchor"])
        q = ("What happened before the battery overheating warning?" if c["warning"]
             else "Is the battery cooling working normally?")
        records.append((c, rec, q))

    results = {"n_records": len(records), "models": {}}
    for model in MODELS:
        rows = []
        with LocalServer(ROOT / "models" / model, port=PORT):
            # condition-major order: consecutive requests differ in the record, so only the shared
            # instruction prefix is ever served from the prompt cache (fair latency comparison)
            for cond in ("F1", "F2", "F3", "F4"):
                for c, rec, q in records:
                    r = run_condition(cond, rec, q)
                    rows.append({"source": c["source"], "scenario": c["scenario"], **r})
        summary = {}
        for cond in ("F1", "F2", "F3", "F4"):
            rs = [r for r in rows if r["cond"] == cond]
            summary[cond] = {
                "n": len(rs),
                "answers_with_untraceable_numbers": sum(bool(r["untraceable_numbers"]) for r in rs),
                "untraceable_numbers_total": sum(len(r["untraceable_numbers"]) for r in rs),
                "answers_with_out_of_record_hypotheses": sum(bool(r["c2"]) for r in rs),
                "answers_with_overclaim": sum(bool(r["c3"]) for r in rs),
                "valid_choice_strings": sum(r["valid"] for r in rs),
                "covers_all_conclusions": sum(r["covers_conclusions"] for r in rs),
                "p50_wall_ms": statistics.median(r["wall_ms"] for r in rs),
                "mean_gen_tokens": round(statistics.mean(r["gen_tokens"] or 0 for r in rs), 1),
                "mean_prompt_tokens": round(statistics.mean(r["prompt_tokens"] or 0 for r in rs), 1),
            }
        results["models"][model] = {"summary": summary, "rows": rows}
        print(f"\n{model}")
        print(f"  {'cond':<4} {'untraceable#':>13} {'out-of-record':>14} {'overclaim':>10} {'valid':>6} {'covers':>7} {'p50 ms':>7} {'gen tok':>8} {'prompt tok':>10}")
        for cond, s in summary.items():
            print(f"  {cond:<4} {s['answers_with_untraceable_numbers']:>8}/{s['n']:<4} {s['answers_with_out_of_record_hypotheses']:>9}/{s['n']:<4} "
                  f"{s['answers_with_overclaim']:>5}/{s['n']:<4} {s['valid_choice_strings']:>3}/{s['n']:<2} {s['covers_all_conclusions']:>3}/{s['n']:<3} "
                  f"{s['p50_wall_ms']:>7.0f} {s['mean_gen_tokens']:>8} {s['mean_prompt_tokens']:>10}")
    from bench.env import header

    results["header"] = header()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
