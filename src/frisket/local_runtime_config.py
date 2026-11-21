"""Dependency-light local installation configuration shared with execution.

This module deliberately has no server imports: CLI, MCP, and library run
paths need the same local allowance default as the HTTP server.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


DEFAULT_ALLOWANCE_USD = "2"
ALLOWANCE_ENV = "FRISKET_COST_CONSENT_USD"
ALLOWANCE_SETTINGS_KEY = "allowance_usd"
LEGACY_ALLOWANCE_SETTINGS_KEY = "cost_preapproval_usd"
_MAX_ALLOWANCE_USD = Decimal("1000000000000")


class InvalidLocalRuntimeConfigError(ValueError):
    """The local runtime configuration cannot safely describe a threshold."""


def normalize_allowance_usd(value: object) -> str:
    """Return a canonical, finite non-negative USD decimal string."""

    if not isinstance(value, str):
        raise InvalidLocalRuntimeConfigError("allowance_usd must be a decimal string")
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise InvalidLocalRuntimeConfigError(
            "allowance_usd must be a decimal string"
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise InvalidLocalRuntimeConfigError("allowance_usd must be non-negative")
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -6 or amount >= _MAX_ALLOWANCE_USD:
        raise InvalidLocalRuntimeConfigError(
            "allowance_usd must fit in 12 whole and 6 decimal digits"
        )
    normalized = format(amount, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def extract_stored_allowance_usd(settings: Mapping[str, Any]) -> str | None:
    """Read the allowance from a settings mapping, accepting either storage key."""

    canonical = settings.get(ALLOWANCE_SETTINGS_KEY)
    legacy = settings.get(LEGACY_ALLOWANCE_SETTINGS_KEY)
    if canonical is not None and legacy is not None:
        normalized_canonical = normalize_allowance_usd(canonical)
        normalized_legacy = normalize_allowance_usd(legacy)
        if normalized_canonical != normalized_legacy:
            raise InvalidLocalRuntimeConfigError(
                f"conflicting {ALLOWANCE_SETTINGS_KEY} and "
                f"{LEGACY_ALLOWANCE_SETTINGS_KEY} values"
            )
        return normalized_canonical
    if canonical is not None:
        return normalize_allowance_usd(canonical)
    if legacy is not None:
        return normalize_allowance_usd(legacy)
    return None


def resolve_local_allowance_usd(
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
    value = extract_stored_allowance_usd(settings)
    if value is not None:
        return value
    raw_env = (os.environ if env is None else env).get(ALLOWANCE_ENV)
    if raw_env is None:
        return DEFAULT_ALLOWANCE_USD
    try:
        return normalize_allowance_usd(raw_env)
    except InvalidLocalRuntimeConfigError:
        # An explicitly malformed startup knob must never silently grant
        # standing coverage; preserve the fail-closed posture.
        return "0"


__all__ = [
    "ALLOWANCE_ENV",
    "ALLOWANCE_SETTINGS_KEY",
    "DEFAULT_ALLOWANCE_USD",
    "InvalidLocalRuntimeConfigError",
    "LEGACY_ALLOWANCE_SETTINGS_KEY",
    "extract_stored_allowance_usd",
    "normalize_allowance_usd",
    "resolve_local_allowance_usd",
]
