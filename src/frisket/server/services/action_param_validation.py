"""Server-truthful param preflight for the action forms.

A tiny dedicated seam beside the estimate route (services/action_previews.py).
Typed actions project their canonical Pydantic Params verdict through this
service and resolve request-dependent outputs that a static catalog cannot
enumerate. Legacy actions and plugins continue to run explicitly declared
field validators.

Declarations are read from the launcher-safe base catalog (first-party actions)
and, only when a kind is not first-party, from the project's plugin action
entries — so a plugin manifest that declares the same ``param_validators`` key
is validated by the same path with no per-plugin wiring.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from frisket.actions.registry import ACTION_REGISTRY, NEW_ACTION_IDS
from frisket.actions.types import discover_references, source_kind
from frisket.authoring.action_metadata import (
    action_available_in_edition,
    action_edition_unavailable_message,
)
from frisket.server.param_validation import (
    ParamValidationContext,
    validate_param,
    validator_keys,
)
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


ACTION_PARAM_VALIDATION_RESULT_SCHEMA_VERSION = (
    "frisket.action_param_validation_result.v1"
)
_MODEL_DIAGNOSTIC = "__all__"

_TYPED_ERROR_MESSAGES = {
    "missing": "This field is required.",
    "string_too_short": "Enter a value.",
    "string_too_long": "Shorten this value.",
    "string_type": "Enter text.",
    "too_short": "Select at least one value.",
    "too_long": "Choose fewer values.",
    "list_type": "Select one or more values.",
    "dict_type": "Enter a valid value.",
    "model_type": "Enter a valid value.",
    "literal_error": "Choose one of the available options.",
    "int_type": "Enter a whole number.",
    "int_parsing": "Enter a whole number.",
    "float_type": "Enter a number.",
    "float_parsing": "Enter a number.",
    "bool_type": "Choose yes or no.",
    "bool_parsing": "Choose yes or no.",
    "extra_forbidden": "This value is not allowed.",
    "category_labels_required": "Add at least one label.",
    "category_labels_unique": "Category labels must be unique.",
    "category_labels_forbidden": "Only category fields can declare labels.",
    "category_label_descriptions_unknown": "Describe only declared category labels.",
    "category_label_descriptions_required": "Category label descriptions cannot be blank.",
}


def _declared_validators(source: Mapping[str, Any]) -> dict[str, str]:
    hints = source.get("ui_hints")
    declared = hints.get("param_validators") if isinstance(hints, Mapping) else None
    if not isinstance(declared, Mapping):
        return {}
    keys = validator_keys()
    return {
        str(param): str(key)
        for param, key in declared.items()
        if isinstance(key, str) and key in keys
    }


def _unwrapped_annotation(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _union_members(annotation: Any) -> tuple[Any, ...]:
    annotation = _unwrapped_annotation(annotation)
    if get_origin(annotation) in (Union, UnionType):
        return get_args(annotation)
    return ()


def _matches_submitted_shape(annotation: Any, value: Any) -> bool:
    """Match a JSON value to a union branch without using its display name."""

    annotation = _unwrapped_annotation(annotation)
    origin = get_origin(annotation)
    if origin is list:
        return isinstance(value, list)
    if origin is tuple:
        return isinstance(value, (list, tuple))
    if origin is dict or origin is Mapping:
        return isinstance(value, Mapping)
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return isinstance(value, Mapping)
        return isinstance(value, annotation)
    return False


def _submitted_union_member(annotation: Any, value: Any) -> Any | None:
    candidates = [
        member
        for member in _union_members(annotation)
        if _matches_submitted_shape(member, value)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _union_member_shape(annotation: Any) -> str | None:
    annotation = _unwrapped_annotation(annotation)
    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset):
        return "array"
    if origin is dict or origin is Mapping:
        return "object"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    return None


def _selected_union_detail(
    details: list[dict[str, Any]], annotation: Any, value: Any
) -> dict[str, Any] | None:
    """Drop only direct shape mismatches from Pydantic's existing verdict."""

    member = _submitted_union_member(annotation, value)
    if member is None:
        return None
    shape = _union_member_shape(member)
    mismatches = {
        "array": frozenset({"model_type", "dict_type"}),
        "object": frozenset({"list_type", "tuple_type", "set_type", "frozenset_type"}),
    }.get(shape, frozenset())
    for detail in details:
        location = tuple(detail.get("loc", ()))
        if len(location) == 2 and str(detail.get("type")) in mismatches:
            continue
        return detail
    return None


def _typed_error_message(detail: Mapping[str, Any], annotation: Any) -> str:
    """Expose stable, actionable copy without leaking Pydantic type names."""

    if detail.get("type") == "value_error":
        return str(detail.get("msg") or "Enter a valid value.").removeprefix(
            "Value error, "
        )
    if detail.get("type") == "too_short":
        minimum = detail.get("ctx", {}).get("min_length")
        if source_kind(annotation) in {"columns", "columns_or_template"}:
            return "Choose at least one input column."
        if isinstance(minimum, int) and minimum > 1:
            return f"Choose at least {minimum} values."
    return _TYPED_ERROR_MESSAGES.get(str(detail.get("type")), "Enter a valid value.")


def _typed_param_diagnostics(
    error: ValidationError,
    *,
    params_model: type[BaseModel],
    raw_params: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Project Pydantic's canonical Params verdict onto form fields.

    Nested errors belong to their top-level control (for example ``routes``),
    while model-level validators use one explicit form-wide key. A field gets
    its first actionable error only. Union branch-shape mismatches are removed
    from Pydantic's existing structured verdict, so a list never reports a
    competing object branch (or its internal type name).
    """

    details_by_field: dict[str, list[dict[str, Any]]] = {}
    for detail in error.errors(include_url=False, include_input=False):
        location = detail.get("loc", ())
        field = (
            str(location[0])
            if location and isinstance(location[0], str)
            else _MODEL_DIAGNOSTIC
        )
        details_by_field.setdefault(field, []).append(detail)

    diagnostics: dict[str, dict[str, Any]] = {}
    for field, details in details_by_field.items():
        model_field = params_model.model_fields.get(field)
        selected = None
        selected_union = False
        if (
            model_field is not None
            and field in raw_params
            and all(len(tuple(detail.get("loc", ()))) > 1 for detail in details)
        ):
            selected = _selected_union_detail(
                details, model_field.annotation, raw_params[field]
            )
            selected_union = selected is not None
        detail = selected or details[0]
        location = tuple(detail.get("loc", ()))
        if field == _MODEL_DIAGNOSTIC:
            path = list(location)
        elif selected_union:
            path = list(location[2:])
        else:
            path = list(location[1:])
        code = str(detail.get("type") or "value_error")
        diagnostics[field] = {
            "ok": False,
            "message": _typed_error_message(
                detail, model_field.annotation if model_field is not None else None
            ),
            "code": code,
            **({"path": path} if path else {}),
        }
    return diagnostics


def _typed_reference_diagnostics(params: Any, error: Any) -> dict[str, dict[str, Any]]:
    """Project one shared project-reference refusal onto its Params fields."""

    raw_columns = list(error.details.get("columns", ()))
    if isinstance(error.details.get("column"), str):
        raw_columns.append({"name": error.details["column"]})
    problem_columns = {
        str(item.get("name")) if isinstance(item, Mapping) else str(item)
        for item in raw_columns
    }
    fields = {
        name: sorted(
            reference.column
            for reference in discover_references(getattr(params, name))
            if reference.column in problem_columns
        )
        for name in params.__class__.model_fields
    }
    fields = {name: columns for name, columns in fields.items() if columns}
    missing = [item for item in raw_columns if isinstance(item, str)]
    message = (
        f'No column named "{missing[0]}" on this sheet.'
        if len(missing) == 1
        else str(error)
    )
    return {name: {"ok": False, "message": message} for name in fields} or {
        _MODEL_DIAGNOSTIC: {"ok": False, "message": message}
    }


def _typed_action_error_diagnostics(
    params: Any, error: Any
) -> dict[str, dict[str, Any]]:
    """Put a typed terminal refusal on its source field when identifiable."""

    column = error.details.get("column")
    if isinstance(column, str):
        fields = [
            name
            for name in params.__class__.model_fields
            if any(
                reference.column == column
                for reference in discover_references(getattr(params, name))
            )
        ]
        if fields:
            message = (
                f'No column named "{column}" on this sheet.'
                if "actual_type" not in error.details
                else error.message
            )
            return {name: {"ok": False, "message": message} for name in fields}
    return {_MODEL_DIAGNOSTIC: {"ok": False, "message": error.message}}


class ActionParamValidationService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def _validators_for_kind(self, project_id: str, kind: str) -> dict[str, str]:
        # Typed actions validate through their registered Params below.
        # Legacy plugin validators come from the installed project's catalog.
        from frisket.server.action_catalog_hints import (
            _project_plugin_action_catalog_entries,
        )

        project = self._workspace.get(project_id)
        for entry in _project_plugin_action_catalog_entries(project):
            if entry.get("kind") == kind:
                return _declared_validators(entry)
        return {}

    def _context_for(
        self, project_id: str, params: dict[str, Any]
    ) -> ParamValidationContext:
        # The ``template`` validator judges ``{{placeholder}}`` tokens against
        # the target sheet's real columns; resolve them from ``params.sheet_id``.
        # Any miss (no sheet_id, sheet gone, project unreadable) yields an empty
        # column set, so the validator passes rather than false-alarming.
        sheet_id = params.get("sheet_id")
        if not isinstance(sheet_id, int) or isinstance(sheet_id, bool):
            return ParamValidationContext()
        try:
            project = self._workspace.get(project_id)
            names = {str(column["name"]) for column in project.columns(int(sheet_id))}
        except Exception:
            return ParamValidationContext()
        return ParamValidationContext(column_names=frozenset(names))

    def validate_params(
        self, project_id: str, action: dict[str, Any]
    ) -> dict[str, Any]:
        kind = str(action.get("kind") or action.get("action_id") or "").strip()
        if not action_available_in_edition(kind, self._workspace.edition):
            raise RouteError(
                400,
                action_edition_unavailable_message(kind, self._workspace.edition),
            )
        params = action.get("params")
        params = params if isinstance(params, dict) else {}
        logical_outputs: list[dict[str, Any]] = []
        creates_sheet: bool | None = None
        diagnostics: dict[str, Any] = {}
        from frisket.authoring.workbench.installed_actions import (
            bind_installed_action,
            resolve_installed_action,
        )

        registered = None
        declarations = {}
        try:
            if kind in NEW_ACTION_IDS:
                registered = ACTION_REGISTRY.get(kind)
            else:
                installed = resolve_installed_action(
                    self._workspace.get(project_id), kind
                )
                if installed is not None:
                    registered = installed[0]
        except (KeyError, TypeError, ValueError) as error:
            diagnostics = {_MODEL_DIAGNOSTIC: {"ok": False, "message": str(error)}}
        if registered is not None:
            terminal = registered.definition.run
            try:
                typed_params = terminal.params_model.model_validate(params)
                from frisket.actions.core import CreateSheet

                fields = terminal.resolve_output_fields(typed_params)
                if isinstance(terminal, CreateSheet):
                    creates_sheet = True
                    if fields is None:
                        from frisket.actions.system import BoundTypedActionRequest
                        from frisket.actions.types import ActionRequest
                        from frisket.engine.executor.table_action import (
                            builtin_table_source,
                        )
                        from frisket.engine.executor.table_preview import (
                            table_preview_refusal,
                        )

                        request = ActionRequest.model_validate(
                            {
                                **action,
                                "action_id": kind,
                                # Schema discovery has no destination to publish.
                                "sheet_name": "schema-discovery",
                                "idempotency_key": action.get("idempotency_key")
                                or "parameter-validation",
                            }
                        )
                        bound = (
                            BoundTypedActionRequest.bind(registered, request)
                            if kind in NEW_ACTION_IDS
                            else bind_installed_action(
                                self._workspace.get(project_id), request
                            )
                        )
                        if bound is None:
                            raise ValueError("Installed action is no longer available")
                        refusal = table_preview_refusal(bound)
                        if refusal is not None:
                            raise ValueError(refusal.message)
                        with builtin_table_source(
                            self._workspace.get(project_id), project_id, bound
                        ).prepare(check_sheet_name=False, schema_only=True) as table:
                            fields = table.output_fields
                logical_outputs = [
                    {"key": field.key, "column_type": field.column_type}
                    for field in (fields or ())
                ]
                scope = action.get("scope")
                sheet_id = scope.get("sheet_id") if isinstance(scope, Mapping) else None
                if (
                    isinstance(sheet_id, int)
                    and not isinstance(sheet_id, bool)
                    and sheet_id > 0
                ):
                    from frisket.actions.core import (
                        ColumnTransform,
                        CreateSheet,
                        ModelRows,
                        SemanticJoin,
                        _ProjectAction,
                    )
                    from frisket.actions.join_types import JoinedTablesReader
                    from frisket.contracts.action import ActionError
                    from frisket.engine.executor.column_transform_action import (
                        preflight_column_transform_source,
                    )
                    from frisket.engine.executor.map_rows_action import (
                        TypedMapRowsPlanError,
                        validate_model_rows_project_inputs,
                        validate_typed_project_references,
                    )
                    from frisket.engine.executor.temporal_extract_action import (
                        supports_typed_temporal_extract_action,
                        resolve_temporal_extract_outputs,
                    )

                    join_schema = isinstance(
                        terminal, CreateSheet
                    ) and terminal.capabilities == (JoinedTablesReader,)
                    from frisket.actions.page_capture_types import PageCapturer

                    capture_schema = isinstance(
                        terminal, _ProjectAction
                    ) and terminal.capabilities == (PageCapturer,)
                    if (
                        supports_typed_temporal_extract_action(terminal)
                        or join_schema
                        or capture_schema
                    ):
                        from frisket.actions.system import BoundTypedActionRequest
                        from frisket.actions.types import ActionRequest

                        bound = BoundTypedActionRequest.bind(
                            registered,
                            ActionRequest.model_validate(
                                {
                                    "action_id": kind,
                                    "scope": scope,
                                    "params": params,
                                    "idempotency_key": "validate-params",
                                    **(
                                        {"sheet_name": "schema-discovery"}
                                        if join_schema
                                        else {}
                                    ),
                                }
                            ),
                        )
                        project = self._workspace.get(project_id)
                        if capture_schema:
                            from frisket.engine.executor.page_capture_action import (
                                CaptureRefused,
                                prepare_page_capture_action,
                            )

                            try:
                                prepared = prepare_page_capture_action(
                                    project, bound, admission=False
                                )
                                resolved = list(prepared.output_fields)
                                creates_sheet = prepared.creates_sheet
                            except CaptureRefused as error:
                                resolved = error.error
                        elif join_schema:
                            from frisket.engine.executor.table_action import (
                                TableReadRefused,
                                prepare_table_producer,
                            )

                            try:
                                with prepare_table_producer(
                                    project,
                                    bound,
                                    check_sheet_name=False,
                                    schema_only=True,
                                ) as prepared:
                                    resolved = [
                                        {"key": column.name, "column_type": column.type}
                                        for column in prepared.table.columns
                                    ]
                            except TableReadRefused as error:
                                resolved = error.error
                        else:
                            resolved = resolve_temporal_extract_outputs(project, bound)
                        if isinstance(resolved, ActionError):
                            diagnostics = _typed_action_error_diagnostics(
                                typed_params, resolved
                            )
                        else:
                            logical_outputs = resolved
                    elif isinstance(terminal, ColumnTransform):
                        error = preflight_column_transform_source(
                            self._workspace.get(project_id),
                            action_id=kind,
                            terminal=terminal,
                            params=typed_params,
                            sheet_id=sheet_id,
                        )
                        if isinstance(error, ActionError):
                            diagnostics = _typed_action_error_diagnostics(
                                typed_params, error
                            )
                    else:
                        try:
                            if isinstance(terminal, ModelRows):
                                raw_row_ids = scope.get("row_ids")
                                validate_model_rows_project_inputs(
                                    self._workspace.get(project_id),
                                    action_id=kind,
                                    terminal=terminal,
                                    params=typed_params,
                                    sheet_id=sheet_id,
                                    row_ids=(
                                        tuple(raw_row_ids)
                                        if isinstance(raw_row_ids, list)
                                        else None
                                    ),
                                )
                            else:
                                validate_typed_project_references(
                                    self._workspace.get(project_id),
                                    sheet_id,
                                    typed_params,
                                )
                                if isinstance(terminal, SemanticJoin):
                                    columns = {
                                        column["name"]: column
                                        for column in self._workspace.get(
                                            project_id
                                        ).columns(sheet_id)
                                    }
                                    logical_outputs.extend(
                                        {
                                            "key": key,
                                            "column_type": str(
                                                columns[ref.name]["type"]
                                            ),
                                        }
                                        for key, ref in terminal.child_source_columns(
                                            typed_params
                                        ).items()
                                    )
                        except TypedMapRowsPlanError as error:
                            diagnostics = _typed_reference_diagnostics(
                                typed_params, error
                            )
            except ValidationError as error:
                # Forms submit partial Params while the user is still editing.
                # Pydantic remains the one validation authority; dynamic
                # outputs become available once the complete model is valid.
                diagnostics = _typed_param_diagnostics(
                    error,
                    params_model=terminal.params_model,
                    raw_params=params,
                )
            except (TypeError, ValueError) as error:
                diagnostics = {_MODEL_DIAGNOSTIC: {"ok": False, "message": str(error)}}
            declarations: dict[str, str] = {}
        else:
            declarations = self._validators_for_kind(project_id, kind)
        context = (
            self._context_for(project_id, params)
            if any(key == "template" for key in declarations.values())
            else None
        )
        for param_name, validator_key in declarations.items():
            if param_name not in params:
                continue
            raw = params[param_name]
            value = "" if raw is None else str(raw)
            diagnostics[param_name] = validate_param(
                validator_key, value, context
            ).to_payload()
        return {
            "schema_version": ACTION_PARAM_VALIDATION_RESULT_SCHEMA_VERSION,
            "action": {"kind": kind},
            "project_id": project_id,
            "diagnostics": diagnostics,
            "logical_outputs": logical_outputs,
            **({"creates_sheet": creates_sheet} if creates_sheet is not None else {}),
        }
