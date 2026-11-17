"""Shared params field validators.

A params rule raises ``ValueError("<text>")``; for action params the text is
the wire code (`validation_helpers._code_from_validation_error`), so each
field keeps its own message by passing it to the factory instead of
restating the check.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import AfterValidator


def nonblank(message: str, *, strip: bool = True) -> AfterValidator:
    """Reject a whitespace-only string with ``message``; ``None`` passes.

    ``strip`` returns the stripped value; ``strip=False`` keeps the value as
    sent and only refuses blanks.
    """

    def check(value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError(message)
        return stripped if strip else value

    return AfterValidator(check)


def trimmed(message: str, *, allow_empty: bool = False) -> AfterValidator:
    """Reject surrounding whitespace, and an empty string unless allowed."""

    def check(value: str) -> str:
        if value != value.strip() or not (value or allow_empty):
            raise ValueError(message)
        return value

    return AfterValidator(check)


def validate_positive_finite_number(value: Any) -> float:
    """Normalize a positive numeric control and reject NaN/infinities."""

    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_params") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("invalid_params")
    return normalized


def validate_positive_finite_rate(value: Any) -> float:
    """Normalize a rate whose reciprocal is a finite pacing interval."""

    normalized = validate_positive_finite_number(value)
    if not math.isfinite(1.0 / normalized):
        raise ValueError("invalid_params")
    return normalized
