"""Tier 1 SLM query planner (FR-QRY-02): fallback for questions the Tier 0 rules cannot route.

Talks to a local llama-server on 127.0.0.1 with a grammar from query/vocabulary.py. Its output is parsed
and then validated by query/plan.py like any other plan. G0 (docs/g0_decision.md): this path is a
fallback with its own latency class, not part of the "under 2 s" common-query claim.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from query.grammar import compact_grammar, json_grammar
from query.plan import Plan, PlanError, from_dict
from query.vocabulary import KEYS

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "runtime" / "llama-b11606" / "llama-server.exe"
DEFAULT_MODEL = ROOT / "models" / "SmolLM2-360M-Instruct-Q4_K_M.gguf"

SYSTEM = (
    "You convert a driver's question about their vehicle into a query plan. "
    "Use only the allowed values. Use intent unsupported for anything that is not a question about vehicle data.\n"
)
# One example per intent; none is a test-set question. Kept identical to bench/plan_latency.py.
EXAMPLES = [
    ("Show all ADAS disengagement events from yesterday.", ["list_events", "none", "yesterday", "none", "adas_disengaged", "latest"]),
    ("How many battery overheating warnings did I get this month?", ["count_events", "none", "this_month", "none", "battery_overtemp_warning", "latest"]),
    ("When did my efficiency drop the most this month?", ["max_drop", "energy_consumption", "this_month", "day", "none", "latest"]),
    ("How has my state of charge on arrival changed over the last week?", ["trend", "state_of_charge", "last_7_days", "day", "none", "latest"]),
    ("Is my range lower this month than last month?", ["compare_periods", "range", "this_month", "none", "none", "latest"]),
    ("Compare energy consumption between city and highway driving.", ["compare_groups", "energy_consumption", "all", "road_type", "none", "latest"]),
    ("What was my state of charge when I last started charging?", ["point_lookup", "state_of_charge", "all", "none", "charging", "latest"]),
    ("Why did the warning lamp come on today?", ["diagnose_event", "none", "today", "none", "warning_lamp", "latest"]),
    ("Open the windows.", ["unsupported", "none", "all", "none", "none", "latest"]),
]


class PlannerUnavailable(RuntimeError):
    pass


class SlmPlanner:
    def __init__(self, port: int = 8089, fmt: str = "json", timeout_s: float = 20.0):
        self.url = f"http://127.0.0.1:{port}"
        self.fmt = fmt
        self.timeout_s = timeout_s
        self.grammar = json_grammar() if fmt == "json" else compact_grammar()
        self.last_timings: dict = {}

    def _render(self, values: list[str]) -> str:
        if self.fmt == "compact":
            return " ".join(values)
        return "{" + ", ".join(f'"{k}": "{v}"' for k, v in zip(KEYS, values)) + "}"

    def prompt(self, question: str) -> str:
        # The question is user text: it is quoted as data inside the prompt and the grammar limits the output,
        # so instructions inside it cannot produce anything outside the plan vocabulary.
        shots = "".join(f"Question: {q}\nPlan: {self._render(v)}\n" for q, v in EXAMPLES)
        question = question.replace("\n", " ").strip()[:300]
        return f"{SYSTEM}{shots}Question: {question}\nPlan: "

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=1) as r:
                return json.load(r).get("status") == "ok"
        except (OSError, ValueError):
            return False

    def plan(self, question: str) -> Plan:
        body = {"prompt": self.prompt(question), "grammar": self.grammar, "n_predict": 64, "temperature": 0,
                "cache_prompt": True}
        req = urllib.request.Request(f"{self.url}/completion", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                data = json.load(r)
        except (OSError, urllib.error.URLError) as e:
            raise PlannerUnavailable(str(e)) from e
        self.last_timings = data.get("timings", {})
        text = data.get("content", "").strip()
        if self.fmt == "compact":
            parts = text.split()
            if len(parts) != len(KEYS):
                raise PlanError(f"planner output is not a plan: {text!r}")
            return from_dict(dict(zip(KEYS, parts)))
        try:
            return from_dict(json.loads(text))
        except json.JSONDecodeError as e:
            raise PlanError(f"planner output is not JSON: {text!r}") from e


class LocalServer:
    """Starts llama-server bound to 127.0.0.1 (context manager). Used by benches and the CLI."""

    def __init__(self, model: Path = DEFAULT_MODEL, port: int = 8089, threads: int = 2):
        self.cmd = [str(SERVER), "-m", str(model), "-t", str(threads), "-tb", str(threads), "-c", "2048",
                    "-np", "1", "--host", "127.0.0.1", "--port", str(port)]
        self.port = port
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        planner = SlmPlanner(self.port)
        t0 = time.perf_counter()
        while not planner.available():
            if self.proc.poll() is not None or time.perf_counter() - t0 > 120:
                raise PlannerUnavailable("llama-server did not start")
            time.sleep(0.2)
        # warm the prompt cache with the shared few-shot prefix
        try:
            planner.plan("warm up")
        except PlanError:
            pass
        return self

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=30)
