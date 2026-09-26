"""Canonical, execution-neutral action proposal validation for Project Ask."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from frisket.actions.core import ColumnTransform
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import root_action_catalog
from frisket.actions.types import (
    ColumnTransformContext,
    InputReference,
    ProjectScope,
    SheetRows,
    discover_references,
)
from frisket.authoring.project_ask import (
    PROJECT_ASK_ACTION_KINDS,
    PROJECT_ASK_CREATE_SHEET_KINDS,
    PROJECT_ASK_ROW_CREATE_SHEET_KINDS,
    ProjectAskRegisteredActionDraft,
)
from frisket.engine.store import Project

logger = logging.getLogger("frisket.project_ask")

WIRE_ACTION_FAMILIES = tuple(
    (
        family,
        tuple(
            kind for kind in PROJECT_ASK_ACTION_KINDS if kind.startswith(f"{family}.")
        ),
    )
    for family in (
        "map",
        "resolve",
        "derive",
        "reduce",
        "media",
        "enrich",
        "web",
        "research",
    )
)
WIRE_ACTION_KINDS = frozenset(
    kind for _family, kinds in WIRE_ACTION_FAMILIES for kind in kinds
)
_ACTION_CATALOG_BY_KIND = {entry.kind: entry for entry in root_action_catalog().actions}
_SAVED_AUTHORIZATION_FIELDS = frozenset({"confirmed", "consented_promise_set_hash"})
_FORBIDDEN_PROPOSAL_FIELDS = _SAVED_AUTHORIZATION_FIELDS | frozenset(
    {"params", "authoring_contract_version", "idempotency_key", "confirmation"}
)


def proposal_action_ids() -> frozenset[str]:
    return WIRE_ACTION_KINDS


def _coerce_proposal(
    kind: Any, spec: dict[str, Any]
) -> tuple[dict[str, Any], tuple[InputReference, ...]] | None:
    """Validate one flat model-authored spec and emit the saved envelope."""

    family = kind if isinstance(kind, str) else ""
    action_kind = spec.get("action_kind")
    if (
        family not in {candidate for candidate, _kinds in WIRE_ACTION_FAMILIES}
        or not isinstance(action_kind, str)
        or action_kind not in WIRE_ACTION_KINDS
        or action_kind.split(".", 1)[0] != family
        or _FORBIDDEN_PROPOSAL_FIELDS.intersection(spec)
    ):
        return None
    creates_sheet = action_kind in PROJECT_ASK_CREATE_SHEET_KINDS
    project_scoped = (
        creates_sheet and action_kind not in PROJECT_ASK_ROW_CREATE_SHEET_KINDS
    )
    target_fields = (
        {"sheet_name"}
        if project_scoped
        else {"sheet_id", "sheet_name"}
        if creates_sheet
        else {"sheet_id"}
    )
    params = {
        name: value
        for name, value in spec.items()
        if name not in {"action_kind", *target_fields, "output_names"}
    }
    output_names = spec.get("output_names", {})
    if not isinstance(output_names, dict):
        return None
    registered = ACTION_REGISTRY.get(action_kind)
    try:
        draft = ProjectAskRegisteredActionDraft.model_validate(
            {
                "action_id": action_kind,
                "scope": {"kind": "project"}
                if project_scoped
                else {"kind": "sheet_rows", "sheet_id": spec.get("sheet_id")},
                **({"sheet_name": spec.get("sheet_name")} if creates_sheet else {}),
                "params": params,
                "output_names": output_names,
            },
            strict=True,
        )
        bound_params, _ = registered.bind_values(
            scope=(ProjectScope if project_scoped else SheetRows).model_validate(
                draft.scope.model_dump(), strict=True
            ),
            params=draft.params,
            output_names=draft.output_names,
        )
        references = discover_references(bound_params)
    except Exception:
        return None
    return draft.model_dump(mode="json", exclude_none=True), references


def _prune_native_fill(spec: dict[str, Any]) -> dict[str, Any]:
    """Remove union-schema filler without erasing typed action intent.

    Native structured output may populate fields that belong to another
    action in the model-facing union. The selected typed action is the
    owner: discard undeclared fields and null filler, then let its strict
    params model decide whether every declared value is valid. In particular,
    ``False``, numeric zero, empty strings, and empty lists can be meaningful
    values and must not be rewritten into an action default.
    """

    action_kind = spec.get("action_kind")
    entry = (
        _ACTION_CATALOG_BY_KIND.get(action_kind)
        if isinstance(action_kind, str)
        else None
    )
    properties = entry.input_schema.get("properties", {}) if entry is not None else {}
    declared = set(properties) if isinstance(properties, dict) else set()
    target_field = (
        "sheet_name" if action_kind in PROJECT_ASK_CREATE_SHEET_KINDS else "sheet_id"
    )
    allowed = {"action_kind", target_field, *declared, *_FORBIDDEN_PROPOSAL_FIELDS}
    if action_kind in PROJECT_ASK_ROW_CREATE_SHEET_KINDS:
        allowed.add("sheet_id")
    allowed.add("output_names")
    return {
        key: value
        for key, value in spec.items()
        if key in allowed and (value is not None or key in _FORBIDDEN_PROPOSAL_FIELDS)
    }


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


def validate_proposals(project: Project, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate that proposals reference real sheets/columns; drop broken ones
    (with a logged reason — never a silent vanish,
    copilot-proposal-spec-schema-v1) and coerce each survivor into its strict,
    closed Project AskProposal wire spec."""
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]] = {}
    for sheet in project.sheets():
        columns = project.columns(sheet["id"])
        sheet_columns[sheet["id"]] = (
            {column["id"] for column in columns},
            {str(column["name"]): str(column["type"]) for column in columns},
        )
    valid: list[dict[str, Any]] = []
    for prop in data.get("proposals", []):
        title = prop.get("title", "")
        spec = prop.get("spec", {})
        if not isinstance(spec, dict):
            logger.warning(
                "project_ask_proposal_dropped",
                extra={
                    "event": "project_ask_proposal_dropped",
                    "reason": "spec_not_object",
                    "title": title,
                },
            )
            continue
        spec = _prune_native_fill(spec)
        coercion = _coerce_proposal(prop.get("kind"), spec)
        if coercion is None:
            logger.warning(
                "project_ask_proposal_dropped",
                extra={
                    "event": "project_ask_proposal_dropped",
                    "reason": "wire_contract_failed",
                    "title": title,
                    "kind": prop.get("kind"),
                },
            )
            continue
        coerced, references = coercion
        reference_error = _registered_proposal_project_reference_error(
            project,
            sheet_columns,
            coerced,
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
            continue
        valid.append({"kind": prop.get("kind"), "title": title, "spec": coerced})
    return valid


def validate_action_proposals(
    project: Project,
    proposals: list[dict[str, Any]],
    *,
    scope: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
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
