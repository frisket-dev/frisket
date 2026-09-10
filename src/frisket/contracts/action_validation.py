"""Validate the envelope of installed runtime-plugin actions.

Builtins bind typed ActionRequest objects through actions.system. Runtime
plugins retain their existing ActionSpec envelope and activation checks.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from frisket.contracts.action import (
    ACTION_SCHEMA_VERSION,
    ActionSpec,
    ActionValidationResult,
)
from frisket.contracts.actions.runtime import (
    runtime_action_binding_exists,
    validate_plugin_manifest_contributions,
)
from frisket.contracts.actions.validation_helpers import (
    _error,
    _success_runtime_action,
    _validation_error_details,
)

__all__ = [
    "payload_only_action_refusal",
    "validate_action_spec",
    "validate_plugin_manifest_contributions",
]


def _row_scope_refusal(action: ActionSpec) -> ActionValidationResult | None:
    if action.row_scope is None:
        return None
    return _error(
        "unsupported_row_scope",
        f"{action.kind} does not accept a row_scope",
        action_kind=action.kind,
        field="row_scope",
    )


def payload_only_action_refusal(data: Any) -> ActionValidationResult | None:
    """Refuse unsupported plugin row_scope before opening a project.

    Other validation remains at its existing boundary, after egress and
    stored-receipt replay checks.
    """

    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != ACTION_SCHEMA_VERSION:
        return None
    if not isinstance(data.get("kind"), str):
        return None
    try:
        action = ActionSpec.model_validate(dict(data))
    except ValidationError:
        return None
    return _row_scope_refusal(action)


def validate_action_spec(data: Any) -> ActionValidationResult:
    if not isinstance(data, dict):
        return _error("invalid_action_spec", "ActionSpec must be a JSON object")
    if data.get("schema_version") != ACTION_SCHEMA_VERSION:
        return _error(
            "unsupported_schema_version",
            "ActionSpec schema_version must be frisket.action.v2",
            details={"schema_version": data.get("schema_version")},
        )
    try:
        action = ActionSpec.model_validate(dict(data))
    except ValidationError as exc:
        return _error(
            "invalid_action_spec",
            "ActionSpec did not validate",
            details=_validation_error_details(exc),
        )

    from frisket.actions.registry import ACTION_REGISTRY

    # A runtime binding cannot claim a builtin's name or opt a builtin back
    # into the old envelope. Builtins require typed ActionRequest binding.
    if action.kind in ACTION_REGISTRY.action_ids:
        return _error(
            "unsupported_action_kind",
            "Builtin actions require a typed action_id request",
            action_kind=action.kind,
            field="kind",
        )
    if runtime_action_binding_exists(action.kind):
        scope_refusal = _row_scope_refusal(action)
        if scope_refusal is not None:
            return scope_refusal
        return _success_runtime_action(action)
    return _error(
        "unsupported_action_kind",
        "No installed runtime action binding exists for this kind",
        action_kind=action.kind,
        field="kind",
    )
