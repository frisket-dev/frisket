"""Canonical source reasons shared by row and whole-column execution."""

from collections.abc import Mapping
from typing import Any


def input_unavailability(
    column: str,
    ref: Mapping[str, Any] | None,
    *,
    required: bool = True,
    preserve_invalid: bool = False,
    allow_missing: bool = False,
) -> dict[str, str] | None:
    if not required:
        return None
    outcome = ref.get("outcome") if ref is not None else None
    validity = ref.get("validity") if ref is not None else None
    reason = None
    if outcome in {"pending", "failed", "not_completed"}:
        reason = str(outcome)
    elif validity == "invalid" and not preserve_invalid:
        reason = "invalid"
    elif not allow_missing and (ref is None or validity == "missing"):
        reason = "missing"
    if reason is None:
        return None
    return {
        "column": column,
        "code": "input_" + reason,
        "message": f"Required input {column!r} is {reason.replace('_', ' ')}",
    }


def not_completed_result(reason: Mapping[str, str]) -> dict[str, Any]:
    """Keep unavailable work distinct from successful null and provider failure."""
    return {
        "value": None,
        "outcome": "not_completed",
        "publication_effect": "publish_error",
        "error_code": reason["code"],
        "error": reason["message"],
        "cost": 0.0,
        "tokens_in": None,
        "tokens_out": None,
    }
