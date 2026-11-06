"""Ask DriveTrace a question from the command line.

    python -m query.ask --source hist6w_s42 "Why has my driving range reduced over the last two weeks?"
    python -m query.ask --source ved_455 --slm "what's going on with my efficiency lately"

--slm starts a local llama-server (SmolLM2, 127.0.0.1) for questions the Tier 0 rules cannot route.
"""

import argparse
import contextlib
import sys

from query.engine import QueryEngine
from query.planner import LocalServer, SlmPlanner


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question")
    ap.add_argument("--source", default="hist6w_s42")
    ap.add_argument("--slm", action="store_true", help="enable the Tier 1 SLM fallback")
    ap.add_argument("--role", default="service", choices=["driver", "service", "fleet"])
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    with LocalServer() if args.slm else contextlib.nullcontext():
        engine = QueryEngine(planner=SlmPlanner() if args.slm else None)
        a = engine.ask(args.question, args.source, args.role)
    print(a.text)
    print(f"\n[{a.tier} {a.rule}] plan={a.plan}")
    print("timings ms: " + ", ".join(f"{k} {v}" for k, v in a.timings_ms.items()) + f" | total {a.total_ms:.1f}")
    if a.evidence:
        print("evidence: " + "; ".join(a.evidence))


if __name__ == "__main__":
    main()
