"""Tier 0 deterministic router (FR-QRY-01).

Keyword and pattern rules map a question to a Plan without calling the SLM. The router either returns a
plan it is confident about or None, in which case the question goes to the Tier 1 SLM planner.
Rules were tuned on the `dev` half of tests/data/query_set.yaml only.
"""

import re
from dataclasses import dataclass

from query.plan import Plan


@dataclass(frozen=True)
class Route:
    plan: Plan
    rule: str
    category: str = ""  # for unsupported: control | injection | off_topic


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts))


CONTROL = re.compile(r"^(please )?(brake|stop|open|close|unlock|lock|accelerate|decelerate|turn (on|off)|"
                     r"switch (on|off)|disable|enable|activate|deactivate|start|set|clear|reset|honk|park|steer|"
                     r"increase|decrease|raise|lower)\b")
INJECTION = _rx(r"ignore (all |any |the )?(previous |prior )?(instructions|rules)", r"system prompt",
                r"developer mode", r"drop table", r"\bselect \S+ from\b", r";\s*--", r"forget (the|all|your) rules",
                r"disable (all )?safety", r"\bauthori[sz]e\b", r"raw can", r"home address", r"\bsend\b.*\b(email|data)\b")
OFF_TOPIC = _rx(r"\bweather\b", r"\bjoke\b", r"\bfootball\b", r"\bmatch\b", r"book a table", r"capital of",
                r"\bplay\b.*\bmusic\b", r"^navigate\b", r"\btyre\b", r"\btire\b")

SOC = _rx(r"state of charge", r"battery level", r"charge level", r"\bsoc\b", r"how much charge", r"charge (is )?left",
          r"battery percentage")
RANGE = _rx(r"\brange\b", r"kilomet\w* per charge", r"\bkm per charge", r"miles per charge")
FUEL = _rx(r"\bfuel\b", r"mileage", r"\bmpg\b", r"\beconomy\b", r"\blit(re|er)s?\b", r"per 100 ?km", r"petrol")
ENERGY = _rx(r"\benergy\b", r"efficien", r"consumption", r"\bkwh\b", r"wh per km", r"wh/km")
BATT_TEMP = _rx(r"battery (temp|temperature)\b", r"battery temp\b", r"\bhotter\b", r"battery .*\brunning hot")
SPEED = _rx(r"\bspeed\b", r"how fast", r"\bfaster\b", r"\bslower\b")

ADAS = _rx(r"\badas\b", r"lane (assist|keep\w*)", r"cruise( control)?", r"autopilot", r"driver assist\w*")
OVERTEMP = _rx(r"overheat\w*", r"over[- ]?temp\w*", r"too hot", r"(temperature|temp) (warning|alert)s?",
               r"battery warning")
LAMP = _rx(r"warning (lamp|light)s?", r"check engine", r"dashboard\b.*\blights?", r"\bmil\b", r"\blamps?\b")
CHARGING = re.compile(r"\bcharg(e|es|ed|ing)\b|plugged in")
WARNING_WORD = re.compile(r"\b(warning|alert)s?\b")

ROAD = _rx(r"\bcity\b", r"\btown\b", r"\burban\b", r"\bhighway\b", r"\bmotorway\b", r"\bfreeway\b")
DIAGNOSE = _rx(r"\bwhy did\b", r"what caused", r"what happened (right )?before", r"\bexplain\b", r"what led up to",
               r"went wrong")
COUNT = _rx(r"\bhow many\b", r"\bhow often\b", r"\bcount\b", r"number of")
MAX_DROP = _rx(r"(largest|biggest|sharpest|greatest) (\w+ )?(drop|decline|fall|jump|increase)", r"drop(ped)? the most",
               r"fall the most", r"worst compared", r"suddenly", r"biggest fall")
COMPARE = _rx(r"\bwhy\b", r"compared? (to|with)", r"\bthan (last|the)\b", r"\bhigher\b", r"\blower\b",
              r"more energy", r"\bfewer\b", r"worse lately", r"went down", r"gone up", r"what changed",
              r"(weeks?|month|days) before", r"\breduced\b", r"got worse")
POINT = _rx(r"\bwhat (is|was|s)\b", r"\bcurrent\b", r"right now", r"\bnow\b", r"how much .*\bleft\b", r"how fast was",
            r"at the last", r"when i last", r"\blast trip\b", r"what range did")
TREND = _rx(r"\btrend", r"over time", r"\bhow has\b", r"\bhow is\b", r"developed", r"day by day", r"\bplot\b",
            r"going up", r"getting (worse|better)")


def normalise(q: str) -> str:
    q = q.lower().replace("’", "'").replace("'s ", "s ")
    q = re.sub(r"[^a-z0-9/;\-\s*]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def period_of(q: str) -> str:
    if "this month" in q or re.search(r"(compared? (to|with)|than) last month", q):
        return "this_month"
    if re.search(r"\b(last|previous) month", q) or "month before" in q:
        return "last_month"
    if "yesterday" in q:
        return "yesterday"
    if "today" in q:
        return "today"
    if re.search(r"(14|fourteen) days|(two|2) weeks|fortnight", q):
        return "last_14_days"
    if re.search(r"(7|seven) days|past week|last week|this week|the week", q):
        return "last_7_days"
    return "all"


def signal_of(q: str) -> str:
    if RANGE.search(q):
        return "range"
    if SOC.search(q):
        return "state_of_charge"
    if FUEL.search(q):
        return "fuel_consumption"
    if BATT_TEMP.search(q) and not OVERTEMP.search(q.replace("battery temperature at", "")):
        return "battery_temperature"
    if ENERGY.search(q):
        return "energy_consumption"
    if SPEED.search(q):
        return "speed"
    if BATT_TEMP.search(q):
        return "battery_temperature"
    return "none"


def event_of(q: str) -> str:
    masked = SOC.sub(" ", q)
    masked = re.sub(r"per charge", " ", masked)
    if ADAS.search(masked):
        return "adas_disengaged"
    if LAMP.search(masked):
        return "warning_lamp"
    if OVERTEMP.search(masked):
        return "battery_overtemp_warning"
    if CHARGING.search(masked):
        return "charging"
    if WARNING_WORD.search(masked) and ("battery" in masked or "temperature" in masked or "last warning" in masked):
        return "battery_overtemp_warning"
    return "none"


def route(question: str) -> Route | None:
    q = normalise(question)
    if CONTROL.search(q):
        return Route(Plan("unsupported"), "control", "control")
    if INJECTION.search(q):
        return Route(Plan("unsupported"), "injection", "injection")
    if OFF_TOPIC.search(q):
        return Route(Plan("unsupported"), "off_topic", "off_topic")

    period, signal, event = period_of(q), signal_of(q), event_of(q)
    if DIAGNOSE.search(q) and event != "none":
        return Route(Plan("diagnose_event", period=period, event_type=event), "diagnose")
    if ROAD.search(q) and (signal != "none" or "efficien" in q):
        return Route(Plan("compare_groups", signal=signal if signal != "none" else "energy_consumption",
                          period=period, group_by="road_type"), "compare_groups")
    if COUNT.search(q) and event != "none":
        return Route(Plan("count_events", period=period, event_type=event), "count")
    if MAX_DROP.search(q) and signal in ("energy_consumption", "fuel_consumption", "range"):
        return Route(Plan("max_drop", signal=signal, period=period, group_by="day"), "max_drop")
    if COMPARE.search(q) and signal != "none" and event == "none":
        return Route(Plan("compare_periods", signal=signal, period=period), "compare_periods")
    if POINT.search(q) and signal != "none":
        return Route(Plan("point_lookup", signal=signal, period=period, event_type=event), "point_lookup")
    if TREND.search(q) and signal != "none":
        return Route(Plan("trend", signal=signal, period=period, group_by="day"), "trend")
    if event != "none":
        return Route(Plan("list_events", period=period, event_type=event), "list_events")
    return None
