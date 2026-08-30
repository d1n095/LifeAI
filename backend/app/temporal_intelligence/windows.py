"""Resolve preset recap windows to UTC time ranges."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.temporal_intelligence.types import RecapWindow, TimeRange

_WINDOW_DELTAS: dict[RecapWindow, timedelta] = {
    RecapWindow.HOUR: timedelta(hours=1),
    RecapWindow.DAY: timedelta(days=1),
    RecapWindow.WEEK: timedelta(days=7),
    RecapWindow.MONTH: timedelta(days=30),
    RecapWindow.QUARTER: timedelta(days=90),
    RecapWindow.YEAR: timedelta(days=365),
}


class TemporalWindowError(ValueError):
    pass


def resolve_window(
    window: RecapWindow | str,
    *,
    now: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> TimeRange:
    """Map a preset (or custom bounds) to an inclusive-start / exclusive-end range.

    `entire_project` returns unbounded (start=None, end=None).
    Custom requires both start and end with start < end.
    """
    if not isinstance(window, RecapWindow):
        try:
            window = RecapWindow(str(window))
        except ValueError as exc:
            raise TemporalWindowError(f"unknown window: {window}") from exc

    now = now or datetime.utcnow()

    if window == RecapWindow.ENTIRE_PROJECT:
        return TimeRange(start=None, end=None, window=window, label="entire_project")

    if window == RecapWindow.CUSTOM:
        if start is None or end is None:
            raise TemporalWindowError("custom window requires start and end")
        if start >= end:
            raise TemporalWindowError("custom window requires start < end")
        return TimeRange(start=start, end=end, window=window, label=f"custom:{start.isoformat()}/{end.isoformat()}")

    delta = _WINDOW_DELTAS[window]
    return TimeRange(start=now - delta, end=now, window=window, label=window.value)


def in_range(occurred_at: datetime | None, rng: TimeRange) -> bool:
    if occurred_at is None:
        return False
    if rng.start is not None and occurred_at < rng.start:
        return False
    if rng.end is not None and occurred_at >= rng.end:
        return False
    return True
