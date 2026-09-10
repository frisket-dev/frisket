"""Canonical calendar-date parsing shared by typed cells and SQL filters."""

from __future__ import annotations

from datetime import UTC, date, datetime
import re
from typing import Any


_CALENDAR_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_RFC3339_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)


def parse_utc_calendar_date(value: Any) -> date | None:
    """Parse one exact ISO calendar date or RFC3339 timestamp.

    Timestamps are normalized to UTC before their calendar date is returned.
    Compact dates, ISO week/ordinal dates, naive timestamps, and SQLite's
    numeric/Julian-date forms are deliberately outside this contract.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    if _CALENDAR_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    if not _RFC3339_TIMESTAMP.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    return parsed.astimezone(UTC).date()


def normalize_utc_calendar_date(value: Any) -> str | None:
    """Return canonical ``YYYY-MM-DD`` or ``None`` for an invalid value."""

    parsed = parse_utc_calendar_date(value)
    return parsed.isoformat() if parsed is not None else None


def is_valid_calendar_date(value: Any) -> bool:
    """Whether ``value`` belongs to the canonical date-cell contract."""

    return parse_utc_calendar_date(value) is not None
