"""Canonical, execution-neutral action proposal validation for authoring surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import SheetRows, discover_references
from frisket.engine.store import Project


def proposal_action_ids() -> frozenset[str]:
    """The established authoring allowlist exposed to proposal callers."""

    from frisket.authoring.copilot import WIRE_ACTION_KINDS

    return WIRE_ACTION_KINDS


def validate_action_proposals(
    project: Project,
    proposals: list[dict[str, Any]],
    *,
    scope: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate catalog drafts and close them over a selected Ask scope.

    The catalog's mature coercion remains the source of truth while Copilot
    still exposes its historical public helpers.  Ask then narrows valid
    sheet-row drafts to the rows the user selected.  This module never queues
    or executes an action.
    """

    from frisket.authoring.copilot import validate_proposals

    valid = validate_proposals(project, {"proposals": proposals})
    if scope is None or scope.get("kind") == "project":
        return valid
    return [proposal for proposal in valid if _restrict_scope(project, proposal, scope)]


def _restrict_scope(
    project: Project, proposal: dict[str, Any], source_scope: Mapping[str, Any]
) -> bool:
    spec = proposal.get("spec")
    if not isinstance(spec, dict):
        return False
    draft_scope = spec.get("scope")
    if not isinstance(draft_scope, dict) or draft_scope.get("kind") != "sheet_rows":
        return False
    sheet_id = draft_scope.get("sheet_id")
    if isinstance(sheet_id, bool) or not isinstance(sheet_id, int):
        return False
    sources = [
        source
        for source in source_scope.get("sources", [])
        if isinstance(source, Mapping) and source.get("sheet_id") == sheet_id
    ]
    if not sources:
        return False
    if any(source.get("kind") == "sheet" for source in sources):
        return True

    row_ids: set[int] = set()
    file_columns: set[int] = set()
    has_row_source = False
    for source in sources:
        if source.get("kind") == "rows":
            has_row_source = True
            row_ids.update(
                row_id
                for row_id in source.get("row_ids", [])
                if isinstance(row_id, int) and not isinstance(row_id, bool)
            )
        elif source.get("kind") == "file":
            row_id = source.get("row_id")
            column_id = source.get("column_id")
            if isinstance(row_id, int) and not isinstance(row_id, bool):
                row_ids.add(row_id)
            if isinstance(column_id, int) and not isinstance(column_id, bool):
                file_columns.add(column_id)
    if not row_ids:
        return False
    if (
        file_columns
        and not has_row_source
        and not _references_are_selected_file_columns(
            project, spec, sheet_id, file_columns
        )
    ):
        return False
    narrowed = dict(draft_scope)
    narrowed["row_ids"] = sorted(row_ids)
    spec["scope"] = narrowed
    return True


def _references_are_selected_file_columns(
    project: Project, spec: dict[str, Any], sheet_id: int, columns: set[int]
) -> bool:
    """Reject a file-only draft that would read an unrelated source column."""

    try:
        registered = ACTION_REGISTRY.get(str(spec["action_id"]))
        bound, _ = registered.bind_values(
            scope=SheetRows.model_validate(spec["scope"], strict=True),
            params=spec["params"],
            output_names=spec.get("output_names", {}),
        )
        references = discover_references(bound)
    except Exception:
        return False
    by_name = {
        str(column["name"]): int(column["id"]) for column in project.columns(sheet_id)
    }
    return all(by_name.get(reference.column) in columns for reference in references)
