"""Small shared workflow primitives for CSV, XLSX, and pasted-row imports."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from frisket.contracts.action import (
    ActionError,
    ActionOutput,
    ActionResult,
    ImportRowsColumn,
)
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.import_update import ImportUpdatePlan, plan_import_update
from frisket.engine.store.receipts import ReceiptStore
from frisket.features.temporal_ingress import preflight_typed_rows_for_persistence

if TYPE_CHECKING:
    from frisket.actions.import_xlsx import ImportXlsxParams, UpdateXlsxParams
    from frisket.actions.imports import (
        ImportCsvParams,
        ImportRowsParams,
        UpdateCsvParams,
        UpdateRowsParams,
    )


@dataclass(frozen=True)
class ResolvedImportMapping:
    columns: tuple[ImportRowsColumn, ...]
    key_columns: tuple[str, ...] = ()


def decode_destination_mapping(
    project: Any,
    *,
    sheet_id: int,
    source_names: Sequence[str],
    column_mapping: str | Mapping[str, str | None],
    key_columns: str | Sequence[str] | None = None,
) -> ResolvedImportMapping:
    """Decode a route mapping, then resolve it against the live sheet."""

    try:
        requested = (
            json.loads(column_mapping)
            if isinstance(column_mapping, str)
            else dict(column_mapping)
        )
        keys = (
            json.loads(key_columns)
            if isinstance(key_columns, str)
            else (() if key_columns is None else list(key_columns))
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("import mapping is invalid") from exc
    if not isinstance(requested, dict) or not isinstance(keys, (list, tuple)):
        raise ValueError("import mapping is invalid")
    if any(not isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("import mapping is invalid")
    return resolve_destination_mapping(
        project,
        sheet_id=sheet_id,
        source_names=source_names,
        requested=requested,
        key_columns=keys,
        require_keys=key_columns is not None,
    )


def run_tabular_action(
    project: Any,
    *,
    project_id: str,
    params: (
        "ImportRowsParams | UpdateRowsParams | ImportCsvParams | UpdateCsvParams | "
        "ImportXlsxParams | UpdateXlsxParams"
    ),
    idempotency_key: str,
    deps: ExecutorDeps,
    sheet_name: str | None = None,
    destination_sheet_id: int | None = None,
    confirmation: str | None = None,
    router: Any | None = None,
) -> ActionResult:
    """Run an existing typed tabular action with its ordinary request envelope."""

    from frisket.actions.imports import (
        ImportCsvParams,
        ImportRowsParams,
        UpdateCsvParams,
        UpdateRowsParams,
    )
    from frisket.actions.import_xlsx import ImportXlsxParams, UpdateXlsxParams

    if isinstance(params, UpdateRowsParams):
        action_id, mode = "import.update_rows", "update"
    elif isinstance(params, UpdateCsvParams):
        action_id, mode = "import.update_csv", "update"
    elif isinstance(params, UpdateXlsxParams):
        action_id, mode = "import.update_xlsx", "update"
    elif isinstance(params, ImportRowsParams):
        action_id, mode = (
            "import.rows",
            "create" if destination_sheet_id is None else "append",
        )
    elif isinstance(params, ImportCsvParams):
        action_id, mode = (
            "import.csv",
            "create" if destination_sheet_id is None else "append",
        )
    elif isinstance(params, ImportXlsxParams):
        action_id, mode = (
            "import.xlsx",
            "create" if destination_sheet_id is None else "append",
        )
    else:
        raise TypeError("unsupported tabular import params")

    if mode == "create":
        if not sheet_name:
            raise ValueError("import create requires a sheet name")
        if confirmation is not None:
            raise ValueError("import create does not accept confirmation")
        final_name = resolve_import_sheet_name(
            project,
            request_key=idempotency_key,
            action_kind=action_id,
            requested_name=sheet_name,
        )
        request = {
            "action_id": action_id,
            "scope": {"kind": "project"},
            "sheet_name": final_name,
            "params": params.model_dump(mode="json", exclude_unset=True),
            "idempotency_key": idempotency_key,
        }
    else:
        if destination_sheet_id is None:
            raise ValueError("import append/update requires a destination sheet")
        if mode == "update":
            if not confirmation:
                raise ValueError("import update requires confirmation")
        elif confirmation is not None:
            raise ValueError("import append does not accept confirmation")
        append_action = {
            "import.rows": "import.append_rows",
            "import.csv": "import.append_csv",
            "import.xlsx": "import.append_xlsx",
        }
        request = {
            "action_id": action_id if mode == "update" else append_action[action_id],
            "scope": {"kind": "sheet_rows", "sheet_id": destination_sheet_id},
            "params": params.model_dump(mode="json", exclude_unset=True),
            "idempotency_key": idempotency_key,
        }
        if mode == "update":
            request["confirmation"] = confirmation
    return run_action_spec(
        project, request, project_id=project_id, router=router, deps=deps
    )


def resolve_destination_mapping(
    project: Any,
    *,
    sheet_id: int,
    source_names: Sequence[str],
    requested: Mapping[str, str | None],
    key_columns: Sequence[str] = (),
    require_keys: bool = False,
) -> ResolvedImportMapping:
    """Resolve reviewed source names to visible destination columns."""

    if set(requested) != set(source_names):
        raise ValueError("import mapping must review every source column")
    if (
        project.db.execute("SELECT 1 FROM sheets WHERE id=?", (sheet_id,)).fetchone()
        is None
    ):
        raise ValueError("sheet_not_found")
    keys = tuple(key_columns)
    if require_keys and (not keys or any(not isinstance(key, str) for key in keys)):
        raise ValueError("import update requires at least one key column")
    targets = {
        str(row["name"]): row
        for row in project.db.execute(
            "SELECT name,type,format FROM columns WHERE sheet_id=? AND hidden=0",
            (sheet_id,),
        )
    }
    columns: list[ImportRowsColumn] = []
    for source_name in source_names:
        target_name = requested[source_name]
        if target_name is None:
            continue
        if not isinstance(target_name, str) or target_name not in targets:
            raise ValueError("import mapping names an unavailable target column")
        target = targets[target_name]
        columns.append(
            ImportRowsColumn(
                name=target_name,
                source_name=source_name,
                type=str(target["type"]),
                format=target["format"],
            )
        )
    names = {column.name for column in columns}
    if not columns:
        raise ValueError("import mapping must include at least one column")
    if len(names) != len(columns):
        raise ValueError("import mapping targets must be unique")
    if require_keys and (not set(keys).issubset(names) or set(keys) == names):
        raise ValueError("import update requires unique key and update targets")
    return ResolvedImportMapping(tuple(columns), keys)


def plan_tabular_update(
    project: Any,
    *,
    sheet_id: int,
    columns: Iterable[ImportRowsColumn],
    key_columns: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
    keep_existing_on_blank: bool,
    max_rows: int | None,
) -> ImportUpdatePlan:
    """Compose the server's temporal preflight with the existing update planner."""

    return plan_import_update(
        project,
        sheet_id=sheet_id,
        columns=columns,
        key_columns=key_columns,
        rows=rows,
        keep_existing_on_blank=keep_existing_on_blank,
        preflight_rows=preflight_typed_rows_for_persistence,
        max_rows=max_rows,
    )


def update_preview_payload(plan: ImportUpdatePlan) -> dict[str, Any]:
    """Project only the public update-preview fields."""

    return {
        "matched": plan.matched,
        "unmatched": plan.unmatched,
        "blank_keys": plan.blank_keys,
        "ambiguous": plan.ambiguous,
        "changed_cells": plan.changed_cells,
        "cleared_cells": plan.cleared_cells,
        "samples": list(plan.samples),
        "confirmation": plan.confirmation,
    }


def materialized_sheet_output(result: ActionResult) -> ActionOutput | None:
    return next(
        (
            output
            for output in result.outputs
            if output.ref.get("kind") == "materialized_sheet"
        ),
        None,
    )


def mutation_row_count(result: ActionResult, *, output_name: str) -> int | None:
    output = next((item for item in result.outputs if item.name == output_name), None)
    if output is None:
        return None
    row_count = output.ref.get("row_count")
    return (
        row_count
        if isinstance(row_count, int) and not isinstance(row_count, bool)
        else None
    )


def workload_limit_result(
    *, project_id: str, action_kind: str, row_count: int, max_rows: int
) -> ActionResult:
    """Build the standard action-contract refusal for an admitted upload limit."""

    return _failed_result(
        project_id=project_id,
        action_kind=action_kind,
        error=ActionError(
            code="import_workload_limit_exceeded",
            message=(
                f"{action_kind} row count exceeds the deployment limit of {max_rows}"
            ),
            action_kind=action_kind,
            field="params",
            details={"row_count": row_count, "max_rows": max_rows},
        ),
    )


def resolve_import_sheet_name(
    project: Any,
    *,
    request_key: str,
    action_kind: str,
    requested_name: str,
) -> str:
    """Reuse a completed create-sheet name or allocate the next available one."""

    stored = ReceiptStore(project).find_by_idempotency_key(request_key)
    if stored is not None and stored.action_kind == action_kind:
        output = materialized_sheet_output(stored.parsed())
        if output is not None and output.name is not None:
            return output.name
    used_names = {
        str(row["name"]) for row in project.db.execute("SELECT name FROM sheets")
    }
    name = requested_name
    suffix = 2
    while name in used_names:
        name = f"{requested_name}-{suffix}"
        suffix += 1
    return name
