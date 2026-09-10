"""Root validation and catalog projection for native typed actions."""

from dataclasses import dataclass
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from frisket.authoring.plugin_registry import RuntimeBindingSpec

from pydantic import BaseModel, TypeAdapter, ValidationError

from frisket.actions.core import OutputField, RegisteredAction
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest
from frisket.contracts.action import (
    ActionCatalog,
    ActionCatalogEntry,
    ActionError,
    ActionResult,
    Receipt,
    ActionValidationResult,
)


def action_id_from_request(body: dict[str, Any]) -> str | None:
    value = body.get("action_id")
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class BoundTypedActionRequest:
    action: RegisteredAction
    request: ActionRequest
    params: BaseModel
    output_fields: tuple[OutputField, ...] | None
    implementation_identity: Mapping[str, Any] | None = None
    runtime_binding: "RuntimeBindingSpec | None" = None

    @classmethod
    def bind(cls, action: RegisteredAction, request: ActionRequest) -> Self:
        params, output_fields = action.bind_request(request)
        return cls(action.for_execution(params), request, params, output_fields)


class RootActionValidationResult(ActionValidationResult):
    """Validation result for the native action request boundary."""

    action: ActionRequest | None = None


def typed_action_for_request(
    body: dict[str, Any],
) -> BoundTypedActionRequest:
    action_id = body.get("action_id")
    if not isinstance(action_id, str):
        raise ValueError("typed action request requires action_id")
    registered = ACTION_REGISTRY.get(action_id)
    request = ActionRequest.model_validate(body)
    return BoundTypedActionRequest.bind(registered, request)


def validate_root_action(data: Any) -> RootActionValidationResult:
    """Validate one native ActionRequest against its registered owner."""

    if not isinstance(data, dict):
        return RootActionValidationResult(
            ok=False,
            error=ActionError(
                code="invalid_action_request",
                message="typed action request must be a JSON object",
            ),
        )
    action_id = action_id_from_request(data)
    try:
        bound = typed_action_for_request(data)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return RootActionValidationResult(
            ok=False,
            error=ActionError(
                code="invalid_action_request",
                message=str(exc),
                action_kind=action_id,
            ),
        )
    return RootActionValidationResult(
        ok=True,
        action=bound.request,
        params=bound.params.model_dump(mode="json", by_alias=True, exclude_unset=True),
    )


def root_action_catalog() -> ActionCatalog:
    from frisket.engine.executor.action_dispatch import is_queued

    new_entries = []
    for action in ACTION_REGISTRY.actions:
        entry = action.catalog_entry()
        # Placement belongs to the host execution registry, not the portable
        # authoring declaration. Project it only at the root catalog boundary.
        entry["async_mode"] = "queued" if is_queued(action.action_id) else "sync"
        new_entries.append(ActionCatalogEntry.model_validate(entry))
    return ActionCatalog(
        action_schema=TypeAdapter(ActionRequest).json_schema(),
        error_schema=ActionError.model_json_schema(),
        result_schema=ActionResult.model_json_schema(),
        receipt_schema=Receipt.model_json_schema(),
        validation_result_schema=RootActionValidationResult.model_json_schema(),
        actions=new_entries,
    )


def root_action_catalog_payload() -> dict[str, Any]:
    return root_action_catalog().model_dump(mode="json")
