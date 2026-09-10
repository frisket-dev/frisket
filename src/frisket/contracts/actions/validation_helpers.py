"""Shared validation helpers for action contract definitions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from frisket.contracts.action import (
    ActionError,
    ActionSpec,
    ActionValidationResult,
)


def _error(
    code: str,
    message: str,
    *,
    action_kind: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> ActionValidationResult:
    return ActionValidationResult(
        ok=False,
        error=ActionError(
            code=code,
            message=message,
            action_kind=action_kind,
            field=field,
            details=details or {},
        ),
    )


def _success(action: ActionSpec, params: BaseModel) -> ActionValidationResult:
    return ActionValidationResult(
        ok=True,
        action=action,
        params=params.model_dump(mode="json", by_alias=True),
    )


def _success_runtime_action(action: ActionSpec) -> ActionValidationResult:
    return ActionValidationResult(
        ok=True,
        action=action,
        params=action.params,
    )


def _validation_error_details(exc: ValidationError) -> dict[str, Any]:
    return {"errors": exc.errors(include_url=False, include_context=False)}


# Every wire code a validator in ``frisket.contracts.actions`` can raise, in
# match order. A code that is a SUBSTRING of another must come after it because
# the match below is a substring test. Both completeness and substring ordering
# are enforced mechanically by tests/test_action_params_validator_codes.py,
# which reads the raises straight out of the package; do not treat this tuple
# as a list anyone promised to keep in sync by hand.
VALIDATION_ERROR_CODES: tuple[str, ...] = (
    "duplicate_column_name",
    "invalid_column_type",
    "row_shape_mismatch",
    "invalid_rows_ref",
    "invalid_row_ref",
    "invalid_return_schema",
    "invalid_output_route",
    "duplicate_output_route",
    "duplicate_output_column",
    "invalid_transcription_engine",
    "invalid_ocr_engine",
    "invalid_to_markdown_engine",
    "invalid_language_selection",
    "diarization_unavailable",
    "diarization_speaker_cap",
    "transcription_option_unavailable",
    "searchable_pdf_unsupported_engine",
    "invalid_input_ref",
    "invalid_sheet_ref",
    "invalid_query_spec",
    "invalid_export_destination",
    "invalid_model",
    "invalid_temporal_value",
    "invalid_range",
    "range_out_of_bounds",
    "literal_selection_requires_confirmation",
)

# The code a validator that named nothing specific settles on. Never listed in
# VALIDATION_ERROR_CODES -- it is the fallback, not a match candidate.
VALIDATION_FALLBACK_CODE = "invalid_params"


def _code_from_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)
    for code in VALIDATION_ERROR_CODES:
        for error in errors:
            msg = str(error.get("msg", ""))
            ctx_error = str((error.get("ctx") or {}).get("error", ""))
            if code in msg or code in ctx_error:
                return code
    return VALIDATION_FALLBACK_CODE
