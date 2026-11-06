# Demo script - about 4 minutes

**Setup:** start the isolated deployment (`docker compose -f deploy/compose.yaml up -d`) or the native server, and open http://127.0.0.1:8765. Select the vehicle `hist6w_s42` and the role *Service*. Every number shown on screen comes from the stored vehicle data.

| Time | On screen | What to say |
|---|---|---|
| 0:00-0:20 | Header pill "offline · local only"; a terminal running `sh deploy/check_isolation.sh` ending in `all isolation checks passed` | "Everything runs on the vehicle's own computer. The assistant cannot reach the internet, write the data, or touch the CAN bus - and we prove that with tests, not configuration. Our first network design actually leaked; the tests caught it." |
| 0:20-1:20 | Click **Show all ADAS disengagement events from yesterday**, then **Compare energy consumption between city and highway driving**, then **When did fuel efficiency show the largest drop?** Point at the latency card (about 15 ms). | "Common questions are answered by deterministic rules over a local DuckDB store in milliseconds - well inside the 2-second budget. No language model is needed for these, which is a decision we measured (G0), not assumed." |
| 1:20-2:20 | Click **What happened before the battery overheating warning?** Scroll to the evidence panel (claims with "cause (model-checked)" badges) and the charts with the red event marker. | "This is the diagnostic path. We compare the event with matched earlier charging sessions, run data-quality checks, score explicit hypotheses, and replay a physical battery model with each fault removed. Both high load and reduced cooling were but-for causes here. Each conclusion carries its evidence level." |
| 2:20-3:10 | Open a terminal and show `bench/results/experiment_f_test.json` summary (or the slide): free text vs ECD. Optionally run `python -m query.ask --slm ...`. | "Our novelty is Evidence-Compiled Decoding. The language model never writes the answer: it can only choose items from a grammar compiled from this diagnosis. On 17 held-out cases, free generation invented numbers or causes; ECD had zero violations, by construction." |
| 3:10-3:40 | Switch the role to **Driver** and ask the same diagnosis question. | "The same evidence, for a driver: plain words, no technical numbers, and no location or identity for any role." |
| 3:40-4:00 | Click **Brake hard now**. | "And it never controls the vehicle - control requests are refused for every role, before any model runs." |

**Backup if the live demo fails:** show the same questions with `python -m query.ask` in a terminal, and the tables in `docs/p7_results.md`.

**What not to claim:**
- Timings are from an x86 laptop, not the ARM target.
- Diagnosis accuracy is on synthetic faults.
- ATTRIBUTED causes are model-scoped: the model is the simulator's own.
