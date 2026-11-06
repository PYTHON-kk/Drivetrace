# P5 results - Evidence-Compiled Decoding (ECD)

**Date:** 2026-10-29
**Host:** x86 proxy (AMD Ryzen 7 5800HS, SLM at 2 threads). **Data:** synthetic diagnostic records only.
**Raw results:** `bench/results/experiment_f.json`, log `bench/results/experiment_f.log`. Re-run: `python -m bench.experiment_f`.

## Design as built (revised after G0)

G0 showed that grammar-forced text still costs a decode step per token, so the model does **not** write the answer. It only chooses items; the deterministic renderer writes every word and number.

```
evidence record v1.0 ──► ecd/compiler.py ──► per-record GBNF over choice words ──► SLM (optional) ──► choices
                                   └──────────────► option list in the prompt
choices ──► ecd/renderer.py ──► text (lexicon wording + record slots + required caveats)
```

| Contract | How it is enforced | Checked by |
|---|---|---|
| **C1** no numbers outside the evidence | Choice words contain no digits, so the model cannot emit a number. All numbers in the answer come from record slots, and the fixed wording contains no digits. | Property test over random records; a test that fails if any lexicon text contains a digit |
| **C2** no entity outside the record | Only claims and features present in this record are choices | Property test; Experiment F |
| **C3** wording never stronger than the evidence | A claim can only take phrases at or below its epistemic level; "caused" exists only for ATTRIBUTED claims | Property test; Experiment F |
| Required caveats | Added by the renderer (model is the simulator; unmatched baseline; inconclusive checks), not chosen | Renderer code |
| Roles | Driver sees plain conclusions plus peak and outside temperature only; service sees all features; fleet sees aggregate ones | Property test (driver never receives technical evidence) |

**Two paths:**
- **Deterministic (default):** every claim with its strongest licensed phrase, plus role-specific evidence. It always covers all conclusions and takes no model time.
- **SLM choice (opt-in):** used for question-specific selection and ordering. Anything unlicensed falls back to the deterministic choice.

Diagnosis answers in the product (`query.ask --role driver|service|fleet`) are rendered through ECD.

## Experiment F dry run

15 records (7 dev episodes + 8 history cases), temperature 0, the same facts given to every condition, conditions run one after another so only the shared instruction prefix is cached.

- **F1:** free text.
- **F2:** choices under one static grammar.
- **F3:** ECD (service role).
- **F4:** ECD (driver role).

**SmolLM2-360M**

| Condition | Answers with untraceable numbers | Hypotheses not in the record | Overclaims | Covers every conclusion | p50 latency | Generated tokens |
|---|---|---|---|---|---|---|
| F1 free | 1/15 | 0/15 | 0/15 | 4/15 | 2.4 s | 11 |
| F2 static | 0/15 | 2/15 | 0/15 | 11/15 | 3.5 s | 21 |
| F3 ECD | 0/15 | 0/15 | 0/15 | 13/15 | 2.3 s | 20 |
| F4 ECD driver | 0/15 | 0/15 | 0/15 | 14/15 | 1.5 s | 23 |

**Qwen2.5-0.5B**

| Condition | Answers with untraceable numbers | Hypotheses not in the record | Overclaims | Covers every conclusion | p50 latency | Generated tokens |
|---|---|---|---|---|---|---|
| F1 free | 3/15 | 2/15 | 1/15 | 12/15 | 6.7 s | 116 |
| F2 static | 0/15 | 7/15 | 0/15 | 15/15 | 3.3 s | 16 |
| F3 ECD | 0/15 | 0/15 | 0/15 | 15/15 | 1.9 s | 15 |
| F4 ECD driver | 0/15 | 0/15 | 0/15 | 13/15 | 1.6 s | 16 |

The deterministic default (no model) covers 15/15 with zero violations by construction.

## What this shows, and what it does not

1. **ECD removed every contract violation observed.** Free generation produced invented numbers, hypotheses absent from the evidence and causal wording without a counterfactual (Qwen). A static grammar prevented invented numbers but still let the model name hypotheses the record does not contain (Qwen 7/15). Compiling the grammar from the record removed those. This matches the novelty claim in solution_journey §8.11, now with a measurement behind it.
2. **The guarantee is structural.** The property tests (about 1,000 random records and choices) cover cases the experiment did not sample.
3. **The model adds little here.** For these records the deterministic choice is complete, instant and as good or better on coverage. The SLM's possible value is choosing what is relevant to a specific question; that was not measured (no relevance judgments exist).
4. **Latency is high.** On the x86 proxy an SLM choice costs 1.5-2.3 s, because the record-specific option list (about 100-240 prompt tokens) cannot be cached. On ARM this will be slower. Decision: **deterministic choice is the default; SLM choice is opt-in** until it is measured on the target and shown to help.
5. **Free-text quality is poor at this model size.** SmolLM2 free answers were often unusable (e.g. "1 / / A:"), which supports not letting a model of this size write answers at all.
6. **Small sample and detector limits:** 15 records. The F1 detectors (number matching, hypothesis keywords, causal verbs) are deliberately simple and may miss or over-count; an earlier run over-counted list markers ("1.", "2.") as numbers until the detector was fixed.

## Bugs found by the property tests

- **Digits in fixed wording:** a feature label "(1 = as designed)" and a unit "per 10⁴ A²" put digits into answers that came from no slot, a C1 violation in the renderer's own text. The labels were changed, and a test now fails on any digit in fixed wording.

## Open items

- Readability rating by people (driver and technician wording).
- Relevance of SLM choices to the question (needs judged examples).
- Combined compiled grammar + forced-token handling inside llama.cpp, to cut latency.
- ARM-target measurement of F3/F4.
- Roles currently shape diagnosis answers only; P6 extends role scoping (location, identity) to all answers.
