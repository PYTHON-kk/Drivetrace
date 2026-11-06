"""Deterministic answer templates (P2). Every number comes from the executor result, never from a model.

Wording follows the epistemic levels of solution_journey §8.11: observed facts are stated plainly,
period comparisons say what *coincided* with a change, and nothing here claims a cause.
"""

from ecd.answer import answer as ecd_answer
from query.executor import Result
from query.plan import Plan
from query.vocabulary import EVENT_LABELS

REFUSALS = {
    "control": "I can only read vehicle data. I can't control the vehicle.",
    "injection": "I can't do that. I only answer questions about this vehicle's recorded data.",
    "off_topic": "I can only answer questions about this vehicle's data, such as trips, energy use, warnings and ADAS events.",
    "not_understood": "I didn't understand that question. You can ask, for example, \"How many overheating warnings this month?\" "
                      "or \"Compare energy consumption between city and highway driving.\"",
}
SIGNAL_NAMES = {
    "Vehicle.Powertrain.TractionBattery.Temperature.Average": ("battery average temperature", "°C", 1),
    "Vehicle.Powertrain.TractionBattery.Temperature.Max": ("battery maximum temperature", "°C", 1),
    "Vehicle.Powertrain.TractionBattery.CurrentCurrent": ("battery current (+ = charging)", "A", 0),
    "Vehicle.Powertrain.TractionBattery.StateOfCharge.Current": ("state of charge", "%", 0),
    "Vehicle.VendorExt.TractionBattery.Cooling.PumpCommand": ("coolant pump command (0 = off, 1 = on)", "", 0),
    "Vehicle.VendorExt.TractionBattery.Cooling.PumpSpeed": ("coolant pump speed", "rpm", 0),
    "Vehicle.VendorExt.TractionBattery.Cooling.CoolantOutletTemperature": ("coolant outlet temperature", "°C", 1),
    "Vehicle.Exterior.AirTemperature": ("outside temperature", "°C", 1),
    "Vehicle.Speed": ("speed", "km/h", 0),
    "Vehicle.Powertrain.CombustionEngine.ECT": ("engine coolant temperature", "°C", 1),
}
DECIMALS = {"Wh/km": 0, "l/100 km": 1, "°C": 1, "km/h": 0, "%": 0, "km": 0, "W": 0}


def num(v, unit: str = "") -> str:
    if v is None:
        return "n/a"
    d = DECIMALS.get(unit, 1)
    s = f"{v:,.{d}f}"
    return f"{s} {unit}".strip() if unit and unit != "%" else (s + unit)


def pct(new, old) -> str:
    if not new or not old:
        return "n/a"
    return f"{(new / old - 1) * 100:+.0f}%"


def day(d) -> str:
    return f"{d:%a %d %b}"


def when(t) -> str:
    return f"{t:%a %d %b %H:%M}"


def _describe(attrs: dict) -> str:
    parts = []
    if "reason" in attrs:
        parts.append(attrs["reason"].replace("_", " "))
    if "trigger" in attrs:
        parts.append(f"{attrs['trigger'].replace('_', ' ')}")
    if "speed_kmh" in attrs:
        parts.append(f"at {float(attrs['speed_kmh']):.0f} km/h")
    if "lamp" in attrs:
        parts.append(attrs["lamp"].replace("_", " "))
    if "measured_avg_c" in attrs:
        parts.append(f"battery avg {attrs['measured_avg_c']} °C, max {attrs.get('measured_max_c')} °C")
    if "type" in attrs:
        parts.append(f"{attrs['type']} {attrs.get('requested_power_kw', '')} kW".strip())
    if "soc_start_pct" in attrs:
        parts.append(f"from {attrs['soc_start_pct']}%")
    return ", ".join(parts)


def render_refusal(category: str) -> str:
    return REFUSALS.get(category, REFUSALS["not_understood"])


def render(plan: Plan, r: Result, role: str = "service") -> str:
    d, w = r.data, r.window.label
    i = plan.intent
    if i == "list_events":
        label = EVENT_LABELS[plan.event_type]
        if d["total"] == 0:
            return f"No {label}s {w}."
        lines = [f"{d['total']} {label}{'s' if d['total'] != 1 else ''} {w}:"]
        for e in d["events"][:10]:
            trip = f" (trip {e['trip_no']})" if e["trip_no"] is not None else ""
            extra = _describe(e["attrs"])
            lines.append(f"• {when(e['local_time'])}{' — ' + extra if extra else ''}{trip}")
        if d["total"] > 10:
            lines.append(f"…and {d['total'] - 10} more.")
        return "\n".join(lines)

    if i == "count_events":
        label = EVENT_LABELS[plan.event_type]
        text = f"{d['total']} {label}{'s' if d['total'] != 1 else ''} {w}."
        if len(d["by_day"]) > 1:
            text += " By day: " + ", ".join(f"{x:%d %b} ({n})" for x, n in d["by_day"][:8]) + ("…" if len(d["by_day"]) > 8 else "") + "."
        return text

    if i == "max_drop":
        if not d["found"]:
            return f"Not enough driving {w} to compare days (each day needs at least 10 km)."
        u = d["unit"]
        what = "fuel efficiency" if u == "l/100 km" else "energy efficiency"
        return (f"The largest day-to-day drop in {what} {w} was on {day(d['day'])}: {d['label']} rose to "
                f"{num(d['value'], u)} from {num(d['prev_value'], u)} on {day(d['prev_day'])} "
                f"({pct(d['value'], d['prev_value'])}). That day you drove {num(d['km'], 'km')}, average outside "
                f"temperature {num(d['ambient_c'], '°C')}, cabin heater {num(d['heater_w'], 'W')}, "
                f"{num((d['highway_share'] or 0) * 100, '%')} highway. Compared {d['days_compared']} days.")

    if i == "trend":
        u = d["unit"]
        if len(d["series"]) < 2:
            return f"Not enough data {w} to show a trend for {d['label']}."
        (d0, v0), (d1, v1) = d["first"], d["last"]
        direction = "rising" if d["slope_per_week"] > 0 else "falling"
        return (f"{d['label'].capitalize()} {w}: {num(v0, u)} on {day(d0)} to {num(v1, u)} on {day(d1)}; "
                f"overall {direction} by about {num(abs(d['slope_per_week']), u)} per week over {len(d['series'])} days. "
                f"Lowest {num(d['min'][1], u)} on {day(d['min'][0])}, highest {num(d['max'][1], u)} on {day(d['max'][0])}.")

    if i == "compare_periods":
        prev = d["previous_window"].label
        u = d["unit"]
        if d["mode"] == "signal":
            return (f"Average {d['label']}: {num(d['current'], u)} {w} vs {num(d['previous'], u)} {prev} "
                    f"({pct(d['current'], d['previous'])}).")
        c, o = d["current"], d["previous"]
        if not c["trips"] or not o["trips"]:
            return f"Not enough trips to compare ({w} vs {prev})."
        text = f"{d['label'].capitalize()}: {num(c['value'], u)} {w} vs {num(o['value'], u)} {prev} ({pct(c['value'], o['value'])})."
        if d["asked"] == "range" and c["value"] and o["value"]:
            change = (o["value"] / c["value"] - 1) * 100
            text += (f" With the same usable battery energy, that is about {abs(change):.0f}% "
                     f"{'less' if change < 0 else 'more'} range per charge.")
        ctx = [f"outside temperature {num(o['ambient_c'], '°C')} → {num(c['ambient_c'], '°C')}"]
        if c["heater_w"] is not None:
            ctx.append(f"cabin heater {num(o['heater_w'], 'W')} → {num(c['heater_w'], 'W')}")
        ctx.append(f"highway share {num((o['highway_share'] or 0) * 100, '%')} → {num((c['highway_share'] or 0) * 100, '%')}")
        ctx.append(f"average moving speed {num(o['avg_kmh'], 'km/h')} → {num(c['avg_kmh'], 'km/h')}")
        text += " Over the same periods: " + "; ".join(ctx) + "."
        text += " These changes coincided with the change in consumption; they are not proven causes."
        return text

    if i == "compare_groups":
        g, u = d["groups"], d["unit"]
        parts = [f"{name} {num(x['value'], u)} over {num(x['km'], 'km')} ({x['trips']} trips)"
                 for name, x in sorted(g.items())]
        text = f"{d['label'].capitalize()} {w}: " + "; ".join(parts) + "."
        if "city" in g and "highway" in g and g["city"]["value"] and g["highway"]["value"]:
            diff = (g["highway"]["value"] / g["city"]["value"] - 1) * 100
            if plan.signal == "speed":
                text += f" Highway is {abs(diff):.0f}% {'faster' if diff > 0 else 'slower'} than city."
            else:
                text += f" Highway uses {abs(diff):.0f}% {'more' if diff > 0 else 'less'} than city."
        return text

    if i == "point_lookup":
        if not d["found"]:
            if d["reason"] == "no_event":
                return f"There is no {EVENT_LABELS[plan.event_type]} {w}."
            return "No recent value is recorded for that."
        if d["kind"] == "trip":
            at = f" (the trip containing the {EVENT_LABELS[plan.event_type]})" if d["event"] else ""
            return (f"Trip {d['trip_no']} on {when(d['local_time'])}{at}: {num(d['km'], 'km')}, "
                    f"{d['label']} {num(d['value'], d['unit'])}.")
        if d["is_latest"]:
            return f"Latest {d['label']}: {num(d['value'], d['unit'])} (recorded {when(d['sample_local_time'])})."
        return (f"{d['label'].capitalize()} was {num(d['value'], d['unit'])} at the last "
                f"{EVENT_LABELS[plan.event_type]} ({when(d['sample_local_time'])}).")

    if i == "diagnose_event":
        label = EVENT_LABELS[plan.event_type]
        if not d["found"]:
            return f"There is no {label} {w}."
        lines = [f"Last {label}: {when(d['local_time'])}"
                 + (f" — {_describe(d['attrs'])}" if d["attrs"] and role != "driver" else "") + "."]
        rec = d.get("record")
        if role == "driver":  # drivers get the conclusion only, in plain words
            return "\n".join(lines + ([ecd_answer(rec, "driver")["text"]] if rec else []))
        lines.append("In the 30 minutes before it:")
        order = list(SIGNAL_NAMES)
        for path, s in sorted(d["signals"].items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99):
            name, unit, dec = SIGNAL_NAMES.get(path, (path, "", 1))
            u = f" {unit}" if unit else ""
            lines.append(f"• {name}: {s['start']:.{dec}f} → {s['at_event']:.{dec}f}{u} "
                         f"(range {s['min']:.{dec}f}–{s['max']:.{dec}f})")
        if d["events_before"]:
            lines.append("Events in the hour before: " + "; ".join(
                f"{t:%H:%M} {etype.replace('_', ' ').lower()}" for etype, t, _ in d["events_before"][-6:]) + ".")
        if rec is None:
            lines.append("Cause analysis is available for battery over-temperature warnings only.")
        else:
            lines.append("Diagnosis: " + ecd_answer(rec, role)["text"])
        return "\n".join(lines)

    return render_refusal("not_understood")
