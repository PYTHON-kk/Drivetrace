"""Single source of truth for the query-plan vocabulary.

The GBNF grammars (query/plan_grammar*.gbnf), the validator, the executor and the test set all use these
lists. Regenerate the grammars after editing: python -m query.grammar
"""

KEYS = ["intent", "signal", "period", "group_by", "event_type", "aggregate"]

INTENTS = ["list_events", "count_events", "max_drop", "trend", "compare_periods", "compare_groups",
           "point_lookup", "diagnose_event", "summary_health", "unsupported"]
SIGNALS = ["energy_consumption", "range", "battery_temperature", "speed", "fuel_consumption", "state_of_charge", "none"]
PERIODS = ["today", "yesterday", "last_7_days", "last_14_days", "this_month", "last_month", "all"]
GROUP_BY = ["none", "road_type", "day", "trip"]
EVENT_TYPES = ["none", "adas_disengaged", "battery_overtemp_warning", "charging", "warning_lamp"]
AGGREGATES = ["latest", "mean", "max", "min", "duration", "count"]

VALUES = {"intent": INTENTS, "signal": SIGNALS, "period": PERIODS, "group_by": GROUP_BY,
          "event_type": EVENT_TYPES, "aggregate": AGGREGATES}

# Plan event_type -> canonical event type in the event index
EVENT_TYPE_MAP = {
    "adas_disengaged": "ADAS_DISENGAGED",
    "battery_overtemp_warning": "BATT_OVERTEMP_WARNING_SET",
    "charging": "CHARGING_STARTED",
    "warning_lamp": "WARNING_LAMP_ON",
}

# Plan signal -> canonical VSS path for point values and daily series
SIGNAL_PATHS = {
    "range": "Vehicle.Powertrain.Range",
    "battery_temperature": "Vehicle.Powertrain.TractionBattery.Temperature.Average",
    "speed": "Vehicle.Speed",
    "state_of_charge": "Vehicle.Powertrain.TractionBattery.StateOfCharge.Current",
}
CONSUMPTION_SIGNALS = {"energy_consumption", "fuel_consumption"}

# Human-readable names used by the answer templates
EVENT_LABELS = {
    "adas_disengaged": "ADAS disengagement",
    "battery_overtemp_warning": "battery over-temperature warning",
    "charging": "charging session",
    "warning_lamp": "warning-lamp event",
}
SIGNAL_LABELS = {
    "energy_consumption": "energy consumption",
    "fuel_consumption": "fuel consumption",
    "range": "range",
    "battery_temperature": "battery temperature",
    "speed": "speed",
    "state_of_charge": "state of charge",
}
