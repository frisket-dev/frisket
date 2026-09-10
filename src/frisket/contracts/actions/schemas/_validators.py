"""Standard params validators with canonical error codes (C5 Deliverable 2).

Every rule in the action schemas is `raise ValueError("<code>")` — the string
IS the wire code (`validation_helpers._code_from_validation_error`). These are
the shared implementations of the patterns every map-family op repeats, so the
code vocabulary is systematic instead of hand-copied:

- `validate_optional_row_ids`: ONE acceptance set for row scopes —
  None passes; empty, non-positive, boolean, or duplicated ids raise
  `invalid_input_ref`.
- `validate_output_name`: output-name non-blank is `invalid_params`
  (canonical; previously drifted across invalid_params /
  duplicate_output_column / invalid_input_ref).
- `validate_model_id`: the `provider/model` router id shape is
  `invalid_model` (canonical, including ner's llm engine).
- duplicate EMITTED output columns are `duplicate_output_column`
  (`duplicate_column_name` stays for imported source columns only).

`rule`/`before_rule`/`cross_rule` are the Annotated-with-required-code and
op-level-validate hooks the Tier-1 signature-derived params generator emits:
each custom rule names its error code explicitly; the check raises ValueError
(any message) or returns the normalized value, and the wrapper re-raises with
the declared code.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator

from frisket.contracts.actions.schemas._base import StrictPositiveInt, StrictString
from frisket.local_model_ids import parse_local_model_id


def validate_optional_row_ids(row_ids: Any) -> Any:
    """The canonical row-scope acceptance set (code: invalid_input_ref).

    Runs in mode="before" so boolean/non-positive raw items are caught before
    lax int coercion could silently accept them. Non-list values pass through
    for the type validation to report the shape error.
    """
    if row_ids is None:
        return None
    if not isinstance(row_ids, (list, tuple)):
        return row_ids
    if not row_ids:
        raise ValueError("invalid_input_ref")
    if any(
        isinstance(item, bool) or (isinstance(item, (int, float)) and item <= 0)
        for item in row_ids
    ):
        raise ValueError("invalid_input_ref")
    try:
        unique = set(row_ids)
    except TypeError:
        return row_ids
    if len(unique) != len(row_ids):
        raise ValueError("invalid_input_ref")
    return row_ids


OptionalRowIds = Annotated[
    list[int] | None,
    BeforeValidator(validate_optional_row_ids),
]


def validate_output_name(value: str) -> str:
    """Output names are stripped and must be non-blank (code: invalid_params)."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("invalid_params")
    return stripped


OutputName = Annotated[str, AfterValidator(validate_output_name)]
NonBlankParam = OutputName
StrictNonBlankParam = Annotated[
    StrictString,
    AfterValidator(validate_output_name),
]


def validate_input_ref(value: str) -> str:
    """Strip a required input reference (code: invalid_input_ref)."""

    stripped = value.strip()
    if not stripped:
        raise ValueError("invalid_input_ref")
    return stripped


InputRef = Annotated[str, AfterValidator(validate_input_ref)]
StrictInputRef = Annotated[StrictString, AfterValidator(validate_input_ref)]


def validate_optional_nonblank_param(value: str | None) -> str | None:
    """Strip an optional value and reject blanks (code: invalid_params)."""

    if value is None:
        return None
    return validate_output_name(value)


OptionalNonBlankParam = Annotated[
    str | None,
    AfterValidator(validate_optional_nonblank_param),
]
OptionalStrictNonBlankParam = Annotated[
    StrictString | None,
    AfterValidator(validate_optional_nonblank_param),
]


def validate_strict_scoped_row_ids(
    row_ids: list[int] | None,
) -> list[int] | None:
    """Validate a strict optional row scope (code: invalid_input_ref)."""

    if row_ids is None:
        return None
    if not row_ids:
        raise ValueError("invalid_input_ref")
    if any(isinstance(item, bool) or item <= 0 for item in row_ids):
        raise ValueError("invalid_input_ref")
    if len(row_ids) != len(set(row_ids)):
        raise ValueError("invalid_input_ref")
    return row_ids


StrictScopedRowIds = Annotated[
    list[StrictPositiveInt] | None,
    AfterValidator(validate_strict_scoped_row_ids),
]


def validate_single_strict_input_columns(columns: list[str]) -> list[str]:
    """Normalize exactly one strict input column (code: invalid_input_ref)."""

    cleaned = [column.strip() for column in columns]
    if len(cleaned) != 1 or any(not column for column in cleaned):
        raise ValueError("invalid_input_ref")
    return cleaned


SingleStrictInputColumns = Annotated[
    list[StrictString],
    AfterValidator(validate_single_strict_input_columns),
]


def validate_input_columns(columns: list[str]) -> list[str]:
    """Strip and deduplicate input columns (code: invalid_input_ref)."""

    cleaned = [column.strip() for column in columns]
    if any(not column for column in cleaned):
        raise ValueError("invalid_input_ref")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("invalid_input_ref")
    return cleaned


InputColumns = Annotated[list[str], AfterValidator(validate_input_columns)]


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


def validate_model_id(value: str) -> str:
    """Model router ids are `provider/model` (code: invalid_model)."""
    if "/" not in value:
        raise ValueError("invalid_model")
    if value.startswith("ollama/"):
        try:
            parse_local_model_id(value)
        except ValueError as exc:
            raise ValueError("invalid_model") from exc
    return value


def ensure_unique_output_columns(names: list[str]) -> None:
    """Duplicate EMITTED output columns (code: duplicate_output_column)."""
    if len(names) != len(set(names)):
        raise ValueError("duplicate_output_column")


def _recode(code: str, check: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def run(value: Any) -> Any:
        try:
            return check(value)
        except ValueError as exc:
            raise ValueError(code) from exc

    return run


def rule(code: str, check: Callable[[Any], Any]) -> AfterValidator:
    """An op-specific field rule with a REQUIRED error code.

    `check(value)` returns the (possibly normalized) value or raises
    ValueError; the raise surfaces on the wire as `code`. This is the
    Annotated metadata the signature-derived params generator consumes:
    `Annotated[str, rule("invalid_value", check)]`.
    """
    return AfterValidator(_recode(code, check))


def before_rule(code: str, check: Callable[[Any], Any]) -> BeforeValidator:
    """`rule`, but running before type coercion (raw-value checks, e.g.
    regex group/timeout shapes)."""
    return BeforeValidator(_recode(code, check))


def cross_rule(code: str, check: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """An op-level cross-field rule with a REQUIRED error code, for the
    generated params model's `validate=` hook: `check(params)` returns the
    model (or None to keep it) or raises ValueError -> `code`."""

    def run(model: Any) -> Any:
        try:
            result = check(model)
        except ValueError as exc:
            raise ValueError(code) from exc
        return model if result is None else result

    return run
