"""Compatibility exports for Project Ask action proposal validation."""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ActionRequest, InputReference, discover_references
from frisket.authoring.project_ask import ProjectAskRegisteredActionDraft

from frisket.authoring.project_ask_actions import (
    PreparedProjectAskDraft,
    describe_project_ask_action,
    prepare_validated_project_ask_draft,
    project_ask_action_catalog,
    search_project_ask_actions,
    validate_action_proposal,
)


def _bind_draft(
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[InputReference, ...]] | None:
    """Legacy builtin-only binding helper retained for focused wire tests.

    Project-aware callers must use ``prepare_validated_project_ask_draft`` so
    enabled plugin membership is checked against their catalog.
    """

    try:
        draft = ProjectAskRegisteredActionDraft.model_validate(spec, strict=True)
        data = draft.model_dump(mode="json", exclude_none=True)
        request = ActionRequest.model_validate(
            {**data, "idempotency_key": "project-ask-draft"}
        )
        bound = typed_action_for_request(request.model_dump(mode="json"))
    except (KeyError, TypeError, ValidationError, ValueError):
        return None
    return data, discover_references(bound.params)


__all__ = [
    "PreparedProjectAskDraft",
    "describe_project_ask_action",
    "prepare_validated_project_ask_draft",
    "project_ask_action_catalog",
    "search_project_ask_actions",
    "validate_action_proposal",
]
