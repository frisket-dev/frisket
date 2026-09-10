"""Shared cadence parsing for queue schedulers (source polls + embedding refresh).

Dependency-free on purpose: both ``frisket.jobs.sources`` and
``frisket.jobs.embeddings`` parse the same schedule strings, and sources already
depends on embeddings — putting the parser here avoids an import cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _parse_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _schedule_interval(schedule: str | None) -> timedelta | None:
    if not schedule or not schedule.strip():
        return None
    s = schedule.strip().lower()
    aliases = {
        "@hourly": timedelta(hours=1),
        "hourly": timedelta(hours=1),
        "1h": timedelta(hours=1),
        "@daily": timedelta(days=1),
        "daily": timedelta(days=1),
        "24h": timedelta(days=1),
    }
    if s in aliases:
        return aliases[s]
    parts = s.split()
    if len(parts) != 5:
        raise ValueError(f"unsupported schedule: {schedule!r}")
    minute, hour, day, month, dow = parts
    if day != "*" or month != "*" or dow != "*":
        raise ValueError(f"unsupported schedule: {schedule!r}")
    if minute.startswith("*/") and hour == "*":
        n = int(minute[2:])
        if n <= 0:
            raise ValueError(f"unsupported schedule: {schedule!r}")
        return timedelta(minutes=n)
    if minute.isdigit() and hour == "*":
        return timedelta(hours=1)
    if minute.isdigit() and hour.startswith("*/"):
        n = int(hour[2:])
        if n <= 0:
            raise ValueError(f"unsupported schedule: {schedule!r}")
        return timedelta(hours=n)
    if minute.isdigit() and hour.isdigit():
        return timedelta(days=1)
    raise ValueError(f"unsupported schedule: {schedule!r}")
