"""Canonical, execution-neutral action proposal validation for Project Ask."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from frisket.actions.core import ColumnTransform
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import (
    ColumnTransformContext,
    InputReference,
    ProjectScope,
    SheetRows,
    discover_references,
)
from frisket.authoring.project_ask import (
    PROJECT_ASK_ACTION_KINDS,
    ProjectAskRegisteredActionDraft,
)
from frisket.engine.store import Project

logger = logging.getLogger("frisket.project_ask")


def proposal_action_ids() -> frozenset[str]:
    return PROJECT_ASK_ACTION_KINDS


def _bind_draft(
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[InputReference, ...]] | None:
    """Strictly bind one canonical, authorization-free action draft."""

    try:
        draft = ProjectAskRegisteredActionDraft.model_validate(spec, strict=True)
    except (TypeError, ValidationError, ValueError):
        return None
    if draft.action_id not in PROJECT_ASK_ACTION_KINDS:
        return None
    registered = ACTION_REGISTRY.get(draft.action_id)
    try:
        scope_data = draft.scope.model_dump()
        if scope_data.get("row_ids") is not None:
            scope_data["row_ids"] = tuple(scope_data["row_ids"])
        bound_params, _ = registered.bind_values(
            scope=(
                ProjectScope if draft.scope.kind == "project" else SheetRows
            ).model_validate(scope_data, strict=True),
            params=draft.params,
            output_names=draft.output_names,
        )
        references = discover_references(bound_params)
    except Exception:
        return None
    return draft.model_dump(mode="json", exclude_none=True), references


def _proposal_project_reference_error(
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]],
    params: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Check the bounded project references shared by Project Ask action params."""

    sheet_id = params.get("sheet_id")
    if sheet_id is not None:
        if sheet_id not in sheet_columns:
            return "unknown_sheet_id", {"sheet_id": sheet_id}
        _column_ids, columns_by_name = sheet_columns[sheet_id]
        for name, value in params.items():
            if not (
                name == "group_by"
                or name.endswith("_column")
                or name.endswith("_columns")
            ):
                continue
            values = value if isinstance(value, list) else [value]
            unknown = [item for item in values if item not in columns_by_name]
            if unknown:
                return "unknown_input_columns", {
                    "sheet_id": sheet_id,
                    "param": name,
                    "unknown_columns": unknown,
                }

    source_sheet_id = params.get("source_sheet_id")
    if source_sheet_id is not None:
        source = sheet_columns.get(source_sheet_id)
        if source is None:
            return "unknown_sheet_id", {"sheet_id": source_sheet_id}
        source_column_id = params.get("source_column_id")
        if source_column_id is not None and source_column_id not in source[0]:
            return "unknown_input_column_id", {
                "sheet_id": source_sheet_id,
                "column_id": source_column_id,
            }

    source = params.get("source")
    if isinstance(source, dict) and "sheet_id" in source:
        nested_sheet_id = source["sheet_id"]
        nested_sheet = sheet_columns.get(nested_sheet_id)
        if nested_sheet is None:
            return "unknown_sheet_id", {"sheet_id": nested_sheet_id}
        nested_column_id = source.get("column_id")
        if nested_column_id is not None and nested_column_id not in nested_sheet[0]:
            return "unknown_input_column_id", {
                "sheet_id": nested_sheet_id,
                "column_id": nested_column_id,
            }
    return None


def _registered_proposal_project_reference_error(
    project: Project,
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]],
    draft: dict[str, Any],
    references: tuple[InputReference, ...],
) -> tuple[str, dict[str, Any]] | None:
    if draft["scope"]["kind"] == "project":
        return _proposal_project_reference_error(sheet_columns, draft["params"])
    sheet_id = draft["scope"]["sheet_id"]
    sheet = sheet_columns.get(sheet_id)
    if sheet is None:
        return "unknown_sheet_id", {"sheet_id": sheet_id}
    requested_rows = draft["scope"].get("row_ids")
    if requested_rows is not None and set(
        project.visible_row_ids(sheet_id, requested_rows)
    ) != set(requested_rows):
        return "unknown_input_rows", {
            "sheet_id": sheet_id,
            "row_ids": requested_rows,
        }
    columns_by_name = sheet[1]
    unknown = [
        reference.column
        for reference in references
        if reference.column not in columns_by_name
    ]
    if unknown:
        return "unknown_input_columns", {
            "sheet_id": sheet_id,
            "param": "params",
            "unknown_columns": unknown,
        }
    incompatible = [
        {
            "name": reference.column,
            "actual_type": columns_by_name[reference.column],
            "accepted_column_types": list(reference.accepted_column_types),
        }
        for reference in references
        if reference.accepted_column_types is not None
        and reference.column in columns_by_name
        and columns_by_name[reference.column] not in reference.accepted_column_types
    ]
    if incompatible:
        return "incompatible_input_columns", {
            "sheet_id": sheet_id,
            "param": "params",
            "columns": incompatible,
        }
    action_id = draft["action_id"]
    terminal = ACTION_REGISTRY.get(action_id).definition.run
    from frisket.actions.core import ModelRows

    if isinstance(terminal, ModelRows):
        from frisket.engine.executor.map_rows_action import (
            TypedMapRowsPlanError,
            validate_model_rows_project_inputs,
        )

        try:
            typed_params = terminal.params_model.model_validate(
                draft["params"], strict=True
            )
            validate_model_rows_project_inputs(
                project,
                action_id=action_id,
                terminal=terminal,
                params=typed_params,
                sheet_id=sheet_id,
                row_ids=(
                    tuple(draft["scope"]["row_ids"])
                    if "row_ids" in draft["scope"]
                    else None
                ),
            )
        except TypedMapRowsPlanError as error:
            return error.code, dict(error.details)
        except ValidationError as error:
            return "invalid_params", {
                "sheet_id": sheet_id,
                "param": "params",
                "error_message": str(error),
            }
    if isinstance(terminal, ColumnTransform) and terminal.preflight is not None:
        params = terminal.params_model.model_validate(draft["params"], strict=True)
        [reference] = references
        try:
            terminal.preflight(
                params,
                ColumnTransformContext(source_type=columns_by_name[reference.column]),
            )
        except (TypeError, ValueError) as error:
            return "invalid_params", {
                "sheet_id": sheet_id,
                "param": "params",
                "error_message": str(error),
            }
    return None


def validate_action_proposal(
    project: Project,
    spec: Mapping[str, Any],
    *,
    scope: Mapping[str, Any] | None = None,
    title: str = "",
) -> dict[str, Any] | None:
    """Return one strict canonical draft when it is safe to offer for review."""

    sheet_columns: dict[int, tuple[set[int], dict[str, str]]] = {}
    for sheet in project.sheets():
        columns = project.columns(sheet["id"])
        sheet_columns[sheet["id"]] = (
            {column["id"] for column in columns},
            {str(column["name"]): str(column["type"]) for column in columns},
        )

    bound = _bind_draft(spec)
    if bound is None:
        logger.warning(
            "project_ask_proposal_dropped",
            extra={
                "event": "project_ask_proposal_dropped",
                "reason": "wire_contract_failed",
                "title": title,
            },
        )
        return None
    draft, references = bound
    if scope is not None and scope.get("kind") != "project":
        if not _restrict_scope(project, draft, scope):
            logger.warning(
                "project_ask_proposal_dropped",
                extra={
                    "event": "project_ask_proposal_dropped",
                    "reason": "outside_ask_scope",
                    "title": title,
                },
            )
            return None
        rebound = _bind_draft(draft)
        if rebound is None:
            logger.warning(
                "project_ask_proposal_dropped",
                extra={
                    "event": "project_ask_proposal_dropped",
                    "reason": "scope_binding_failed",
                    "title": title,
                },
            )
            return None
        draft, references = rebound
    reference_error = _registered_proposal_project_reference_error(
        project,
        sheet_columns,
        draft,
        references,
    )
    if reference_error is not None:
        reason, details = reference_error
        logger.warning(
            "project_ask_proposal_dropped",
            extra={
                "event": "project_ask_proposal_dropped",
                "reason": reason,
                "title": title,
                **details,
            },
        )
        return None
    return draft


def _restrict_scope(
    project: Project, spec: dict[str, Any], source_scope: Mapping[str, Any]
) -> bool:
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
    unrestricted_rows: set[int] = set()
    file_columns_by_row: dict[int, set[int]] = {}
    for source in sources:
        if source.get("kind") == "rows":
            source_rows = {
                row_id
                for row_id in source.get("row_ids", [])
                if isinstance(row_id, int) and not isinstance(row_id, bool)
            }
            row_ids.update(source_rows)
            unrestricted_rows.update(source_rows)
        elif source.get("kind") == "file":
            row_id = source.get("row_id")
            column_id = source.get("column_id")
            if isinstance(row_id, int) and not isinstance(row_id, bool):
                row_ids.add(row_id)
                if isinstance(column_id, int) and not isinstance(column_id, bool):
                    file_columns_by_row.setdefault(row_id, set()).add(column_id)
    if not row_ids:
        return False
    requested_rows = draft_scope.get("row_ids")
    if requested_rows is not None:
        if not set(requested_rows).issubset(row_ids):
            return False
        row_ids = set(requested_rows)
    if not _references_fit_selected_cells(
        project,
        spec,
        sheet_id,
        row_ids,
        unrestricted_rows,
        file_columns_by_row,
    ):
        return False
    narrowed = dict(draft_scope)
    narrowed["row_ids"] = sorted(row_ids)
    spec["scope"] = narrowed
    return True


def _references_fit_selected_cells(
    project: Project,
    spec: dict[str, Any],
    sheet_id: int,
    target_rows: set[int],
    unrestricted_rows: set[int],
    file_columns_by_row: dict[int, set[int]],
) -> bool:
    """Keep each file-selected row within its own selected input cells."""

    try:
        registered = ACTION_REGISTRY.get(str(spec["action_id"]))
        scope = dict(spec["scope"])
        if scope.get("row_ids") is not None:
            scope["row_ids"] = tuple(scope["row_ids"])
        bound, _ = registered.bind_values(
            scope=SheetRows.model_validate(scope, strict=True),
            params=spec["params"],
            output_names=spec.get("output_names", {}),
        )
        references = discover_references(bound)
    except Exception:
        return False
    by_name = {
        str(column["name"]): int(column["id"]) for column in project.columns(sheet_id)
    }
    reference_columns: set[int] = set()
    for reference in references:
        column_id = by_name.get(reference.column)
        if column_id is None:
            return False
        reference_columns.add(column_id)
    return all(
        row_id in unrestricted_rows
        or reference_columns <= file_columns_by_row.get(row_id, set())
        for row_id in target_rows
    )
