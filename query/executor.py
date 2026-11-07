"""Allowlisted plan executor (FR-QRY-03, FR-ANA-01).

Each intent maps to one handler with fixed, parameterised SQL over the memory layers built by
memory/build.py. Plan values never reach SQL text: they are looked up in fixed dictionaries
(query/vocabulary.py) and passed as bound parameters. The executor only reads.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from memory.store import Store
from query.plan import Plan, PlanError
from query.timeparse import Window, previous, resolve
from query.vocabulary import EVENT_TYPE_MAP, SIGNAL_PATHS
from security.privacy import scrub_attrs

P = "Vehicle.Powertrain.TractionBattery."
V = "Vehicle.VendorExt.TractionBattery.Cooling."
DIAG_SIGNALS = [  # shown in the factual pre-event timeline, when the vehicle records them
    P + "Temperature.Average", P + "Temperature.Max", P + "CurrentCurrent", P + "StateOfCharge.Current",
    V + "PumpCommand", V + "PumpSpeed", V + "CoolantOutletTemperature", "Vehicle.Exterior.AirTemperature",
    "Vehicle.Speed", "Vehicle.Powertrain.CombustionEngine.ECT",
]
MIN_DAY_KM = 10.0     # days with less driving are too noisy for efficiency comparisons
MIN_TRIP_KM = 0.5
HIGHWAY_SHARE = 0.5   # a trip is "highway" if at least half of its moving time is above 70 km/h
LOOKUP_WINDOW = timedelta(minutes=10)
PRE_EVENT = timedelta(minutes=30)


@dataclass
class Result:
    intent: str
    window: Window
    data: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Context:
    key: str
    tz: str
    now: datetime
    first: datetime
    signals: frozenset
    event_types: frozenset
    has_energy: bool
    has_fuel: bool


class Executor:
    def __init__(self, store: Store):
        self.store = store
        self._ctx: dict[str, Context] = {}
        self._diagnoser = None

    @property
    def diagnoser(self):
        if self._diagnoser is None:
            from diagnosis.engine import Diagnoser
            self._diagnoser = Diagnoser(self.store)
        return self._diagnoser

    # ------------------------------------------------------------------ context
    def context(self, key: str) -> Context:
        if key not in self._ctx:
            s = self.store
            now, first = s.query(f"SELECT max(last_ts), min(first_ts) FROM {s.aggregates(key, '1 day')}")[0]
            signals = frozenset(r[0] for r in s.query(f"SELECT DISTINCT signal_path FROM {s.aggregates(key, '1 day')}"))
            events = frozenset(r[0] for r in s.query(f"SELECT DISTINCT event_type FROM {s.event_index(key)}"))
            energy, fuel = s.query(f"""SELECT count(*) FILTER (WHERE battery_wh IS NOT NULL AND km > {MIN_TRIP_KM}),
                                              count(*) FILTER (WHERE fuel_l_est > 0 AND km > {MIN_TRIP_KM})
                                       FROM {s.trips(key)}""")[0]
            self._ctx[key] = Context(key, s.source(key).timezone, now, first, signals, events, energy > 0, fuel > 0)
        return self._ctx[key]

    def run(self, key: str, plan: Plan) -> Result:
        ctx = self.context(key)
        window = resolve(plan.period, ctx.now, ctx.tz, ctx.first)
        handler = HANDLERS.get(plan.intent)
        if handler is None:
            raise PlanError(f"intent {plan.intent!r} is not executable")
        return handler(self, ctx, plan, window)

    # ------------------------------------------------------------------ helpers
    def _local_day(self, ctx: Context, col: str) -> str:
        return f"CAST(timezone('{ctx.tz}', {col}) AS DATE)"

    def _consumption(self, ctx: Context, signal: str, notes: list[str]) -> tuple[str, str, str]:
        """(SQL expression over trips, unit, label) for energy or fuel use."""
        energy = ("sum(battery_wh) / nullif(sum(km), 0)", "Wh/km", "energy consumption")
        fuel = ("100.0 * sum(fuel_l_est) / nullif(sum(km), 0)", "l/100 km", "fuel consumption")
        want_fuel = signal == "fuel_consumption"
        if want_fuel and ctx.has_fuel:
            return fuel
        if not want_fuel and ctx.has_energy:
            if signal == "range":
                notes.append("this vehicle's range follows its energy consumption, so the answer uses energy consumption")
            return energy
        if ctx.has_energy:
            notes.append("this vehicle has no fuel data; the answer uses battery energy consumption instead")
            return energy
        if ctx.has_fuel:
            notes.append("this vehicle has no battery energy data; the answer uses estimated fuel consumption instead")
            return fuel
        raise PlanError("this vehicle records neither battery energy nor fuel flow")

    def _event_type(self, ctx: Context, plan: Plan, notes: list[str]) -> str:
        etype = EVENT_TYPE_MAP[plan.event_type]
        if etype not in ctx.event_types:
            notes.append(f"this vehicle's data never contains {etype} events, so zero does not mean it was checked")
        return etype

    def _signal_path(self, ctx: Context, signal: str) -> str:
        path = SIGNAL_PATHS.get(signal)
        if path is None or path not in ctx.signals:
            raise PlanError(f"this vehicle does not record {signal.replace('_', ' ')}")
        return path

    def _value_at(self, ctx: Context, path: str, ts: datetime) -> tuple[float, datetime] | None:
        rows = self.store.query(
            f"""SELECT arg_max(value, ts), max(ts) FROM {self.store.signals(ctx.key)}
                WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) AND signal_path = ? AND ts BETWEEN ? AND ?""",
            [ts - LOOKUP_WINDOW, ts, path, ts - LOOKUP_WINDOW, ts])
        return (rows[0][0], rows[0][1]) if rows and rows[0][1] is not None else None

    def _trips(self, key: str) -> str:
        return f"(SELECT * FROM {self.store.trips(key)} WHERE km > {MIN_TRIP_KM})"

    def _charging_session_window(self, ctx: Context, start_ts: datetime) -> tuple[datetime, datetime] | None:
        rows = self.store.query(
            f"""SELECT ts FROM {self.store.event_index(ctx.key)}
                WHERE event_type = 'CHARGING_STOPPED' AND ts > ? ORDER BY ts LIMIT 1""", [start_ts])
        if not rows:
            return None
        return start_ts, rows[0][0]


# ---------------------------------------------------------------------- handlers
def list_events(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    r = Result(plan.intent, w)
    etype = ex._event_type(ctx, plan, r.notes)
    rows = ex.store.query(
        f"""SELECT event_id, timezone('{ctx.tz}', ts), trip_no, attrs FROM {ex.store.event_index(ctx.key)}
            WHERE event_type = ? AND ts >= ? AND ts < ? ORDER BY ts""", [etype, w.start, w.end])
    r.data = {"total": len(rows), "events": [
        {"event_id": e, "local_time": t, "trip_no": trip, "attrs": scrub_attrs(json.loads(a or "{}"))} for e, t, trip, a in rows[:20]]}
    r.evidence = [e for e, *_ in rows[:20]]
    return r


def count_events(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    r = Result(plan.intent, w)
    etype = ex._event_type(ctx, plan, r.notes)
    rows = ex.store.query(
        f"""SELECT {ex._local_day(ctx, 'ts')} AS d, count(*), min(event_id) FROM {ex.store.event_index(ctx.key)}
            WHERE event_type = ? AND ts >= ? AND ts < ? GROUP BY d ORDER BY d""", [etype, w.start, w.end])
    r.data = {"total": sum(n for _, n, _ in rows), "by_day": [(d, n) for d, n, _ in rows]}
    r.evidence = [f"{etype} on {d}" for d, _, _ in rows[:10]]
    return r


def max_drop(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    r = Result(plan.intent, w)
    expr, unit, label = ex._consumption(ctx, plan.signal, r.notes)
    rows = ex.store.query(f"""
        WITH d AS (
            SELECT {ex._local_day(ctx, 't_on')} AS day, {expr} AS v, sum(km) AS km,
                   sum(ambient_c * duration_s) / nullif(sum(duration_s) FILTER (WHERE ambient_c IS NOT NULL), 0) AS amb,
                   sum(heater_w * duration_s) / nullif(sum(duration_s) FILTER (WHERE heater_w IS NOT NULL), 0) AS heater,
                   sum(highway_share * duration_s) / nullif(sum(duration_s), 0) AS hwy,
                   min(trip_no) AS t0, max(trip_no) AS t1
            FROM {ex._trips(ctx.key)} WHERE t_on >= ? AND t_on < ? GROUP BY day
            HAVING sum(km) >= {MIN_DAY_KM} AND (day < ? OR ?)),
        j AS (SELECT *, lag(v) OVER (ORDER BY day) AS prev_v, lag(day) OVER (ORDER BY day) AS prev_day FROM d)
        SELECT day, v, prev_day, prev_v, v - prev_v AS jump, km, amb, heater, hwy, t0, t1,
               (SELECT count(*) FROM d) AS n_days
        FROM j WHERE prev_v IS NOT NULL ORDER BY jump DESC LIMIT 1""",
        [w.start, w.end, ctx.now.astimezone(ZoneInfo(ctx.tz)).date(), plan.period == "today"])
    if plan.period != "today":
        r.notes.append("today is excluded because it is still in progress")
    if not rows:
        r.data = {"found": False, "unit": unit, "label": label}
        return r
    day, v, pday, pv, jump, km, amb, heater, hwy, t0, t1, n = rows[0]
    r.data = {"found": True, "unit": unit, "label": label, "day": day, "value": v, "prev_day": pday, "prev_value": pv,
              "jump": jump, "km": km, "ambient_c": amb, "heater_w": heater, "highway_share": hwy, "days_compared": n}
    r.evidence = [f"trips {t0}-{t1} on {day}"]
    return r


def trend(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    r = Result(plan.intent, w)
    if plan.signal in ("energy_consumption", "fuel_consumption") or (
            plan.signal == "range" and SIGNAL_PATHS["range"] not in ctx.signals):
        expr, unit, label = ex._consumption(ctx, plan.signal, r.notes)
        series = ex.store.query(f"""
            SELECT {ex._local_day(ctx, 't_on')} AS day, {expr} AS v FROM {ex._trips(ctx.key)}
            WHERE t_on >= ? AND t_on < ? GROUP BY day HAVING sum(km) >= {MIN_DAY_KM} ORDER BY day""", [w.start, w.end])
    else:
        path = ex._signal_path(ctx, plan.signal)
        unit = UNITS.get(plan.signal, "")
        label = plan.signal.replace("_", " ")
        series = ex.store.query(f"""
            SELECT {ex._local_day(ctx, 'window_start')} AS day, sum(mean * n) / sum(n) AS v
            FROM {ex.store.aggregates(ctx.key)} WHERE signal_path = ? AND window_start >= ? AND window_start < ?
            GROUP BY day ORDER BY day""", [path, w.start, w.end])
        if plan.signal == "speed":
            r.notes.append("daily mean of all speed samples, including stops")
    r.data = {"unit": unit, "label": label, "series": series}
    if len(series) >= 2:
        xs = [(d - series[0][0]).days for d, _ in series]
        ys = [v for _, v in series]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
        r.data.update(first=series[0], last=series[-1], slope_per_week=slope * 7, mean=my,
                      min=min(series, key=lambda s: s[1]), max=max(series, key=lambda s: s[1]))
    r.evidence = [f"{len(series)} daily values, {series[0][0]} to {series[-1][0]}"] if series else []
    return r


def _period_stats(ex: Executor, ctx: Context, expr: str, w: Window):
    return ex.store.query(f"""
        SELECT count(*) AS trips, sum(km) AS km, {expr} AS v,
               sum(ambient_c * duration_s) / nullif(sum(duration_s) FILTER (WHERE ambient_c IS NOT NULL), 0) AS amb,
               sum(heater_w * duration_s) / nullif(sum(duration_s) FILTER (WHERE heater_w IS NOT NULL), 0) AS heater,
               sum(ac_w * duration_s) / nullif(sum(duration_s) FILTER (WHERE ac_w IS NOT NULL), 0) AS ac,
               sum(highway_share * duration_s) / nullif(sum(duration_s), 0) AS hwy,
               sum(avg_moving_kmh * km) / nullif(sum(km), 0) AS kmh, min(trip_no), max(trip_no)
        FROM {ex._trips(ctx.key)} WHERE t_on >= ? AND t_on < ?""", [w.start, w.end])[0]


STAT_KEYS = ["trips", "km", "value", "ambient_c", "heater_w", "ac_w", "highway_share", "avg_kmh", "trip_from", "trip_to"]


def compare_periods(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    r = Result(plan.intent, w)
    prev = previous(w, plan.period, ctx.tz)
    if plan.signal in ("speed", "battery_temperature", "state_of_charge"):
        path = ex._signal_path(ctx, plan.signal)
        vals = []
        for win in (w, prev):
            vals.append(ex.store.query(f"""SELECT sum(mean * n) / nullif(sum(n), 0) FROM {ex.store.aggregates(ctx.key)}
                                            WHERE signal_path = ? AND window_start >= ? AND window_start < ?""",
                                       [path, win.start, win.end])[0][0])
        r.data = {"mode": "signal", "label": plan.signal.replace("_", " "), "unit": UNITS[plan.signal],
                  "current": vals[0], "previous": vals[1], "previous_window": prev}
        return r
    expr, unit, label = ex._consumption(ctx, plan.signal, r.notes)
    cur = dict(zip(STAT_KEYS, _period_stats(ex, ctx, expr, w)))
    old = dict(zip(STAT_KEYS, _period_stats(ex, ctx, expr, prev)))
    r.data = {"mode": "consumption", "label": label, "unit": unit, "asked": plan.signal,
              "current": cur, "previous": old, "previous_window": prev}
    for s in (cur, old):
        if s["trips"]:
            r.evidence.append(f"trips {s['trip_from']}-{s['trip_to']}")
    return r


def compare_groups(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    r = Result(plan.intent, w)
    if plan.signal == "speed":
        expr, unit, label = "sum(avg_moving_kmh * km) / nullif(sum(km), 0)", "km/h", "average moving speed"
    else:
        expr, unit, label = ex._consumption(ctx, plan.signal, r.notes)
    rows = ex.store.query(f"""
        SELECT CASE WHEN highway_share >= {HIGHWAY_SHARE} THEN 'highway' ELSE 'city' END AS grp,
               count(*), sum(km), {expr}
        FROM {ex._trips(ctx.key)} WHERE t_on >= ? AND t_on < ? GROUP BY grp ORDER BY grp""", [w.start, w.end])
    r.data = {"label": label, "unit": unit, "asked": plan.signal,
              "groups": {g: {"trips": n, "km": km, "value": v} for g, n, km, v in rows}}
    r.notes.append("trips count as highway when at least half of their moving time is above 70 km/h")
    return r


def point_lookup(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    r = Result(plan.intent, w)
    event = None
    if plan.event_type != "none":
        etype = ex._event_type(ctx, plan, r.notes)
        rows = ex.store.query(f"""SELECT event_id, ts, trip_no FROM {ex.store.event_index(ctx.key)}
                                  WHERE event_type = ? AND ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1""",
                              [etype, w.start, w.end])
        if not rows:
            r.data = {"found": False, "reason": "no_event"}
            return r
        event = {"event_id": rows[0][0], "ts": rows[0][1], "trip_no": rows[0][2]}
        r.evidence.append(rows[0][0])

    agg = plan.aggregate
    if agg in ("duration", "count"):
        if plan.event_type != "charging":
            raise PlanError("duration and count are supported only for charging events")
        if event is None:
            r.data = {"found": False, "reason": "no_event"}
            return r
        session = ex._charging_session_window(ctx, event["ts"])
        if not session:
            r.data = {"found": False, "reason": "missing_charging_stop"}
            return r
        start, end = session
        seconds = (end - start).total_seconds()
        r.data = {"found": True, "kind": "aggregate", "aggregate": agg, "label": "charging session",
                  "value": seconds / 60.0 if agg == "duration" else 1, "unit": "minutes" if agg == "duration" else "",
                  "start_local_time": start.astimezone(ZoneInfo(ctx.tz)), "end_local_time": end.astimezone(ZoneInfo(ctx.tz)),
                  "event": event}
        return r

    if agg in ("mean", "max", "min"):
        if plan.signal in ("energy_consumption", "fuel_consumption"):
            raise PlanError(f"aggregate {agg} is not available for {plan.signal.replace('_', ' ')}")
        path = ex._signal_path(ctx, plan.signal)
        win_start, win_end = w.start, w.end
        if event and plan.event_type == "charging":
            session = ex._charging_session_window(ctx, event["ts"])
            if not session:
                r.data = {"found": False, "reason": "missing_charging_stop"}
                return r
            win_start, win_end = session
            r.evidence.append("charging session window")
        op = {"mean": "avg(value)", "max": "max(value)", "min": "min(value)"}[agg]
        rows = ex.store.query(
            f"""SELECT {op}, max(ts) FROM {ex.store.signals(ctx.key)}
                WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                  AND signal_path = ? AND ts >= ? AND ts < ?""",
            [win_start, win_end, path, win_start, win_end])
        value, ts = rows[0] if rows else (None, None)
        if ts is None:
            r.data = {"found": False, "reason": "no_sample"}
            return r
        if plan.signal == "range":
            value = value / 1000.0
        r.data = {"found": True, "kind": "aggregate", "aggregate": agg, "label": plan.signal.replace("_", " "),
                  "unit": UNITS[plan.signal], "value": value, "sample_local_time": ts.astimezone(ZoneInfo(ctx.tz)),
                  "event": event, "window": (win_start.astimezone(ZoneInfo(ctx.tz)), win_end.astimezone(ZoneInfo(ctx.tz)))}
        return r

    if plan.signal in ("energy_consumption", "fuel_consumption"):
        expr, unit, label = ex._consumption(ctx, plan.signal, r.notes)
        if event and event["trip_no"] is not None:
            where, args = "trip_no = ?", [event["trip_no"]]
        else:
            where, args = "t_on < ?", [event["ts"] if event else w.end]
        rows = ex.store.query(f"""SELECT trip_no, t_on, km, {expr} FROM {ex._trips(ctx.key)} WHERE {where}
                                  GROUP BY ALL ORDER BY t_on DESC LIMIT 1""", args)
        if not rows:
            r.data = {"found": False, "reason": "no_trip"}
            return r
        trip, t_on, km, v = rows[0]
        r.data = {"found": True, "kind": "trip", "label": label, "unit": unit, "value": v, "trip_no": trip,
                  "local_time": t_on.astimezone(ZoneInfo(ctx.tz)),
                  "km": km, "event": event}
        r.evidence.append(f"trip {trip}")
        return r
    path = ex._signal_path(ctx, plan.signal)
    at = event["ts"] if event else ctx.now
    found = ex._value_at(ctx, path, at)
    if found is None:
        r.data = {"found": False, "reason": "no_sample"}
        return r
    value, ts = found
    if plan.signal == "range":
        value = value / 1000.0  # m -> km
    r.data = {"found": True, "kind": "signal", "label": plan.signal.replace("_", " "), "unit": UNITS[plan.signal],
              "value": value, "sample_local_time": ts.astimezone(ZoneInfo(ctx.tz)), "event": event,
              "is_latest": event is None}
    return r


def summary_health(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    r = Result(plan.intent, w)
    q = f"""SELECT event_type, count(*) FROM {ex.store.event_index(ctx.key)}
            WHERE ts >= ? AND ts < ? AND event_type IN ('BATT_OVERTEMP_WARNING_SET', 'WARNING_LAMP_ON', 'ADAS_DISENGAGED')
            GROUP BY event_type"""
    rows = dict(ex.store.query(q, [w.start, w.end]))
    latest = ex.store.query(
        f"""SELECT event_type, ts, event_id FROM {ex.store.event_index(ctx.key)}
            WHERE ts >= ? AND ts < ? AND event_type IN ('BATT_OVERTEMP_WARNING_SET', 'WARNING_LAMP_ON')
            ORDER BY ts DESC LIMIT 1""", [w.start, w.end])
    latest_warning = None
    if latest:
        etype, ts, eid = latest[0]
        latest_warning = {"event_type": etype, "ts": ts.astimezone(ZoneInfo(ctx.tz)), "event_id": eid}
        r.evidence.append(eid)
    r.data = {
        "warnings": rows.get("BATT_OVERTEMP_WARNING_SET", 0),
        "lamps": rows.get("WARNING_LAMP_ON", 0),
        "adas": rows.get("ADAS_DISENGAGED", 0),
        "latest_warning": latest_warning,
    }
    return r


def diagnose_event(ex: Executor, ctx: Context, plan: Plan, w: Window) -> Result:
    """Factual timeline before the event plus the P4 evidence record (baseline, gate, hypotheses, counterfactual)."""
    r = Result(plan.intent, w)
    etype = ex._event_type(ctx, plan, r.notes)
    rows = ex.store.query(f"""SELECT event_id, ts, trip_no, attrs FROM {ex.store.event_index(ctx.key)}
                              WHERE event_type = ? AND ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1""",
                          [etype, w.start, w.end])
    if not rows:
        r.data = {"found": False}
        return r
    event_id, ts, trip_no, attrs = rows[0]
    paths = [p for p in DIAG_SIGNALS if p in ctx.signals]
    placeholders = ", ".join("?" for _ in paths)
    series = ex.store.query(f"""
        SELECT signal_path, arg_min(value, ts), arg_max(value, ts), min(value), max(value), count(*)
        FROM {ex.store.signals(ctx.key)}
        WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) AND ts BETWEEN ? AND ? AND signal_path IN ({placeholders})
        GROUP BY signal_path""", [ts - PRE_EVENT, ts, ts - PRE_EVENT, ts, *paths])
    before = ex.store.query(f"""SELECT event_type, timezone('{ctx.tz}', ts), attrs FROM {ex.store.event_index(ctx.key)}
                                WHERE ts >= ? AND ts < ? AND event_id <> ? ORDER BY ts""",
                            [ts - timedelta(minutes=60), ts, event_id])
    r.data = {"found": True, "event_id": event_id, "local_time": ts.astimezone(ZoneInfo(ctx.tz)), "trip_no": trip_no,
              "attrs": scrub_attrs(json.loads(attrs or "{}")),
              "signals": {p: {"start": a, "at_event": b, "min": lo, "max": hi, "samples": n}
                          for p, a, b, lo, hi, n in series},
              "events_before": [(t, tm, scrub_attrs(json.loads(a or "{}"))) for t, tm, a in before]}
    r.evidence = [event_id, f"signals {PRE_EVENT.seconds // 60} min before the event"]
    if etype == "BATT_OVERTEMP_WARNING_SET":
        rec = ex.diagnoser.diagnose_event(ctx.key, event_id)
        r.data["record"] = rec
        r.evidence += [b["ref"] for b in rec["baseline"]["windows"]]
    return r


UNITS = {"battery_temperature": "°C", "speed": "km/h", "state_of_charge": "%", "range": "km"}

HANDLERS = {
    "list_events": list_events, "count_events": count_events, "max_drop": max_drop, "trend": trend,
    "compare_periods": compare_periods, "compare_groups": compare_groups, "point_lookup": point_lookup,
    "diagnose_event": diagnose_event, "summary_health": summary_health,
}
