"""Resolve plan periods against the vehicle clock (FR-QRY-04).

"Now" is the time of the newest stored sample for the source, not the wall clock: a replayed or
archived vehicle answers "yesterday" relative to its own data. Calendar periods use the vehicle's
local time zone (from source.json).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Window:
    start: datetime  # UTC, inclusive
    end: datetime    # UTC, exclusive
    label: str  # reads after a noun phrase: "5 warnings {label}"

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400


def _midnight(t: datetime) -> datetime:
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(t: datetime) -> datetime:
    return _midnight(t).replace(day=1)


def _utc(t: datetime) -> datetime:
    return t.astimezone(timezone.utc)


def resolve(period: str, now: datetime, tz: str, first: datetime) -> Window:
    z = ZoneInfo(tz)
    local = now.astimezone(z)
    end = now + timedelta(microseconds=1)  # make the newest sample inclusive
    if period == "today":
        start = _midnight(local)
        return Window(_utc(start), end, f"today ({start:%a %d %b})")
    if period == "yesterday":
        start = _midnight(local) - timedelta(days=1)
        return Window(_utc(start), _utc(_midnight(local)), f"yesterday ({start:%a %d %b})")
    if period in ("last_7_days", "last_14_days"):
        days = 7 if period == "last_7_days" else 14
        return Window(now - timedelta(days=days), end, f"in the last {days} days")
    if period == "this_month":
        start = _month_start(local)
        return Window(_utc(start), end, f"in {start:%B %Y} so far")
    if period == "last_month":
        this = _month_start(local)
        start = _month_start(this - timedelta(days=1))
        return Window(_utc(start), _utc(this), f"in {start:%B %Y}")
    if period == "all":
        return Window(first, end, "across all stored history")
    raise ValueError(f"unknown period {period!r}")


def previous(window: Window, period: str, tz: str) -> Window:
    """The comparison period immediately before `window` (same length; calendar month for months)."""
    z = ZoneInfo(tz)
    if period in ("this_month", "last_month"):
        start_local = window.start.astimezone(z)
        prev_start = _month_start(start_local - timedelta(days=1))
        return Window(_utc(prev_start), window.start, f"in {prev_start:%B %Y}")
    length = window.end - window.start
    days = round(length.total_seconds() / 86400)
    return Window(window.start - length, window.start, f"in the {days} days before")
