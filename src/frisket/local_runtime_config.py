"""Dependency-light local installation configuration shared with execution.

This module deliberately has no server imports: CLI, MCP, and library run
paths need the same local cost-preapproval default as the HTTP server.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_COST_PREAPPROVAL_USD = "2"
COST_PREAPPROVAL_ENV = "FRISKET_COST_CONSENT_USD"
_MAX_COST_PREAPPROVAL_USD = Decimal("1000000000000")


class InvalidLocalRuntimeConfigError(ValueError):
    """The local runtime configuration cannot safely describe a threshold."""


def normalize_cost_preapproval_usd(value: object) -> str:
    """Return a canonical, finite non-negative USD decimal string."""

    if not isinstance(value, str):
        raise InvalidLocalRuntimeConfigError(
            "cost_preapproval_usd must be a decimal string"
        )
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise InvalidLocalRuntimeConfigError(
            "cost_preapproval_usd must be a decimal string"
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise InvalidLocalRuntimeConfigError(
            "cost_preapproval_usd must be non-negative"
        )
    if amount.as_tuple().exponent < -6 or amount >= _MAX_COST_PREAPPROVAL_USD:
        raise InvalidLocalRuntimeConfigError(
            "cost_preapproval_usd must fit in 12 whole and 6 decimal digits"
        )
    normalized = format(amount, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def resolve_local_cost_preapproval_usd(
    root: str | Path,
    env: Mapping[str, str] | None = None,
) -> str:
    """Read local storage, else the startup knob, else the $2 default."""

    path = Path(root) / ".frisket" / "runtime_settings.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        settings = {}
    except (OSError, UnicodeError) as exc:
        raise InvalidLocalRuntimeConfigError(
            f"cannot read runtime settings at {path}"
        ) from exc
    else:
        try:
            settings: Any = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise InvalidLocalRuntimeConfigError(
                f"runtime settings at {path} are not valid JSON"
            ) from exc
        if not isinstance(settings, dict):
            raise InvalidLocalRuntimeConfigError(
                f"runtime settings at {path} must be a JSON object"
            )
    value = settings.get("cost_preapproval_usd")
    if value is not None:
        return normalize_cost_preapproval_usd(value)
    raw_env = (os.environ if env is None else env).get(COST_PREAPPROVAL_ENV)
    if raw_env is None:
        return DEFAULT_COST_PREAPPROVAL_USD
    try:
        return normalize_cost_preapproval_usd(raw_env)
    except InvalidLocalRuntimeConfigError:
        # An explicitly malformed startup knob must never silently grant
        # standing coverage; preserve the old fail-closed posture.
        return "0"


__all__ = [
    "DEFAULT_COST_PREAPPROVAL_USD",
    "COST_PREAPPROVAL_ENV",
    "InvalidLocalRuntimeConfigError",
    "normalize_cost_preapproval_usd",
    "resolve_local_cost_preapproval_usd",
]
