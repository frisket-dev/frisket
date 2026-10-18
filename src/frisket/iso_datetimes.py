"""Strict ISO date-time validation without timezone invention."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any


_ISO_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?P<zone>Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?\Z"
)


def parse_iso_datetime(value: Any) -> datetime | None:
    """Parse one canonical local or offset-aware ISO date-time string."""

    if not isinstance(value, str):
        return None
    match = _ISO_DATETIME.fullmatch(value)
    if match is None:
        return None
    try:
        parsed = datetime.fromisoformat(
            f"{value[:-1]}+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return None
    zone = match.group("zone")
    aware = parsed.tzinfo is not None and parsed.utcoffset() is not None
    if (zone is None and aware) or (zone is not None and not aware):
        return None
    return parsed


def is_valid_iso_datetime(value: Any) -> bool:
    """Whether ``value`` preserves a complete local or aware date-time."""

    return parse_iso_datetime(value) is not None
