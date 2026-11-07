"""DriveTrace local web UI (FR-UI-01..03) - the *assistant* process.

    python -m uvicorn ui.app:app --app-dir drivetrace --host 127.0.0.1 --port 8765

The assistant only reads the canonical store. It binds to 127.0.0.1, installs the egress guard (no outbound
connections except loopback), exposes no API docs, refuses location signals, and never touches CAN or
any vehicle interface - in deployment it runs in its own container with read-only data and no network
(deploy/compose.yaml).
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from security import egress

egress.install()  # before anything else can open a connection

from diagnosis.window import COOLANT, CURRENT, PUMP_RPM, T_AVG, T_MAX  # noqa: E402
from ecd.compiler import LEXICON  # noqa: E402
from memory.store import Store  # noqa: E402
from query.engine import QueryEngine  # noqa: E402
from query.planner import SlmPlanner  # noqa: E402
from security.privacy import ROLES, check_role, signal_allowed  # noqa: E402

STATIC = Path(__file__).with_name("static")
MAX_POINTS = 600

app = FastAPI(title="DriveTrace", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
store = Store()

slm_host = os.environ.get("DRIVETRACE_SLM_HOST", "127.0.0.1")
slm_port = int(os.environ.get("DRIVETRACE_SLM_PORT", "8089"))
slm_env = os.environ.get("DRIVETRACE_SLM", "").strip().lower()
planner = None
if slm_env in ("1", "true", "yes", "on") or os.environ.get("DRIVETRACE_SLM_HOST"):
    planner = SlmPlanner(host=slm_host, port=slm_port)
else:
    p = SlmPlanner(host=slm_host, port=slm_port)
    if p.available():
        planner = p
engine = QueryEngine(store, planner=planner)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    source: str
    role: str = "service"


def _ms(t) -> int:
    return int(t.timestamp() * 1000)


def _series(key: str, path: str, start: datetime, end: datetime) -> list[list]:
    rows = store.query(f"""SELECT epoch_ms(ts), value FROM {store.signals(key)}
                           WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) AND signal_path = ?
                             AND ts BETWEEN ? AND ? ORDER BY ts""", [start, end, path, start, end])
    step = max(1, len(rows) // MAX_POINTS)
    return [[int(t), round(float(v), 3)] for t, v in rows[::step]]


def _charts(answer, role: str) -> list[dict]:
    r = answer.result
    if r is None:
        return []
    plan = answer.plan or {}
    if plan.get("intent") == "trend" and r.data.get("series"):
        return [{"title": r.data["label"], "unit": r.data["unit"], "x": "day",
                 "points": [[str(d), round(float(v), 3)] for d, v in r.data["series"]]}]
    if plan.get("intent") == "count_events" and r.data.get("by_day"):
        return [{"title": "events per day", "unit": "", "x": "day", "kind": "bar",
                 "points": [[str(d), n] for d, n in r.data["by_day"]]}]
    rec = r.data.get("record") if plan.get("intent") == "diagnose_event" else None
    if rec:
        anchor = datetime.fromisoformat(rec["anchor"]["ts"])
        start = datetime.fromisoformat(rec["target_window"]["start"]) - timedelta(minutes=5)
        key = rec["source"]
        out = []
        signals = [(T_AVG, "battery average temperature", "°C"), (T_MAX, "battery maximum temperature", "°C"),
                   (CURRENT, "battery current (+ = charging)", "A"), (PUMP_RPM, "coolant pump speed", "rpm"),
                   (COOLANT, "coolant outlet temperature", "°C")]
        if role == "driver":  # drivers see the temperature only, like the answer text
            signals = signals[:1]
        for path, title, unit in signals:
            pts = _series(key, path, start, anchor + timedelta(minutes=5))
            if pts:
                out.append({"title": title, "unit": unit, "x": "time", "points": pts, "marker": _ms(anchor),
                            "tz": store.source(key).timezone})  # axis in the vehicle's local time, like the answer
        return out
    return []


def _details(answer, role: str) -> dict:
    r = answer.result
    rec = r.data.get("record") if r is not None else None
    if not rec:
        return {}
    def label(c):
        spec = LEXICON["claims"].get(c["hypothesis"], {})
        return spec.get("driver", c["label"]) if role == "driver" else c["label"]

    return {"claims": [{"label": label(c), "level": c["level"], "verdict": (c.get("counterfactual") or {}).get("verdict")}
                       for c in rec["claims"]],
            "gate": rec["gate"]["status"], "baseline": {"status": rec["baseline"]["status"],
                                                        "sessions": [w["ref"] for w in rec["baseline"]["windows"]]},
            "record_version": rec["record_version"]}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/sources")
def sources():
    out = []
    for key, s in store.sources.items():
        ctx = engine.executor.context(key)
        out.append({"key": key, "dataset": s.dataset, "source_type": s.source_type, "timezone": s.timezone,
                    "first": ctx.first.isoformat(), "last": ctx.now.isoformat()})
    return {"sources": out, "roles": list(ROLES)}


@app.post("/api/ask")
def ask(req: AskRequest):
    try:
        check_role(req.role)
        store.source(req.source)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    a = engine.ask(req.question, req.source, req.role)
    return {"text": a.text, "ok": a.ok, "tier": a.tier, "rule": a.rule, "plan": a.plan, "notes": a.notes,
            "evidence": a.evidence[:20], "timings_ms": a.timings_ms, "total_ms": round(a.total_ms, 1),
            "details": _details(a, req.role), "charts": _charts(a, req.role)}


@app.get("/api/series")
def series(source: str, path: str, start: datetime, end: datetime = Query(...)):
    if not signal_allowed(path):
        raise HTTPException(403, "this signal is not available through the assistant")
    try:
        store.source(source)
    except KeyError as e:
        raise HTTPException(400, str(e))
    if end - start > timedelta(days=2):
        raise HTTPException(400, "window too long (max 2 days)")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    return {"path": path, "points": _series(source, path, start, end)}
