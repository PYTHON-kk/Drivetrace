"""Tier 0 deterministic router (FR-QRY-01).

Keyword and pattern rules map a question to a Plan without calling the SLM. The router either returns a
plan it is confident about or None, in which case the question goes to the Tier 1 SLM planner.
Rules were tuned on the `dev` half of tests/data/query_set.yaml only.
"""

import re
from difflib import SequenceMatcher
from dataclasses import dataclass

from query.plan import Plan


@dataclass(frozen=True)
class Route:
    plan: Plan
    rule: str
    category: str = ""  # for unsupported: control | injection | off_topic


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts))


CONTROL = re.compile(r"\b(brake|stop|open|close|unlock|lock|accelerate|decelerate|turn (on|off)|"
                     r"switch (on|off)|disable|enable|activate|deactivate|start|set|clear|reset|honk|park|steer|"
                     r"increase|decrease|raise|lower|turn)\b")
CONTROL_START = re.compile(r"^(brake|stop|open|close|unlock|lock|accelerate|decelerate|turn|switch|disable|enable|"
                           r"activate|deactivate|start|set|clear|reset|honk|park|steer|increase|decrease|raise|lower)\b")
CONTROL_OBJECT = re.compile(r"\b(door|doors|window|windows|ac|air ?con|air ?conditioning|brake|brakes|"
                            r"light|lights|headlight|headlights|horn|wiper|wipers|heater|charging|charge|car)\b")
CONTROL_SAFE_READ = _rx(r"\bhow often did i brake\b", r"\bbrake warning\b", r"\bdid the brake warning\b")
POLITE_PREFIX = re.compile(r"^(can you|could you|would you|will you|i want you to|i need you to|you must|you need to|"
                           r"can u|plz|please|kindly|just|now|quickly)\s+", re.I)
INJECTION = _rx(r"ignore (all |any |the )?(previous |prior )?(instructions|rules)", r"system prompt",
                r"developer mode", r"drop table", r"\bselect \S+ from\b", r";\s*--", r"forget (the|all|your) rules",
                r"disable (all )?safety", r"\bauthori[sz]e\b", r"raw can", r"home address", r"\bsend\b.*\b(email|data)\b")
OFF_TOPIC = _rx(r"\bweather\b", r"\bjoke\b", r"\bfootball\b", r"\bfootball match\b", r"\bmatch score\b",
                r"book a table", r"capital of", r"\bplay\b.*\bmusic\b", r"^navigate\b", r"\bspeed limit\b")

SOC = _rx(r"state of charge", r"battery level", r"charge level", r"\bsoc\b", r"how much charge", r"charge (is )?left",
          r"battery percentage", r"battery charge\b", r"battery\b.*\b(left|remaining)\b")
RANGE = _rx(r"\brange\b", r"kilomet\w* per charge", r"\bkm per charge", r"miles per charge")
FUEL = _rx(r"\bfuel\b", r"mileage", r"\bmpg\b", r"\beconomy\b", r"\blit(re|er)s?\b", r"per 100 ?km", r"petrol")
ENERGY = _rx(r"\benergy\b", r"efficien", r"consumption", r"\bkwh\b", r"wh per km", r"wh/km")
BATT_TEMP = _rx(r"battery (temp|temperature)\b", r"battery temp\b", r"\bhotter\b", r"battery .*\brunning hot",
                r"battery .*\bgetting (hot|hotter|warm)\b", r"\bgetting hot\b")
SPEED = _rx(r"\bspeed\b", r"how fast", r"\bfaster\b", r"\bslower\b")

ADAS = _rx(r"\badas\b", r"lane (assist|keep\w*)", r"cruise( control)?", r"autopilot", r"driver assist\w*")
OVERTEMP = _rx(r"overheat\w*", r"over[- ]?temp\w*", r"too hot", r"(temperature|temp) (warning|alert)s?",
               r"battery warning")
LAMP = _rx(r"warning (lamp|light)s?", r"check engine", r"dashboard\b.*\blights?", r"\bmil\b", r"\blamps?\b")
CHARGING = re.compile(r"\bcharg(e|es|ed|ing)\b|plugged in")
WARNING_WORD = re.compile(r"\b(warning|alert)s?\b")

ROAD = _rx(r"\bcity\b", r"\btown\b", r"\burban\b", r"\bhighway\b", r"\bmotorway\b", r"\bfreeway\b")
DIAGNOSE = _rx(r"\bwhy did\b", r"what (may have |could have )?caused", r"what happened (right )?before",
               r"\bexplain\b", r"what led up to", r"went wrong")
COUNT = _rx(r"\bhow many\b", r"\bhow often\b", r"\bcount\b", r"number of")
AGG_MEAN = _rx(r"\baverage\b", r"\bmean\b")
AGG_MAX = _rx(r"\bmax\b", r"\bmaximum\b", r"\bhighest\b", r"\bpeak\b", r"\bhottest\b")
AGG_MIN = _rx(r"\bmin\b", r"\bminimum\b", r"\blowest\b")
AGG_DURATION = _rx(r"\bhow long\b", r"\bduration\b")
MAX_DROP = _rx(r"(largest|biggest|sharpest|greatest) (\w+ )?(drop|decline|fall|jump|increase)", r"drop(ped)? the most",
               r"fall the most", r"worst compared", r"suddenly", r"biggest fall")
COMPARE = _rx(r"\bwhy\b", r"compared? (to|with)", r"\bthan (last|the)\b", r"\bhigher\b", r"\blower\b",
              r"more energy", r"\bfewer\b", r"worse lately", r"went down", r"gone up", r"what changed",
              r"(weeks?|month|days) before", r"\breduced\b", r"got worse")
POINT = _rx(r"\bwhat (is|was|s)\b", r"\bcurrent\b", r"right now", r"\bnow\b", r"\blatest\b", r"how much .*\bleft\b", r"how fast was",
            r"at the last", r"when i last", r"\blast trip\b", r"what range did", r"\bwhats\b")
TREND = _rx(r"\btrend", r"over time", r"\bhow has\b", r"\bhow is\b", r"developed", r"day by day", r"\bplot\b",
            r"going up", r"getting (worse|better|hot|hotter|warm)")
SUMMARY = _rx(r"\bany faults?\b", r"\banything wrong\b", r"\b(do i have|are there) (any )?warnings?\b",
              r"\bany warnings?\b", r"\b(is there )?(a |any )?faults?\b", r"\bsummary\b",
              r"\bhow( is|s) my battery( doing)?$")

MONTH_NAME = (r"(january|february|march|april|may|june|july|august|september|october|november|december|"
              r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)")
TIME_UNSUPPORTED = _rx(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    r"\b(last night|this morning|this afternoon|this evening|yesterday morning|yesterday evening|yesterday afternoon)\b",
    r"\b(in|since|last|this|during|from)\s+" + MONTH_NAME + r"\b",
    MONTH_NAME + r"\s+\d{1,2}(st|nd|rd|th)?\b",
    r"\b\d{1,2}(st|nd|rd|th)?\s+(of\s+)?" + MONTH_NAME + r"\b",
    r"\b\d{4}-\d{1,2}-\d{1,2}\b",
    r"\bsince\s+\S+",
    r"\b\d+\s+(day|days|week|weeks|month|months)\s+ago\b",
    r"\bfrom\s+\d+\s+(day|days|week|weeks|month|months)\s+ago\b",
    r"\blast\s+(?!7\b|14\b|two\b|2\b)\d+\s+(day|days|week|weeks)\b",
    r"\blast\s+\d+\s+months\b",
)

HINGLISH = {
    "kitna": "how much",
    "kitni": "how much",
    "kitne": "how many",
    "bacha": "left",
    "bachi": "left",
    "batti": "battery",
    "gaadi": "vehicle",
    "aaj": "today",
    "pichle hafte": "last week",
    "pichhle hafte": "last week",
}

CONTRACTIONS = {
    "what's": "what is",
    "how's": "how is",
    "it's": "it is",
    "whats": "whats",
}

TYPO_VOCAB = {
    "whats",
    "what", "is", "was", "how", "much", "charge", "battery", "temperature", "speed", "range", "fuel",
    "energy", "consumption", "average", "mean", "max", "maximum", "min", "minimum", "highest", "lowest",
    "charging", "warning", "warnings", "overheating", "adas", "lane", "assist", "disengage", "today", "yesterday",
    "week", "month", "last", "this", "trip", "duration", "summary", "fault", "faults", "efficiency", "why",
    "did", "come", "on", "from", "since", "days", "ago", "sessions", "state", "of", "coolant", "pump",
    "working", "tyre", "tire", "pressure", "match", "logged", "gps", "mera", "hai", "kal", "vehicle",
}


def normalise(q: str) -> str:
    q = q.lower().replace("’", "'")
    for src, dst in CONTRACTIONS.items():
        q = q.replace(src, dst)
    for src, dst in HINGLISH.items():
        q = q.replace(src, dst)
    if " kal " in f" {q} ":
        if re.search(r"\b(was|were|did|came|happened|went|got|had)\b", q):
            q = q.replace(" kal ", " yesterday ")
        else:
            q = q.replace(" kal ", " ambiguous_kal ")
    q = re.sub(r"[^a-z0-9/;\-\s*]", " ", q)
    words = re.sub(r"\s+", " ", q).strip().split()
    fixed: list[str] = []
    for tok in words:
        if tok in TYPO_VOCAB or tok.isdigit() or len(tok) < 5:
            fixed.append(tok)
            continue
        if not tok.isalpha():
            fixed.append(tok)
            continue
        best, score = tok, 0.0
        for cand in (c for c in TYPO_VOCAB if c and c[0] == tok[0]):
            s = SequenceMatcher(None, tok, cand).ratio()
            if s > score:
                best, score = cand, s
        fixed.append(best if score >= 0.88 else tok)
    return " ".join(fixed)


def period_of(q: str) -> str:
    if "this month" in q or re.search(r"(compared? (to|with)|than) last month", q):
        return "this_month"
    if re.search(r"\b(last|previous) month", q) or "month before" in q:
        return "last_month"
    if "yesterday" in q:
        return "yesterday"
    if "today" in q:
        return "today"
    if re.search(r"(14|fourteen) day(s)?|(two|2) week(s)?|fortnight", q):
        return "last_14_days"
    if re.search(r"(7|seven) day(s)?|past week|last week|this week|the week", q):
        return "last_7_days"
    return "all"


def aggregate_of(q: str) -> str:
    if AGG_DURATION.search(q):
        return "duration"
    if AGG_MEAN.search(q):
        return "mean"
    if AGG_MAX.search(q):
        return "max"
    if AGG_MIN.search(q):
        return "min"
    return "latest"


def _is_control(q: str) -> bool:
    if CONTROL_SAFE_READ.search(q):
        return False
    text = q
    while True:
        stripped = POLITE_PREFIX.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    if CONTROL_START.search(text):
        return True
    return False


def _needs_time_clarification(q: str) -> bool:
    if "ambiguous_kal" in q:
        return True
    if re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", q):
        return True
    if re.search(r"\b(last night|this morning|this afternoon|this evening|yesterday morning|yesterday evening|yesterday afternoon)\b", q):
        return True
    if re.search(r"\b(today|yesterday|this month|last month|last week|past week|this week|"
                 r"last 7 days|last 14 days|two weeks|2 weeks|fortnight)\b", q):
        return False
    if re.search(r"\blast\s+(7|14|two|2)\s+(day|days|week|weeks)\b", q):
        return False
    if not TIME_UNSUPPORTED.search(q):
        return False
    return True


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
    if WARNING_WORD.search(masked) and ("battery" in masked or "temperature" in masked or "last warning" in masked or COUNT.search(masked)):
        return "battery_overtemp_warning"
    return "none"


def route(question: str) -> Route | None:
    q = normalise(question)
    if INJECTION.search(q):
        return Route(Plan("unsupported"), "injection", "injection")
    if _is_control(q):
        return Route(Plan("unsupported"), "control", "control")
    if OFF_TOPIC.search(q):
        return Route(Plan("unsupported"), "off_topic", "off_topic")
    if _needs_time_clarification(q):
        return Route(Plan("unsupported"), "time_clarify", "time_clarify")

    if "tyre" in q or "tire" in q:
        if "pressure" in q:
            return Route(Plan("unsupported"), "tyre_not_recorded", "tyre_not_recorded")
        return Route(Plan("unsupported"), "off_topic", "off_topic")

    period, signal, event, aggregate = period_of(q), signal_of(q), event_of(q), aggregate_of(q)
    if SUMMARY.search(q):
        return Route(Plan("summary_health", period=period), "summary_health")
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
    if aggregate == "duration" and event == "charging":
        return Route(Plan("point_lookup", period=period, event_type=event, aggregate=aggregate), "point_lookup")
    if POINT.search(q) and signal != "none":
        return Route(Plan("point_lookup", signal=signal, period=period, event_type=event, aggregate=aggregate), "point_lookup")
    if TREND.search(q) and signal != "none":
        return Route(Plan("trend", signal=signal, period=period, group_by="day"), "trend")
    if signal != "none" and aggregate in ("mean", "max", "min"):
        return Route(Plan("point_lookup", signal=signal, period=period, event_type=event, aggregate=aggregate), "point_lookup")
    if "match" in q and signal != "none":
        return Route(Plan("point_lookup", signal=signal, period=period, aggregate=aggregate), "point_lookup")
    if signal != "none" and "how much" in q:
        return Route(Plan("point_lookup", signal=signal, period=period, event_type=event, aggregate=aggregate), "point_lookup")
    if event != "none":
        return Route(Plan("list_events", period=period, event_type=event), "list_events")
    return None
