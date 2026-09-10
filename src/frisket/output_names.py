"""Column-name rules shared by typed actions and the row runner."""

from __future__ import annotations

from collections.abc import Iterable


RUNNER_RESERVED_OUTPUT_NAMES = frozenset(
    {
        "confidence",
        "outcome",
        "justification",
        "error",
        "error_code",
        "__field_errors__",
    }
)


def validate_runner_output_name(value: str) -> str:
    """Reject names the row runner consumes as control metadata."""

    if not isinstance(value, str):
        raise ValueError("invalid_params")
    stripped = value.strip()
    if not stripped:
        raise ValueError("invalid_params")
    if stripped in RUNNER_RESERVED_OUTPUT_NAMES or stripped.endswith("_confidence"):
        raise ValueError("invalid_params")
    return stripped


def validate_runner_output_family(names: Iterable[str]) -> list[str]:
    """Reject duplicate names and value/justification namespace collisions."""

    normalized = [validate_runner_output_name(name) for name in names]
    namespace = set(normalized)
    if len(normalized) != len(namespace):
        raise ValueError("duplicate_output_column")
    if any(f"{name}_justification" in namespace for name in normalized):
        raise ValueError("duplicate_output_column")
    return normalized
