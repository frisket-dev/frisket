"""Project-aware catalog and canonical draft preparation for Project Ask."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError

from frisket.actions.core import ColumnTransform, ModelRows
from frisket.actions.system import (
    BoundTypedActionRequest,
    root_action_catalog_payload,
    typed_action_for_request,
)
from frisket.actions.types import (
    ActionRequest,
    ColumnTransformContext,
    InputReference,
    SheetColumnRef,
    SheetRef,
    discover_references,
)
from frisket.authoring.project_ask import ProjectAskRegisteredActionDraft
from frisket.authoring.workbench.installed_actions import bind_installed_action
from frisket.engine.store import Project

logger = logging.getLogger("frisket.project_ask")
_DRAFT_IDEMPOTENCY_KEY = "project-ask-draft"
_ASK_FORBIDDEN_CAPABILITIES = frozenset({"unsafe:local_code"})
_ASK_FORBIDDEN_EFFECTS = frozenset({"execute_trusted_local_python"})


@dataclass(frozen=True)
class PreparedProjectAskDraft:
    """A catalog-admitted, bound draft that is still unable to execute."""

    draft: dict[str, Any]
    request: ActionRequest
    bound: BoundTypedActionRequest
    catalog_entry: dict[str, Any]
    references: tuple[InputReference, ...]
    owned_output_inputs: tuple["OwnedOutputInput", ...] = ()

    @property
    def implementation_identity(self) -> Mapping[str, Any] | None:
        """Resolved builtin/plugin identity for a later execution boundary."""

        return self.bound.implementation_identity


@dataclass(frozen=True)
class OwnedOutputInput:
    """One receipt-proven derived cell used by a file-scoped Ask action."""

    receipt_id: str
    sheet_id: int
    row_id: int
    column_id: int


OutputGrantAllows = Callable[..., bool]


def _catalog_entries(catalog_payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    entries = catalog_payload.get("actions")
    if not isinstance(entries, list):
        raise ValueError("project action catalog is malformed")
    by_id: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("project action catalog is malformed")
        action_id = entry.get("kind")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("project action catalog is malformed")
        if action_id in by_id:
            raise ValueError("project action catalog has duplicate action ids")
        by_id[action_id] = dict(entry)
    return by_id


def project_ask_action_catalog(
    catalog_payload: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Return the exact builtin and enabled-plugin entries for one project."""

    return tuple(
        entry
        for _, entry in sorted(_catalog_entries(catalog_payload).items())
        if _available_for_project_ask(entry)
    )


def _available_for_project_ask(entry: Mapping[str, Any]) -> bool:
    """Generated local code never enters an Ask proposal or automatic path."""

    raw_capabilities = entry.get("required_capabilities")
    raw_effects = entry.get("side_effects")
    if (
        not isinstance(raw_capabilities, list)
        or not isinstance(raw_effects, list)
        or not all(isinstance(value, str) for value in raw_capabilities)
        or not all(isinstance(value, str) for value in raw_effects)
    ):
        return False
    hints = entry.get("ui_hints")
    if isinstance(hints, Mapping) and hints.get("unavailable_reason"):
        return False
    capabilities = set(raw_capabilities)
    effects = set(raw_effects)
    return not (
        capabilities.intersection(_ASK_FORBIDDEN_CAPABILITIES)
        or effects.intersection(_ASK_FORBIDDEN_EFFECTS)
    )


def describe_project_ask_action(
    catalog_payload: Mapping[str, Any], action_id: str
) -> dict[str, Any] | None:
    """Look up one action only if this project's catalog currently admits it."""

    if not isinstance(action_id, str) or not action_id:
        return None
    entry = _catalog_entries(catalog_payload).get(action_id)
    return entry if entry is not None and _available_for_project_ask(entry) else None


def search_project_ask_actions(
    catalog_payload: Mapping[str, Any], query: str, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    """Rank compact action facts from one authoritative project catalog."""

    terms = query.casefold().split()
    ranked: list[tuple[int, dict[str, Any]]] = []
    for entry in project_ask_action_catalog(catalog_payload):
        title = entry.get("title")
        description = entry.get("description")
        if not isinstance(title, str) or not isinstance(description, str):
            continue
        text = f"{entry['kind']} {title} {description}".casefold()
        score = sum(term in text for term in terms)
        if score:
            ranked.append((score, entry))
    ranked.sort(key=lambda item: (-item[0], str(item[1]["kind"])))
    return [entry for _, entry in ranked[:limit]], len(ranked) > limit


def _bind_project_action(
    project: Project, request: ActionRequest
) -> BoundTypedActionRequest:
    """Bind through the installed-plugin resolver before the builtin registry."""

    installed = bind_installed_action(project, request)
    if installed is not None:
        return installed
    return typed_action_for_request(request.model_dump(mode="json"))


def _bind_project_ask_draft(
    project: Project,
    spec: Mapping[str, Any],
    *,
    catalog_payload: Mapping[str, Any],
) -> PreparedProjectAskDraft:
    """Bind one keyless draft after catalog membership is established."""

    draft_model = ProjectAskRegisteredActionDraft.model_validate(spec, strict=True)
    draft = draft_model.model_dump(mode="json", exclude_none=True)
    catalog_entry = describe_project_ask_action(catalog_payload, draft_model.action_id)
    if catalog_entry is None:
        raise ValueError("action is not available for this project")
    request = ActionRequest.model_validate(
        {**draft, "idempotency_key": _DRAFT_IDEMPOTENCY_KEY}
    )
    bound = _bind_project_action(project, request)
    return PreparedProjectAskDraft(
        draft=draft,
        request=request,
        bound=bound,
        catalog_entry=catalog_entry,
        references=discover_references(bound.params),
    )


def _default_builtin_catalog() -> dict[str, Any]:
    """Compatibility fallback for direct callers without a project catalog grant."""

    return root_action_catalog_payload()


def _proposal_project_reference_error(
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]],
    params: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Check bounded secondary project references in action parameters."""

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


def _project_reference_error(
    project: Project,
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]],
    prepared: PreparedProjectAskDraft,
) -> tuple[str, dict[str, Any]] | None:
    draft = prepared.draft
    raw_reference_error = _proposal_project_reference_error(
        sheet_columns, draft["params"]
    )
    if raw_reference_error is not None:
        return raw_reference_error
    for reference in _bound_sheet_references(prepared.bound.params):
        sheet = sheet_columns.get(reference.sheet_id)
        if sheet is None:
            return "unknown_sheet_id", {"sheet_id": reference.sheet_id}
        if isinstance(reference, SheetColumnRef) and reference.column not in sheet[1]:
            return "unknown_input_columns", {
                "sheet_id": reference.sheet_id,
                "param": "params",
                "unknown_columns": [reference.column],
            }
    if draft["scope"]["kind"] == "project":
        return None
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
        for reference in prepared.references
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
        for reference in prepared.references
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
    terminal = prepared.bound.action.definition.run
    if isinstance(terminal, ModelRows):
        from frisket.engine.executor.map_rows_action import (
            TypedMapRowsPlanError,
            validate_model_rows_project_inputs,
        )

        try:
            validate_model_rows_project_inputs(
                project,
                action_id=prepared.request.action_id,
                terminal=terminal,
                params=prepared.bound.params,
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
        [reference] = prepared.references
        try:
            terminal.preflight(
                prepared.bound.params,
                ColumnTransformContext(source_type=columns_by_name[reference.column]),
            )
        except (TypeError, ValueError) as error:
            return "invalid_params", {
                "sheet_id": sheet_id,
                "param": "params",
                "error_message": str(error),
            }
    return None


def _bound_sheet_references(value: Any) -> tuple[SheetRef, ...]:
    """Find typed secondary project references without action-name branches."""

    from pydantic import BaseModel

    found: list[SheetRef] = []
    seen: set[int] = set()

    def visit(item: Any) -> None:
        if id(item) in seen:
            return
        if isinstance(item, SheetRef):
            seen.add(id(item))
            found.append(item)
            return
        if isinstance(item, BaseModel):
            seen.add(id(item))
            for field in type(item).model_fields:
                visit(getattr(item, field))
        elif isinstance(item, Mapping):
            seen.add(id(item))
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            seen.add(id(item))
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(found)


def _secondary_references_fit_scope(
    references: tuple[SheetRef, ...], source_scope: Mapping[str, Any]
) -> bool:
    """Whole-sheet secondary reads need an explicitly selected whole sheet."""

    if source_scope.get("kind") == "project":
        return True
    full_sheets = {
        source.get("sheet_id")
        for source in source_scope.get("sources", [])
        if isinstance(source, Mapping) and source.get("kind") == "sheet"
    }
    return all(reference.sheet_id in full_sheets for reference in references)


def _references_fit_selected_cells(
    project: Project,
    references: tuple[InputReference, ...],
    sheet_id: int,
    target_rows: set[int],
    unrestricted_rows: set[int],
    file_columns_by_row: dict[int, set[int]],
    output_grants: tuple[Any, ...],
    output_grant_allows: OutputGrantAllows | None,
) -> tuple[OwnedOutputInput, ...] | None:
    """Keep each file-selected row within its own selected input cells."""

    by_name = {
        str(column["name"]): int(column["id"]) for column in project.columns(sheet_id)
    }
    reference_columns: set[int] = set()
    for reference in references:
        column_id = by_name.get(reference.column)
        if column_id is None:
            return None
        reference_columns.add(column_id)
    owned_inputs: list[OwnedOutputInput] = []
    receipt_ids = tuple(
        sorted(
            {
                str(grant.receipt_id)
                for grant in output_grants
                if isinstance(getattr(grant, "receipt_id", None), str)
            }
        )
    )
    for row_id in target_rows:
        if row_id in unrestricted_rows:
            continue
        selected_columns = file_columns_by_row.get(row_id, set())
        for column_id in reference_columns - selected_columns:
            if output_grant_allows is None or not output_grant_allows(
                output_grants,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
            ):
                return None
            if not receipt_ids:
                return None
            # The shared predicate decides whether the cell is readable. Bind
            # the exact cell plus current receipt proofs without copying a
            # possibly large grant row set into an Ask event.
            owned_inputs.extend(
                OwnedOutputInput(receipt_id, sheet_id, row_id, column_id)
                for receipt_id in receipt_ids
            )
    return tuple(
        sorted(
            set(owned_inputs),
            key=lambda item: (
                item.receipt_id,
                item.sheet_id,
                item.row_id,
                item.column_id,
            ),
        )
    )


def _restrict_scope(
    project: Project,
    prepared: PreparedProjectAskDraft,
    source_scope: Mapping[str, Any],
    *,
    output_grants: tuple[Any, ...],
    output_grant_allows: OutputGrantAllows | None,
) -> tuple[OwnedOutputInput, ...] | None:
    draft_scope = prepared.draft.get("scope")
    if not isinstance(draft_scope, dict) or draft_scope.get("kind") != "sheet_rows":
        return None
    sheet_id = draft_scope.get("sheet_id")
    if isinstance(sheet_id, bool) or not isinstance(sheet_id, int):
        return None
    sources = [
        source
        for source in source_scope.get("sources", [])
        if isinstance(source, Mapping) and source.get("sheet_id") == sheet_id
    ]
    if not sources:
        return None
    if any(source.get("kind") == "sheet" for source in sources):
        return ()

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
        return None
    requested_rows = draft_scope.get("row_ids")
    if requested_rows is not None:
        if not set(requested_rows).issubset(row_ids):
            return None
        row_ids = set(requested_rows)
    owned_inputs = _references_fit_selected_cells(
        project,
        prepared.references,
        sheet_id,
        row_ids,
        unrestricted_rows,
        file_columns_by_row,
        output_grants,
        output_grant_allows,
    )
    if owned_inputs is None:
        return None
    prepared.draft["scope"] = {**draft_scope, "row_ids": sorted(row_ids)}
    return owned_inputs


def _default_output_grant_allows(
    grants: Iterable[Any], *, sheet_id: int, row_id: int, column_id: int
) -> bool:
    """Use the shared receipt scope boundary in composed server deployments."""

    from frisket.server.services.project_qa_output_scope import output_grant_allows

    return output_grant_allows(
        grants, sheet_id=sheet_id, row_id=row_id, column_id=column_id
    )


def prepare_validated_project_ask_draft(
    project: Project,
    spec: Mapping[str, Any],
    *,
    catalog_payload: Mapping[str, Any],
    scope: Mapping[str, Any] | None = None,
    output_grants: Iterable[Any] = (),
    output_grant_allows: OutputGrantAllows | None = None,
) -> PreparedProjectAskDraft:
    """Prepare a catalog-admitted draft for review or later execution.

    This validates the keyless canonical request, current project catalog,
    action-specific binder, referenced sheets/columns/rows, and an optional
    frozen Ask source scope. The result remains execution-neutral: a later
    executor must mint its own idempotency and approval envelope, then
    revalidate live state at that boundary.
    """

    prepared = _bind_project_ask_draft(project, spec, catalog_payload=catalog_payload)
    current_output_grants = tuple(output_grants)
    if current_output_grants and output_grant_allows is None:
        output_grant_allows = _default_output_grant_allows
    if scope is not None and scope.get("kind") != "project":
        owned_output_inputs = _restrict_scope(
            project,
            prepared,
            scope,
            output_grants=current_output_grants,
            output_grant_allows=output_grant_allows,
        )
        if owned_output_inputs is None:
            raise ValueError("action draft is outside the Ask source scope")
        prepared = _bind_project_ask_draft(
            project, prepared.draft, catalog_payload=catalog_payload
        )
        prepared = replace(prepared, owned_output_inputs=owned_output_inputs)
        if not _secondary_references_fit_scope(
            _bound_sheet_references(prepared.bound.params), scope
        ):
            raise ValueError("action draft reads a sheet outside the Ask source scope")
    sheet_columns: dict[int, tuple[set[int], dict[str, str]]] = {}
    for sheet in project.sheets():
        columns = project.columns(sheet["id"])
        sheet_columns[sheet["id"]] = (
            {column["id"] for column in columns},
            {str(column["name"]): str(column["type"]) for column in columns},
        )
    reference_error = _project_reference_error(project, sheet_columns, prepared)
    if reference_error is not None:
        reason, _details = reference_error
        raise ValueError(f"action draft has invalid project inputs: {reason}")
    return prepared


def validate_action_proposal(
    project: Project,
    spec: Mapping[str, Any],
    *,
    catalog_payload: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
    title: str = "",
) -> dict[str, Any] | None:
    """Return one catalog-admitted canonical draft safe to offer for review."""

    if catalog_payload is None:
        catalog_payload = _default_builtin_catalog()
    try:
        prepared = prepare_validated_project_ask_draft(
            project, spec, catalog_payload=catalog_payload, scope=scope
        )
    except (KeyError, TypeError, ValidationError, ValueError):
        logger.warning(
            "project_ask_proposal_dropped",
            extra={
                "event": "project_ask_proposal_dropped",
                "reason": "wire_contract_failed",
                "title": title,
            },
        )
        return None
    return prepared.draft
