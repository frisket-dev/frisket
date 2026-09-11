"""Host-owned execution for typed column and row project mutations."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from frisket.actions.core import _ProjectAction
from frisket.actions.mutations import COLUMN_DISPLAY_FORMATS
from frisket.actions.review_replay import ReviewDecisionParams
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    AcceptedReplayColumn,
    AcceptedReplayValue,
    CellEditor,
    ColumnCreator,
    ColumnPatcher,
    ColumnTyper,
    CreatedColumn,
    CreatedRow,
    AppendedRows,
    DeletedRows,
    DismissedReplayValue,
    EditedCells,
    PatchedColumn,
    QueryCellEditor,
    QueryEditedCells,
    ReviewDecision,
    RetypedColumn,
    RowCreator,
    RowsAppender,
    RowsUpdater,
    UpdatedImportedRows,
    RowDeleter,
    TableError,
)
from frisket.authoring import column_types
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.contracts.actions.schemas._base import (
    MAX_CELL_EDITS,
    canonical_column_type,
)
from frisket.engine.executor.action_inventory import (
    ExecutorContext,
    ExecutorDeps,
    _ActionCoreSpec,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_lifecycle import (
    _child_sheet_deterministic_result_from_existing,
    _run_action_core_spec,
)
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.action_receipts import _receipt_ops_are_applied
from frisket.engine.executor.action_families.imports import _compact_import_rowset_ref
from frisket.engine.executor.import_sources import open_local_file_reader
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.executor.project_mutation_runtime import (
    CallOnce as _CallOnce,
    Refusal as _Refusal,
    claimed_column as _claimed_column,
    op_spec as _op_spec,
    refuse as _refuse,
    text_hash as _text_hash,
    write_edit_overlay as _write_edit_overlay,
    write_op as _write_op,
)
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
)
from frisket.engine.store.current_cells import (
    decoded_cell_validity,
    refresh_current_cells,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.artifact_timeline import TimelineError
from frisket.features.temporal_ingress import (
    is_temporal_column_type,
    preflight_typed_rows_for_persistence,
    validate_temporal_persistence_value,
)
from frisket.features.watchlists.specs import (
    canonical_json,
    normalize_query_spec,
    query_spec_hash,
)
from frisket.querysets import (
    SHEET_FILTER_ROWSET_EVALUATOR,
    SheetRowSetError,
    resolve_sheet_filter_rows,
)


_TEMPORAL_ERRORS = {
    "invalid_temporal_value": "The temporal value is malformed.",
    "timeline_not_found": "The temporal value refers to a missing timeline.",
    "timeline_stale": "The temporal value timeline anchor is stale.",
    "timeline_duration_required": "The temporal timeline duration is not finalized.",
    "ambiguous_time_mapping": "The temporal value timeline mapping is ambiguous.",
    "range_out_of_bounds": "The temporal value exceeds its timeline duration.",
}

logger = logging.getLogger("frisket.executor")


def _refresh_pending_review_summary(project: Any, project_id: str) -> None:
    try:
        project.refresh_pending_review_summary()
    except Exception:
        logger.warning(
            "review_pending_summary_refresh_failed",
            exc_info=True,
            extra={
                "event": "review_pending_summary_refresh_failed",
                "action_kind": "review.decision",
                "project_id": project_id,
            },
        )


class _ColumnPatcher(_CallOnce):
    def patch(
        self,
        column_id: int,
        *,
        sheet_id: int | None,
        format: str | None,
    ) -> PatchedColumn:
        self._begin()
        if format is not None and format not in COLUMN_DISPLAY_FORMATS:
            _refuse(
                "invalid_column_format",
                "column.patch format is not supported",
                action_kind=self._action.kind,
                field="params.format",
                details={"format": format},
            )
        row = self._cur.execute(
            "SELECT c.id AS column_id, c.sheet_id AS sheet_id, c.name AS column_name, "
            "c.type AS type, c.format AS format_before, c.hidden AS column_hidden, "
            "s.name AS sheet_name, s.hidden AS sheet_hidden "
            "FROM columns c JOIN sheets s ON s.id=c.sheet_id WHERE c.id=?",
            (column_id,),
        ).fetchone()
        if row is None:
            _refuse(
                "invalid_column_ref",
                "column.patch target column was not found",
                action_kind=self._action.kind,
                field="params.column_id",
                details={"column_id": column_id},
            )
        if sheet_id is not None and int(row["sheet_id"]) != sheet_id:
            _refuse(
                "invalid_column_ref",
                "column.patch target sheet_id does not match column",
                action_kind=self._action.kind,
                field="params.sheet_id",
                details={
                    "expected_sheet_id": sheet_id,
                    "actual_sheet_id": int(row["sheet_id"]),
                },
            )
        if int(row["column_hidden"]) or int(row["sheet_hidden"]):
            _refuse(
                "invalid_column_ref",
                "column.patch target must identify a visible column",
                action_kind=self._action.kind,
                field="params.column_id",
                details={"column_id": column_id},
            )
        _claimed_column(self._project, column_id, action_kind=self._action.kind)
        self._cur.execute("UPDATE columns SET format=? WHERE id=?", (format, column_id))
        op_id = _write_op(
            self._cur,
            kind="column.patch",
            label=f"column.patch {row['column_name']} format {format or 'default'}",
            spec=_op_spec(self._action, self._params_hash),
            undo_info={
                "column_formats": {str(column_id): row["format_before"]},
                "column_formats_after": {str(column_id): format},
            },
        )
        result = PatchedColumn(
            sheet_id=int(row["sheet_id"]),
            sheet_name=str(row["sheet_name"]),
            column_id=column_id,
            column_name=str(row["column_name"]),
            column_type=str(row["type"]),
            format_before=row["format_before"],
            format_after=format,
            op_id=op_id,
        )
        self.result = result
        return result


class _ColumnCreator(_CallOnce):
    def create(
        self,
        sheet_id: int,
        *,
        name: str,
        column_type: str,
        position: int | None,
    ) -> CreatedColumn:
        self._begin()
        if not name:
            _refuse(
                "invalid_column_name",
                "column.add name must not be blank",
                action_kind=self._action.kind,
                field="params.name",
            )
        type_spec = column_types.get_column_type(column_type)
        if type_spec is None:
            _refuse(
                "invalid_column_type",
                "column.add type is not registered",
                action_kind=self._action.kind,
                field="params.type",
                details={"type": column_type},
            )
        from frisket.authoring.workbench.plugin_runtime_capabilities import (
            project_allows_plugin_column_type,
        )

        if not project_allows_plugin_column_type(self._project, type_spec):
            _refuse(
                "invalid_column_type",
                "column.add plugin type is not enabled for this project",
                action_kind=self._action.kind,
                field="params.type",
                details={"type": column_type, "plugin": type_spec.plugin},
            )
        sheet = self._cur.execute(
            "SELECT id, name FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        if sheet is None:
            _refuse(
                "sheet_not_found",
                "column.add target sheet was not found",
                action_kind=self._action.kind,
                field="params.sheet_id",
                details={"sheet_id": sheet_id},
            )
        if (
            self._cur.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
                (sheet_id, name),
            ).fetchone()
            is not None
        ):
            _refuse(
                "column_exists",
                "column.add name already exists on this sheet",
                action_kind=self._action.kind,
                field="params.name",
                details={"name": name},
            )
        ordered = self._cur.execute(
            "SELECT id, position FROM columns WHERE sheet_id=? AND hidden=0 "
            "ORDER BY position, id",
            (sheet_id,),
        ).fetchall()
        if position is None or position > len(ordered):
            base = self._cur.execute(
                "SELECT COALESCE(MAX(position),0) FROM columns WHERE sheet_id=?",
                (sheet_id,),
            ).fetchone()[0]
            new_position = int(base) + 1
        else:
            new_position = int(ordered[position - 1]["position"])
            self._cur.execute(
                "UPDATE columns SET position=position+1 WHERE sheet_id=? AND position>=?",
                (sheet_id, new_position),
            )
        self._cur.execute(
            "INSERT INTO columns (sheet_id,name,type,position,ai_generated,hidden,format) "
            "VALUES (?,?,?,?,0,0,NULL)",
            (sheet_id, name, column_type, new_position),
        )
        column_id = int(self._cur.lastrowid)
        op_id = _write_op(
            self._cur,
            kind="add_column",
            label=f"add column {name}",
            spec=_op_spec(self._action, self._params_hash),
            undo_info={"created_columns": [column_id]},
        )
        result = CreatedColumn(
            sheet_id=sheet_id,
            sheet_name=str(sheet["name"]),
            column_id=column_id,
            name=name,
            column_type=column_type,
            position=new_position,
            op_id=op_id,
        )
        self.result = result
        return result


class _RowCreator(_CallOnce):
    def create(self, sheet_id: int, *, cells: Mapping[str, Any]) -> CreatedRow:
        self._begin()
        sheet = self._cur.execute(
            "SELECT id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        if sheet is None:
            _refuse(
                "sheet_not_found",
                "row.add target sheet was not found",
                action_kind=self._action.kind,
                field="params.sheet_id",
                details={"sheet_id": sheet_id},
            )
        columns = self._cur.execute(
            "SELECT id,name,type FROM columns WHERE sheet_id=? AND hidden=0",
            (sheet_id,),
        ).fetchall()
        column_ids = {str(row["name"]): int(row["id"]) for row in columns}
        column_types_by_name = {str(row["name"]): str(row["type"]) for row in columns}
        unknown = sorted(set(cells) - set(column_ids))
        if unknown:
            _refuse(
                "invalid_row_ref",
                "row.add cells contain unknown or hidden column names",
                action_kind=self._action.kind,
                field=f"params.cells.{unknown[0]}",
                details={"unknown_columns": unknown},
            )
        raw_record = {name: value for name, value in cells.items() if value is not None}
        try:
            record = preflight_typed_rows_for_persistence(
                self._project,
                column_types_by_name=column_types_by_name,
                rows=[raw_record],
            )[0]
        except TimelineError as exc:
            code = (
                exc.code if exc.code in _TEMPORAL_ERRORS else "invalid_temporal_value"
            )
            _refuse(
                code,
                _TEMPORAL_ERRORS[code],
                action_kind=self._action.kind,
                field="params.cells",
                details={"reason": exc.message},
            )
        base = self._cur.execute(
            "SELECT COALESCE(MAX(position),0) FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
        self._cur.execute(
            "INSERT INTO rows (sheet_id,position,parent_row_id) VALUES (?,?,NULL)",
            (sheet_id, base + 1),
        )
        row_id = int(self._cur.lastrowid)
        op_id = _write_op(
            self._cur,
            kind="add_row",
            label="add row",
            spec=_op_spec(self._action, self._params_hash),
            undo_info={"created_rows": [row_id]},
        )
        producer_id = create_base_cell_producer(
            self._project.db, stage_id=f"op:{op_id}", op_id=op_id
        )
        initialize_base_cells(
            self._project.db,
            producer_id=producer_id,
            cells=[
                BaseCellWrite(
                    row_id=row_id,
                    column_id=column_ids[name],
                    value=value,
                )
                for name, value in record.items()
            ],
        )
        total = int(
            self._cur.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=? AND hidden=0", (sheet_id,)
            ).fetchone()[0]
        )
        result = CreatedRow(sheet_id, row_id, total, op_id, record, column_ids)
        self.result = result
        return result


class _RowsAppender(_CallOnce):
    def __init__(
        self,
        project: Any,
        cur: Any,
        action: _TypedProjectEnvelope,
        params_hash: str,
        *,
        max_rows: int | None = None,
    ) -> None:
        super().__init__(project, cur, action, params_hash)
        self._max_rows = max_rows

    def append(
        self,
        *,
        columns: tuple[Any, ...],
        rows: tuple[Mapping[str, Any], ...],
        source: Mapping[str, Any],
    ) -> AppendedRows:
        return self._append_rows(columns=columns, rows=rows, source=source)

    def append_csv(self, params: Any) -> AppendedRows:
        from frisket.actions.imports import import_csv

        with open_local_file_reader(self._file_sources) as reader:
            produced = import_csv(params, reader)
            return self._append_rows(
                columns=tuple(params.columns),
                rows=(row.output.root for row in produced.rows),
                source=dict(produced.source or {}),
            )

    def append_xlsx(self, params: Any) -> AppendedRows:
        from frisket.actions.import_xlsx import import_xlsx

        with open_local_file_reader(self._file_sources) as reader:
            produced = import_xlsx(params, reader)
            return self._append_rows(
                columns=tuple(params.columns),
                rows=(row.output.root for row in produced.rows),
                source=dict(produced.source or {}),
            )

    def _append_rows(
        self,
        *,
        columns: tuple[Any, ...],
        rows: Any,
        source: Mapping[str, Any],
    ) -> AppendedRows:
        self._begin()
        scope = self._action.row_scope
        sheet_id = int(scope.sheet_id)
        sheet = self._cur.execute(
            "SELECT id,parent_sheet_id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
        ).fetchone()
        if sheet is None:
            _refuse(
                "sheet_not_found",
                "import.append_rows destination sheet was not found",
                action_kind=self._action.kind,
                field="scope.sheet_id",
            )
        if sheet["parent_sheet_id"] is not None:
            _refuse(
                "derived_append_unsupported",
                "Derived sheets cannot receive imported rows because refresh replaces their contents.",
                action_kind=self._action.kind,
                field="scope.sheet_id",
            )
        existing_rows = self._cur.execute(
            "SELECT id,name,type FROM columns WHERE sheet_id=? AND hidden=0",
            (sheet_id,),
        ).fetchall()
        existing = {str(row["name"]): row for row in existing_rows}
        column_ids: dict[str, int] = {}
        column_types: dict[str, str] = {}
        for index, column in enumerate(columns):
            found = existing.get(column.name)
            if found is None or canonical_column_type(
                found["type"]
            ) != canonical_column_type(column.type):
                _refuse(
                    "incompatible_append_mapping",
                    "Every imported column must map to an existing column with the same type.",
                    action_kind=self._action.kind,
                    field=f"params.columns[{index}]",
                    details={"column": column.name},
                )
            column_ids[column.name] = int(found["id"])
            column_types[column.name] = str(found["type"])
        base = int(
            self._cur.execute(
                "SELECT COALESCE(MAX(position),0) FROM rows WHERE sheet_id=?",
                (sheet_id,),
            ).fetchone()[0]
        )
        row_ids: list[int] = []
        operation_spec = {
            "action_id": self._action.kind,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {
                **{
                    key: value
                    for key, value in self._action.params.items()
                    if key != "rows"
                },
                "row_count": 0,
                "params_hash": self._params_hash,
            },
            "idempotency_key": self._action.idempotency_key,
        }
        op_id = _write_op(
            self._cur,
            kind=self._action.kind,
            label="append imported rows",
            spec=operation_spec,
            undo_info={"created_rows": row_ids},
        )
        producer_id = create_base_cell_producer(
            self._project.db, stage_id=f"op:{op_id}", op_id=op_id
        )
        iterator = iter(rows)
        offset = 0
        while True:
            try:
                raw_record = next(iterator)
            except StopIteration:
                break
            except TableError as exc:
                _refuse(
                    exc.code,
                    str(exc),
                    action_kind=self._action.kind,
                    field="params",
                    details=exc.details,
                )
            offset += 1
            # This bounds one request's work, not total destination storage.
            if self._max_rows is not None and offset > self._max_rows:
                _refuse(
                    "import_workload_limit_exceeded",
                    f"{self._action.kind} row count exceeds the deployment limit of {self._max_rows}",
                    action_kind=self._action.kind,
                    field="params",
                    details={"row_count": offset, "max_rows": self._max_rows},
                )
            try:
                record = preflight_typed_rows_for_persistence(
                    self._project, column_types_by_name=column_types, rows=[raw_record]
                )[0]
            except TimelineError as exc:
                code = (
                    exc.code
                    if exc.code in _TEMPORAL_ERRORS
                    else "invalid_temporal_value"
                )
                _refuse(
                    code,
                    _TEMPORAL_ERRORS[code],
                    action_kind=self._action.kind,
                    field="params.rows",
                    details={"reason": exc.message},
                )
            self._cur.execute(
                "INSERT INTO rows (sheet_id,position,parent_row_id) VALUES (?,?,NULL)",
                (sheet_id, base + offset),
            )
            row_id = int(self._cur.lastrowid)
            row_ids.append(row_id)
            initialize_base_cells(
                self._project.db,
                producer_id=producer_id,
                cells=[
                    BaseCellWrite(
                        row_id=row_id,
                        column_id=column_ids[name],
                        value=value,
                    )
                    for name, value in record.items()
                    if value is not None
                ],
            )
        operation_spec["params"]["row_count"] = len(row_ids)
        self._cur.execute(
            "UPDATE ops SET label=?, spec=?, undo_info=? WHERE id=?",
            (
                f"append {len(row_ids)} imported rows",
                json.dumps(operation_spec, sort_keys=True),
                json.dumps({"created_rows": row_ids}, sort_keys=True),
                op_id,
            ),
        )
        total = int(
            self._cur.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=? AND hidden=0", (sheet_id,)
            ).fetchone()[0]
        )
        result = AppendedRows(
            sheet_id, tuple(row_ids), total, op_id, column_ids, source
        )
        self.result = result
        return result


class _RowsUpdater(_CallOnce):
    def __init__(self, *args: Any, max_rows: int | None = None):
        super().__init__(*args)
        self._max_rows = max_rows

    def update(
        self,
        *,
        columns: tuple[Any, ...],
        key_columns: tuple[str, ...],
        rows: tuple[Mapping[str, Any], ...],
        source: Mapping[str, Any],
        keep_existing_on_blank: bool,
    ) -> UpdatedImportedRows:
        self._begin()
        return self._update_rows(
            columns, key_columns, rows, source, keep_existing_on_blank
        )

    def update_csv(self, params: Any) -> UpdatedImportedRows:
        from frisket.actions.imports import import_csv

        self._begin()

        with open_local_file_reader(self._file_sources) as reader:
            produced = import_csv(params, reader)
            return self._update_rows(
                tuple(params.columns),
                tuple(params.key_columns),
                (row.output.root for row in produced.rows),
                dict(produced.source or {}),
                bool(params.keep_existing_on_blank),
            )

    def update_xlsx(self, params: Any) -> UpdatedImportedRows:
        from frisket.actions.import_xlsx import import_xlsx

        self._begin()

        with open_local_file_reader(self._file_sources) as reader:
            produced = import_xlsx(params, reader)
            return self._update_rows(
                tuple(params.columns),
                tuple(params.key_columns),
                (row.output.root for row in produced.rows),
                dict(produced.source or {}),
                bool(params.keep_existing_on_blank),
            )

    def _update_rows(
        self,
        columns: tuple[Any, ...],
        key_columns: tuple[str, ...],
        rows: Any,
        source: Mapping[str, Any],
        keep_existing_on_blank: bool,
    ) -> UpdatedImportedRows:
        from frisket.engine.executor.import_update import plan_import_update

        try:
            plan = plan_import_update(
                self._project,
                sheet_id=int(self._action.row_scope.sheet_id),
                columns=columns,
                key_columns=key_columns,
                rows=rows,
                keep_existing_on_blank=keep_existing_on_blank,
                preflight_rows=preflight_typed_rows_for_persistence,
                max_rows=self._max_rows,
            )
        except TableError as exc:
            _refuse(
                exc.code,
                str(exc),
                action_kind=self._action.kind,
                field="params",
                details=exc.details,
            )
        except OverflowError as exc:
            _refuse(
                "import_workload_limit_exceeded",
                "import.update_rows exceeds the deployment row limit",
                action_kind=self._action.kind,
                field="params.rows",
                details={"row_count": int(exc.args[0]), "max_rows": self._max_rows},
            )
        except TimelineError as exc:
            code = (
                exc.code if exc.code in _TEMPORAL_ERRORS else "invalid_temporal_value"
            )
            _refuse(
                code,
                _TEMPORAL_ERRORS[code],
                action_kind=self._action.kind,
                field="params.rows",
                details={"reason": exc.message},
            )
        except LookupError as exc:
            code = str(exc)
            _refuse(
                code,
                code.replace("_", " "),
                action_kind=self._action.kind,
                field="scope.sheet_id",
            )
        if plan.ambiguous:
            _refuse(
                "ambiguous_import_keys",
                "Duplicate exact keys make the imported update ambiguous",
                action_kind=self._action.kind,
                field="params.rows",
                details={"ambiguous": plan.ambiguous},
            )
        if self._action.confirmation != plan.confirmation:
            _refuse(
                "stale_import_preview",
                "Preview these updates again before applying them",
                action_kind=self._action.kind,
                field="confirmation",
                details={
                    "confirmation": plan.confirmation,
                    "preview": plan.__dict__ | {"targets": []},
                },
            )
        for column_id in {int(target["column_id"]) for target in plan.targets}:
            _claimed_column(
                self._project,
                column_id,
                action_kind=self._action.kind,
                field="params.columns",
            )
        op_id = _write_edit_overlay(
            self._project,
            self._cur,
            label=f"update {plan.matched} imported rows",
            spec={
                "action_id": self._action.kind,
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": int(self._action.row_scope.sheet_id),
                },
                "params": {
                    **{
                        key: value
                        for key, value in self._action.params.items()
                        if key != "rows"
                    },
                    "row_count": plan.matched + plan.unmatched + plan.ambiguous,
                    "params_hash": self._params_hash,
                },
                "idempotency_key": self._action.idempotency_key,
            },
            targets=list(plan.targets),
            stale_reason="imported_cell_update",
        )
        result = UpdatedImportedRows(
            int(self._action.row_scope.sheet_id),
            tuple(sorted({int(target["row_id"]) for target in plan.targets})),
            plan.changed_cells,
            op_id,
            {
                str(target["column_name"]): int(target["column_id"])
                for target in plan.targets
            },
            source,
        )
        self._plan = plan
        self.result = result
        return result


class _RowDeleter(_CallOnce):
    def delete(self, sheet_id: int, *, row_ids: tuple[int, ...]) -> DeletedRows:
        self._begin()
        if not row_ids:
            _refuse(
                "invalid_params",
                "row.delete row_ids must not be empty",
                action_kind=self._action.kind,
                field="params.row_ids",
            )
        if (
            self._cur.execute(
                "SELECT id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
            ).fetchone()
            is None
        ):
            _refuse(
                "sheet_not_found",
                "row.delete target sheet was not found",
                action_kind=self._action.kind,
                field="params.sheet_id",
                details={"sheet_id": sheet_id},
            )
        visible: set[int] = set()
        for start in range(0, len(row_ids), 900):
            chunk = row_ids[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            visible.update(
                int(row["id"])
                for row in self._cur.execute(
                    f"SELECT id FROM rows WHERE sheet_id=? AND hidden=0 "
                    f"AND id IN ({placeholders})",
                    (sheet_id, *chunk),
                ).fetchall()
            )
        missing = [row_id for row_id in row_ids if row_id not in visible]
        if missing:
            _refuse(
                "invalid_row_ref",
                "row.delete row_ids must reference visible rows on the target sheet",
                action_kind=self._action.kind,
                field="params.row_ids",
                details={"invalid_rows": missing},
            )
        self._cur.executemany(
            "UPDATE rows SET hidden=1 WHERE id=? AND sheet_id=?",
            [(row_id, sheet_id) for row_id in row_ids],
        )
        op_id = _write_op(
            self._cur,
            kind="delete_rows",
            label="delete row" if len(row_ids) == 1 else "delete rows",
            spec=_op_spec(self._action, self._params_hash),
            undo_info={"deleted_rows": list(row_ids)},
        )
        total = int(
            self._cur.execute(
                "SELECT COUNT(*) FROM rows WHERE sheet_id=? AND hidden=0", (sheet_id,)
            ).fetchone()[0]
        )
        result = DeletedRows(sheet_id, row_ids, total, op_id)
        self.result = result
        return result


def _validate_temporal(
    project: Any,
    *,
    type_name: str,
    value: Any,
    action_kind: str,
    field: str,
    column_id: int,
    row_id: int | None = None,
) -> None:
    if not is_temporal_column_type(type_name) or value is None:
        return
    try:
        validate_temporal_persistence_value(project, type_name=type_name, value=value)
    except TimelineError as exc:
        code = exc.code if exc.code in _TEMPORAL_ERRORS else "invalid_temporal_value"
        details = {"type": type_name, "column_id": column_id, "reason": exc.message}
        if row_id is not None:
            details["row_id"] = row_id
        _refuse(
            code,
            _TEMPORAL_ERRORS[code],
            action_kind=action_kind,
            field=field,
            details=details,
        )


def _parse_value(
    project: Any,
    *,
    type_name: str,
    value: Any,
    action_kind: str,
    field: str,
    column_id: int,
    row_id: int | None = None,
) -> Any:
    try:
        parsed = column_types.parse_value(type_name, value)
    except Exception as exc:
        details = {"type": type_name, "column_id": column_id, "reason": str(exc)}
        if row_id is not None:
            details["row_id"] = row_id
        _refuse(
            "column_value_validation_failed",
            f"{action_kind} value could not be parsed for column type",
            action_kind=action_kind,
            field=field,
            details=details,
        )
    if not column_types.validate_value(type_name, parsed):
        details = {"type": type_name, "column_id": column_id}
        if row_id is not None:
            details["row_id"] = row_id
        _refuse(
            "column_value_validation_failed",
            f"{action_kind} value fails validation for column type",
            action_kind=action_kind,
            field=field,
            details=details,
        )
    _validate_temporal(
        project,
        type_name=type_name,
        value=parsed,
        action_kind=action_kind,
        field=field,
        column_id=column_id,
        row_id=row_id,
    )
    return parsed


class _ColumnTyper(_CallOnce):
    def set_type(
        self, column_id: int, *, sheet_id: int | None, column_type: str
    ) -> RetypedColumn:
        self._begin()
        type_spec = column_types.get_column_type(column_type)
        if type_spec is None:
            _refuse(
                "invalid_column_type",
                "column.set_type type is not registered",
                action_kind=self._action.kind,
                field="params.type",
                details={"type": column_type},
            )
        from frisket.authoring.workbench.plugin_runtime_capabilities import (
            project_allows_plugin_column_type,
        )

        if not project_allows_plugin_column_type(self._project, type_spec):
            _refuse(
                "invalid_column_type",
                "column.set_type plugin type is not enabled for this project",
                action_kind=self._action.kind,
                field="params.type",
                details={"type": column_type, "plugin": type_spec.plugin},
            )
        column = self._cur.execute(
            "SELECT c.id AS column_id,c.sheet_id,c.name AS column_name,"
            "c.type AS type_before,c.hidden AS column_hidden,s.name AS sheet_name,"
            "s.hidden AS sheet_hidden FROM columns c JOIN sheets s ON s.id=c.sheet_id "
            "WHERE c.id=?",
            (column_id,),
        ).fetchone()
        if (
            column is None
            or int(column["column_hidden"])
            or int(column["sheet_hidden"])
        ):
            _refuse(
                "invalid_column_ref",
                "column.set_type target must identify a visible column",
                action_kind=self._action.kind,
                field="params.column_id",
                details={"column_id": column_id},
            )
        if sheet_id is not None and int(column["sheet_id"]) != sheet_id:
            _refuse(
                "invalid_column_ref",
                "column.set_type target sheet_id does not match column",
                action_kind=self._action.kind,
                field="params.sheet_id",
                details={
                    "expected_sheet_id": sheet_id,
                    "actual_sheet_id": int(column["sheet_id"]),
                },
            )
        _claimed_column(self._project, column_id, action_kind=self._action.kind)
        values = self._project.get_values(
            int(column["sheet_id"]), column_id, preserve_invalid=True
        )
        # Temporal values additionally carry project-bound artifact anchors.
        # Broken references remain a referential-integrity error rather than
        # an ordinary type mismatch that can be represented as an invalid cell.
        for row_id, value in values.items():
            _validate_temporal(
                self._project,
                type_name=column_type,
                value=value,
                action_kind=self._action.kind,
                field="params.type",
                column_id=column_id,
                row_id=row_id,
            )
        type_before = str(column["type_before"])
        self._cur.execute(
            "UPDATE columns SET type=? WHERE id=?", (column_type, column_id)
        )
        op_id = _write_op(
            self._cur,
            kind="column.set_type",
            label=f"column.set_type {column['column_name']} as {column_type}",
            spec=_op_spec(self._action, self._params_hash),
            undo_info={
                "column_types": {str(column_id): type_before},
                "column_types_after": {str(column_id): column_type},
            },
        )
        # Refresh after the descriptor op exists: its id is the precedence
        # boundary that retires older edits while retaining source values.
        refresh_current_cells(self._project.db, column_ids=[column_id])
        result = RetypedColumn(
            int(column["sheet_id"]),
            str(column["sheet_name"]),
            column_id,
            str(column["column_name"]),
            type_before,
            column_type,
            op_id,
        )
        self._classified_row_ids = tuple(sorted(values))
        self._invalid_row_ids = tuple(
            sorted(
                row_id
                for row_id, value in values.items()
                if decoded_cell_validity(column_type, value) == "invalid"
            )
        )
        self.result = result
        return result


class _CellEditor(_CallOnce):
    def edit(self, edits: tuple[tuple[int, int, Any], ...]) -> EditedCells:
        self._begin()
        targets: list[dict[str, Any]] = []
        for index, (row_id, column_id, value) in enumerate(edits):
            row = self._cur.execute(
                "SELECT r.id AS row_id,r.sheet_id,c.id AS column_id,c.name AS column_name,c.type AS column_type "
                "FROM rows r JOIN sheets s ON s.id=r.sheet_id AND s.hidden=0 "
                "JOIN columns c ON c.id=? AND c.sheet_id=r.sheet_id AND c.hidden=0 "
                "WHERE r.id=? AND r.hidden=0",
                (column_id, row_id),
            ).fetchone()
            if row is None:
                _refuse(
                    "cell_target_not_found",
                    "cell.edit target cell was not found on a visible row",
                    action_kind=self._action.kind,
                    field=f"params.edits[{index}]",
                    details={"row_id": row_id, "column_id": column_id},
                )
            parsed = _parse_value(
                self._project,
                type_name=str(row["column_type"]),
                value=value,
                action_kind=self._action.kind,
                field=f"params.edits[{index}].value",
                row_id=row_id,
                column_id=column_id,
            )
            current, refs = self._project.get_values_with_refs(
                int(row["sheet_id"]), column_id, row_ids=[row_id]
            )
            targets.append(
                {
                    "sheet_id": int(row["sheet_id"]),
                    "row_id": row_id,
                    "column_id": column_id,
                    "column_name": str(row["column_name"]),
                    "value_before": current.get(row_id),
                    "current_value_ref": refs.get(row_id),
                    "value_after": parsed,
                }
            )
        for column_id in {int(target["column_id"]) for target in targets}:
            _claimed_column(
                self._project,
                column_id,
                action_kind=self._action.kind,
                field="params.edits",
            )
        op_id = _write_edit_overlay(
            self._project,
            self._cur,
            label="manual edit"
            if len(targets) == 1
            else f"manual edit x{len(targets)}",
            spec=_op_spec(self._action, self._params_hash),
            targets=targets,
        )
        edit_refs = tuple(
            {
                "kind": "manual_edit_overlay",
                "op_id": op_id,
                "sheet_id": target["sheet_id"],
                "row_id": target["row_id"],
                "column_id": target["column_id"],
                "column_name": target["column_name"],
                "value_before_hash": _text_hash(
                    json.dumps(target["value_before"], sort_keys=True)
                ),
                "value_after_hash": _text_hash(
                    json.dumps(target["value_after"], sort_keys=True)
                ),
            }
            for target in targets
        )
        result = EditedCells(
            op_id,
            tuple(
                (int(target["row_id"]), int(target["column_id"])) for target in targets
            ),
        )
        self._edit_refs = edit_refs
        self.result = result
        return result


class _QueryCellEditor(_CallOnce):
    def __init__(self, *args: Any, max_rows: int | None = None):
        super().__init__(*args)
        self._max_rows = max_rows

    def edit_query(
        self, *, query: Mapping[str, Any], column_id: int, value: Any
    ) -> QueryEditedCells:
        self._begin()
        try:
            normalized = normalize_query_spec(dict(query))
        except ValueError as exc:
            _refuse(
                "invalid_query_spec",
                "The query spec is malformed or unsupported.",
                action_kind=self._action.kind,
                field="params.query",
                details={"reason": str(exc)},
            )
        if normalized["kind"] != "sheet.filter":
            _refuse(
                "unsupported_query_kind",
                "cell.edit_query supports sheet.filter queries in this slice",
                action_kind=self._action.kind,
                field="params.query.kind",
            )
        scope = normalized.get("scope")
        sheet_id = scope.get("sheet_id") if isinstance(scope, dict) else None
        if not isinstance(sheet_id, int) or isinstance(sheet_id, bool) or sheet_id < 1:
            _refuse(
                "invalid_query_spec",
                "sheet.filter query scope requires a positive sheet_id",
                action_kind=self._action.kind,
                field="params.query.scope.sheet_id",
            )
        if (
            self._cur.execute(
                "SELECT id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
            ).fetchone()
            is None
        ):
            _refuse(
                "invalid_query_spec",
                "sheet.filter query sheet_id does not identify a visible sheet",
                action_kind=self._action.kind,
                field="params.query.scope.sheet_id",
            )
        column = self._cur.execute(
            "SELECT c.id AS column_id,c.name AS column_name,c.type AS column_type FROM columns c JOIN sheets s ON s.id=c.sheet_id WHERE c.id=? AND c.sheet_id=? AND c.hidden=0 AND s.hidden=0",
            (column_id, sheet_id),
        ).fetchone()
        if column is None:
            _refuse(
                "invalid_column_ref",
                "cell.edit_query column_id must be visible on the query sheet",
                action_kind=self._action.kind,
                field="params.column_id",
                details={"sheet_id": sheet_id, "column_id": column_id},
            )
        try:
            rowset = resolve_sheet_filter_rows(
                self._project,
                sheet_id,
                filter_=canonical_json(normalized.get("filter", {})),
                sort=canonical_json(normalized["sort"])
                if "sort" in normalized
                else None,
                limit=self._max_rows
                if self._max_rows is not None
                else self._project.row_count(sheet_id),
                offset=0,
            )
        except SheetRowSetError as exc:
            _refuse(
                "invalid_query_filter",
                "The query filter or sort could not be evaluated.",
                action_kind=self._action.kind,
                field="params.query.filter",
                details={"reason": str(exc)},
            )
        if self._max_rows is not None and rowset.total > self._max_rows:
            _refuse(
                "query_rowset_too_large",
                f"cell.edit_query exceeds the deployment row limit of {self._max_rows}",
                action_kind=self._action.kind,
                field="params.query",
                details={"row_count": rowset.total, "max_rows": self._max_rows},
            )
        if rowset.total == 0:
            _refuse(
                "query_rowset_empty",
                "cell.edit_query resolved zero rows to edit",
                action_kind=self._action.kind,
                field="params.query",
            )
        parsed = _parse_value(
            self._project,
            type_name=str(column["column_type"]),
            value=value,
            action_kind=self._action.kind,
            field="params.value",
            column_id=column_id,
        )
        current, current_refs = self._project.get_values_with_refs(
            sheet_id, column_id, row_ids=rowset.row_ids
        )
        targets = [
            {
                "sheet_id": sheet_id,
                "row_id": row_id,
                "column_id": column_id,
                "column_name": str(column["column_name"]),
                "value_before": current.get(row_id),
                "current_value_ref": current_refs.get(row_id),
                "value_after": parsed,
            }
            for row_id in rowset.row_ids
        ]
        _claimed_column(self._project, column_id, action_kind=self._action.kind)
        row_ids = list(rowset.row_ids)
        spec = _op_spec(self._action, self._params_hash)
        spec["params"]["query"] = normalized
        spec["resolved"] = {
            "query_hash": query_spec_hash(normalized),
            "row_count": len(row_ids),
            "rowset_hash": _text_hash(json.dumps(row_ids, separators=(",", ":"))),
            "evaluator": dict(SHEET_FILTER_ROWSET_EVALUATOR),
        }
        if len(row_ids) <= MAX_CELL_EDITS:
            spec["resolved"]["row_ids"] = row_ids
        op_id = _write_edit_overlay(
            self._project,
            self._cur,
            label="query edit" if len(targets) == 1 else f"query edit x{len(targets)}",
            spec=spec,
            targets=targets,
        )
        rowset_hash = _text_hash(json.dumps(row_ids, separators=(",", ":")))
        if len(targets) > MAX_CELL_EDITS:
            refs = (
                {
                    "kind": "manual_edit_overlay",
                    "op_id": op_id,
                    "sheet_id": sheet_id,
                    "column_id": column_id,
                    "column_name": str(column["column_name"]),
                    "edit_count": len(targets),
                    "rowset_hash": rowset_hash,
                    "value_after_hash": _text_hash(json.dumps(parsed, sort_keys=True)),
                },
            )
        else:
            refs = tuple(
                {
                    "kind": "manual_edit_overlay",
                    "op_id": op_id,
                    "sheet_id": sheet_id,
                    "row_id": target["row_id"],
                    "column_id": column_id,
                    "column_name": str(column["column_name"]),
                    "value_before_hash": _text_hash(
                        json.dumps(target["value_before"], sort_keys=True)
                    ),
                    "value_after_hash": _text_hash(json.dumps(parsed, sort_keys=True)),
                }
                for target in targets
            )
        result = QueryEditedCells(
            sheet_id,
            column_id,
            str(column["column_name"]),
            query_spec_hash(normalized),
            tuple(row_ids),
            op_id,
        )
        self._query = normalized
        self._row_count = len(row_ids)
        self._total = rowset.total
        self._offset = rowset.offset
        self._limit = rowset.limit
        self._edit_refs = refs
        self.result = result
        return result


_CAPABILITY_IMPL = {
    ColumnPatcher: _ColumnPatcher,
    ColumnCreator: _ColumnCreator,
    RowCreator: _RowCreator,
    RowsAppender: _RowsAppender,
    RowsUpdater: _RowsUpdater,
    RowDeleter: _RowDeleter,
    ColumnTyper: _ColumnTyper,
    CellEditor: _CellEditor,
    QueryCellEditor: _QueryCellEditor,
}


def supports_typed_mutation_action(terminal: object) -> bool:
    from frisket.engine.executor.review_replay_action import CAPABILITY_IMPL

    return isinstance(terminal, _ProjectAction) and any(
        terminal.capabilities == (cap,)
        for cap in {
            **_CAPABILITY_IMPL,
            **CAPABILITY_IMPL,
        }
    )


def _result_and_receipt(
    returned: (
        PatchedColumn
        | CreatedColumn
        | CreatedRow
        | AppendedRows
        | UpdatedImportedRows
        | DeletedRows
        | RetypedColumn
        | EditedCells
        | QueryEditedCells
    ),
    *,
    action: _TypedProjectEnvelope,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    capability: _CallOnce,
) -> tuple[ActionResult, Receipt]:
    provider_use: list[dict[str, Any]] = []
    if isinstance(returned, PatchedColumn):
        ref = {
            "kind": "column_patch",
            "sheet_id": returned.sheet_id,
            "sheet_name": returned.sheet_name,
            "column_id": returned.column_id,
            "column_name": returned.column_name,
            "type": returned.column_type,
            "changed_fields": ["format"],
            "format_before": returned.format_before,
            "format_after": returned.format_after,
            "op_id": returned.op_id,
        }
        output = ActionOutput(
            kind="column",
            name=returned.column_name,
            sheet_id=returned.sheet_id,
            column_id=returned.column_id,
            ref=ref,
        )
        inputs = [
            ReceiptIO(
                name="target_column",
                ref={
                    "kind": "target_column",
                    "sheet_id": returned.sheet_id,
                    "sheet_name": returned.sheet_name,
                    "column_id": returned.column_id,
                    "column_name": returned.column_name,
                    "type": returned.column_type,
                    "format_before": returned.format_before,
                },
            )
        ]
        outputs = [ReceiptIO(name=returned.column_name, ref=ref)]
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "column_format_transition",
                    "sheet_id": returned.sheet_id,
                    "column_id": returned.column_id,
                    "format_before": returned.format_before,
                    "format_after": returned.format_after,
                    "op_id": returned.op_id,
                },
                retention="pinned",
            )
        ]
    elif isinstance(returned, CreatedColumn):
        ref = {
            "kind": "added_column",
            "sheet_id": returned.sheet_id,
            "sheet_name": returned.sheet_name,
            "column_id": returned.column_id,
            "column_name": returned.name,
            "type": returned.column_type,
            "position": returned.position,
            "op_id": returned.op_id,
        }
        output = ActionOutput(
            kind="column",
            name=returned.name,
            sheet_id=returned.sheet_id,
            column_id=returned.column_id,
            ref=ref,
        )
        inputs = [
            ReceiptIO(
                name="target_sheet",
                ref={"kind": "target_sheet", "sheet_id": returned.sheet_id},
            )
        ]
        outputs = [ReceiptIO(name=returned.name, ref=ref)]
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "added_column",
                    "sheet_id": returned.sheet_id,
                    "column_id": returned.column_id,
                    "op_id": returned.op_id,
                },
                retention="pinned",
            )
        ]
    elif isinstance(returned, CreatedRow):
        ref = {
            "kind": "added_rows",
            "sheet_id": returned.sheet_id,
            "row_ids": [returned.row_id],
            "op_id": returned.op_id,
            "total": returned.total,
        }
        output = ActionOutput(
            kind="rows",
            name="added_rows",
            sheet_id=returned.sheet_id,
            row_ids=[returned.row_id],
            ref=ref,
        )
        inputs = [
            ReceiptIO(
                name="target_sheet",
                ref={"kind": "target_sheet", "sheet_id": returned.sheet_id},
            )
        ]
        outputs = [ReceiptIO(name="added_rows", ref=ref)]
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "added_row",
                    "sheet_id": returned.sheet_id,
                    "row_id": returned.row_id,
                    "op_id": returned.op_id,
                },
                retention="pinned",
            ),
            *(
                ReceiptEvidence(
                    ref={
                        "kind": "source_cell",
                        "sheet_id": returned.sheet_id,
                        "row_id": returned.row_id,
                        "column_id": returned.column_ids[name],
                        "column_name": name,
                        "value_hash": _text_hash(json.dumps(value, sort_keys=True)),
                    },
                    retention="pinned",
                )
                for name, value in returned.cells.items()
            ),
        ]
    elif isinstance(returned, AppendedRows):
        row_ids = list(returned.row_ids)
        ref = {
            **_compact_import_rowset_ref(
                sheet_id=returned.sheet_id,
                row_ids=row_ids,
                op_id=returned.op_id,
            ),
            "total": returned.total,
        }
        output = ActionOutput(
            kind="rows",
            name="appended_rows",
            sheet_id=returned.sheet_id,
            row_ids=row_ids,
            ref=ref,
        )
        inputs = [
            ReceiptIO(
                name="target_sheet",
                ref={"kind": "target_sheet", "sheet_id": returned.sheet_id},
            )
        ]
        if returned.source:
            inputs.append(ReceiptIO(name="import_source", ref=dict(returned.source)))
        outputs = [ReceiptIO(name="appended_rows", ref=ref)]
        evidence = [ReceiptEvidence(ref=ref, retention="pinned")]
    elif isinstance(returned, UpdatedImportedRows):
        plan = capability._plan
        ref = {
            "kind": "updated_import_rows",
            "sheet_id": returned.sheet_id,
            "row_ids": list(returned.row_ids),
            "row_count": len(returned.row_ids),
            "edit_count": returned.edit_count,
            "cleared_cells": plan.cleared_cells,
            "matched": plan.matched,
            "unmatched": plan.unmatched,
            "op_id": returned.op_id,
        }
        output = ActionOutput(
            kind="edit",
            name="updated_rows",
            sheet_id=returned.sheet_id,
            row_ids=list(returned.row_ids),
            ref=ref,
        )
        inputs = [
            ReceiptIO(
                name="target_sheet",
                ref={"kind": "target_sheet", "sheet_id": returned.sheet_id},
            )
        ]
        if returned.source:
            inputs.append(ReceiptIO(name="import_source", ref=dict(returned.source)))
        outputs = [ReceiptIO(name="updated_rows", ref=ref)]
        evidence = [ReceiptEvidence(ref=ref, retention="pinned")]
    elif isinstance(returned, DeletedRows):
        ref = {
            "kind": "deleted_rows",
            "sheet_id": returned.sheet_id,
            "row_ids": list(returned.row_ids),
            "deleted": len(returned.row_ids),
            "op_id": returned.op_id,
            "total": returned.total,
        }
        output = ActionOutput(
            kind="rows",
            name="deleted_rows",
            sheet_id=returned.sheet_id,
            row_ids=list(returned.row_ids),
            ref=ref,
        )
        inputs = [
            ReceiptIO(
                name="target_sheet",
                ref={"kind": "target_sheet", "sheet_id": returned.sheet_id},
            )
        ]
        outputs = [ReceiptIO(name="deleted_rows", ref=ref)]
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "deleted_row",
                    "sheet_id": returned.sheet_id,
                    "row_id": row_id,
                    "op_id": returned.op_id,
                },
                retention="pinned",
            )
            for row_id in returned.row_ids
        ]
    elif isinstance(returned, RetypedColumn):
        if not isinstance(capability, _ColumnTyper):
            raise TypeError("column type result requires its host capability")
        ref = {
            "kind": "column_type_update",
            "sheet_id": returned.sheet_id,
            "sheet_name": returned.sheet_name,
            "column_id": returned.column_id,
            "column_name": returned.column_name,
            "type_before": returned.type_before,
            "type_after": returned.type_after,
            "op_id": returned.op_id,
        }
        output = ActionOutput(
            kind="column",
            name=returned.column_name,
            sheet_id=returned.sheet_id,
            column_id=returned.column_id,
            ref=ref,
        )
        inputs = [
            ReceiptIO(
                name="target_column",
                ref={
                    "kind": "target_column",
                    "sheet_id": returned.sheet_id,
                    "sheet_name": returned.sheet_name,
                    "column_id": returned.column_id,
                    "column_name": returned.column_name,
                    "type_before": returned.type_before,
                },
            )
        ]
        outputs = [ReceiptIO(name=returned.column_name, ref=ref)]
        evidence = [
            ReceiptEvidence(
                ref={
                    "kind": "column_type_transition",
                    "sheet_id": returned.sheet_id,
                    "column_id": returned.column_id,
                    "type_before": returned.type_before,
                    "type_after": returned.type_after,
                    "op_id": returned.op_id,
                },
                retention="pinned",
            ),
            ReceiptEvidence(
                ref={
                    "kind": "classified_column_values",
                    "sheet_id": returned.sheet_id,
                    "column_id": returned.column_id,
                    "type": returned.type_after,
                    "row_ids": list(capability._classified_row_ids),
                    "row_count": len(capability._classified_row_ids),
                    "invalid_row_ids": list(capability._invalid_row_ids),
                    "invalid_count": len(capability._invalid_row_ids),
                },
                retention="pinned",
            ),
        ]
    elif isinstance(returned, EditedCells):
        if not isinstance(capability, _CellEditor):
            raise TypeError("cell edit result requires its host capability")
        target_refs = [
            {
                "kind": "cell",
                "row_id": row_id,
                "column_id": column_id,
            }
            for row_id, column_id in returned.targets
        ]
        ref = {
            "kind": "manual_edit_batch",
            "op_id": returned.op_id,
            "edit_count": len(returned.targets),
            "targets": target_refs,
        }
        output = ActionOutput(
            kind="edit",
            name="manual_edits",
            row_ids=[row_id for row_id, _column_id in returned.targets],
            ref=ref,
        )
        inputs = [
            ReceiptIO(
                name="target_cells",
                ref={
                    "kind": "cell_edit_targets",
                    "op_id": returned.op_id,
                    "edit_count": len(returned.targets),
                    "targets": target_refs,
                },
            )
        ]
        outputs = [ReceiptIO(name="manual_edits", ref=ref)]
        evidence = [
            ReceiptEvidence(ref=dict(edit_ref), retention="pinned")
            for edit_ref in capability._edit_refs
        ]
    else:
        if not isinstance(capability, _QueryCellEditor):
            raise TypeError("query edit result requires its host capability")
        row_ids = list(returned.row_ids)
        rowset_hash = _text_hash(json.dumps(row_ids, separators=(",", ":")))
        target_refs = [
            {"kind": "cell", "row_id": row_id, "column_id": returned.column_id}
            for row_id in row_ids
        ]
        ref = {
            "kind": "query_cell_edit_batch",
            "op_id": returned.op_id,
            "sheet_id": returned.sheet_id,
            "column_id": returned.column_id,
            "column_name": returned.column_name,
            "query": dict(capability._query),
            "query_hash": returned.query_hash,
            "row_ids": row_ids,
            "row_count": capability._row_count,
            "total": capability._total,
            "edit_count": len(row_ids),
            "evaluator": dict(SHEET_FILTER_ROWSET_EVALUATOR),
            "targets": target_refs,
        }
        if len(row_ids) > MAX_CELL_EDITS:
            ref.pop("row_ids")
            ref["rowset_hash"] = rowset_hash
        output = ActionOutput(
            kind="edit",
            name="query_manual_edits",
            sheet_id=returned.sheet_id,
            column_id=returned.column_id,
            row_ids=row_ids,
            ref=ref,
        )
        target_ref = {
            "kind": "query_cell_edit_targets",
            "op_id": returned.op_id,
            "sheet_id": returned.sheet_id,
            "column_id": returned.column_id,
            "column_name": returned.column_name,
            "query_hash": returned.query_hash,
            "edit_count": len(row_ids),
            "targets": target_refs,
        }
        rowset_ref = {
            "kind": "query_cell_edit_rowset",
            "schema_version": capability._query["schema_version"],
            "sheet_id": returned.sheet_id,
            "query": dict(capability._query),
            "query_hash": returned.query_hash,
            "row_ids": row_ids,
            "row_count": capability._row_count,
            "total": capability._total,
            "offset": capability._offset,
            "limit": capability._limit,
            "evaluator": dict(SHEET_FILTER_ROWSET_EVALUATOR),
        }
        if len(row_ids) > MAX_CELL_EDITS:
            target_ref.pop("targets")
            target_ref["rowset_hash"] = rowset_hash
            rowset_ref.pop("row_ids")
            rowset_ref["rowset_hash"] = rowset_hash
        inputs = [
            ReceiptIO(
                name="query",
                ref={
                    "kind": "query_spec",
                    "schema_version": capability._query["schema_version"],
                    "query": dict(capability._query),
                    "query_hash": returned.query_hash,
                    "sheet_id": returned.sheet_id,
                },
            ),
            ReceiptIO(
                name="target_column",
                ref={
                    "kind": "target_column",
                    "sheet_id": returned.sheet_id,
                    "column_id": returned.column_id,
                    "column_name": returned.column_name,
                },
            ),
            ReceiptIO(name="target_cells", ref=target_ref),
        ]
        outputs = [ReceiptIO(name="query_manual_edits", ref=ref)]
        evidence = [
            ReceiptEvidence(ref=rowset_ref, retention="pinned"),
            *(
                ReceiptEvidence(ref=dict(edit_ref), retention="pinned")
                for edit_ref in capability._edit_refs
            ),
        ]
        provider_use = [
            {
                "provider": "local",
                "service": "frisket.querysets.sheet_filter",
                "version": "v1",
                "external_api": False,
                "cost_actual": 0.0,
            }
        ]
    receipt = Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        op_ids=[returned.op_id],
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=inputs,
        outputs=outputs,
        provider_use=provider_use,
        evidence=evidence,
    )
    result = ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        op_ids=[returned.op_id],
        outputs=[output],
        receipt_id=receipt_id,
    )
    return result, receipt


def _capability_factory(
    project: Any,
    cur: Any,
    action: _TypedProjectEnvelope,
    params_hash: str,
    terminal: _ProjectAction[Any, Any],
    deps: ExecutorDeps,
) -> _CallOnce:
    from frisket.engine.executor.review_replay_action import CAPABILITY_IMPL

    capability_type = {**_CAPABILITY_IMPL, **CAPABILITY_IMPL}[
        terminal.single_capability()
    ]
    if terminal.capabilities == (QueryCellEditor,):
        limits = deps.cell_edit_query_limits
        return capability_type(
            project,
            cur,
            action,
            params_hash,
            max_rows=limits.max_rows if limits is not None else None,
        )
    if terminal.capabilities in ((RowsAppender,), (RowsUpdater,)):
        limits = deps.import_workload_limits
        capability = capability_type(
            project,
            cur,
            action,
            params_hash,
            max_rows=limits.max_rows if limits is not None else None,
        )
        capability._file_sources = deps.local_file_sources
        return capability
    return capability_type(project, cur, action, params_hash)


def _family_result_and_receipt(
    returned: Any, **kwargs: Any
) -> tuple[ActionResult, Receipt]:
    if isinstance(
        returned,
        (
            ReviewDecision,
            AcceptedReplayValue,
            AcceptedReplayColumn,
            DismissedReplayValue,
        ),
    ):
        from frisket.engine.executor.review_replay_action import result_and_receipt

        return result_and_receipt(returned, **kwargs)
    return _result_and_receipt(returned, **kwargs)


def _perform(
    project: Any,
    cur: Any,
    action: _TypedProjectEnvelope,
    params: Any,
    *,
    terminal: _ProjectAction[Any, Any],
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: Any,
    deps: ExecutorDeps,
) -> ActionResult:
    del resolved
    capability = _capability_factory(project, cur, action, params_hash, terminal, deps)
    try:
        returned = terminal.handler(params, capability)
    except _Refusal as exc:
        return _failed_result(
            project_id=project_id, action_kind=action.kind, error=exc.error
        )
    if capability.result is None or returned != capability.result:
        raise TypeError("project mutation handler must return its capability result")
    result, receipt = _family_result_and_receipt(
        returned,
        capability=capability,
        action=action,
        project_id=project_id,
        action_id=action_id,
        receipt_id=receipt_id,
        params_hash=params_hash,
    )
    ReceiptStore(project).insert_completed(receipt, commit=False)
    return result


def _import_change_replay_error(project: Any, receipt: Receipt) -> ActionError | None:
    if not _receipt_ops_are_applied(project, receipt.op_ids):
        return ActionError(
            code="stale_replay",
            message="The imported row change was undone or reclaimed; use a new request key to apply it again.",
            action_kind=receipt.action_kind,
        )
    return None


def run_typed_mutation_action(
    project: Any,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    deps: ExecutorDeps | None = None,
) -> ActionResult:
    terminal = bound.action.definition.run
    if not supports_typed_mutation_action(terminal):
        raise TypeError("typed mutation executor requires a column or row action")
    if (
        isinstance(bound.params, ReviewDecisionParams)
        and bound.params.decision == "edit"
        and "value" not in bound.params.model_fields_set
    ):
        return _failed_result(
            project_id=project_id,
            action_kind=bound.action.action_id,
            error=ActionError(
                code="review_value_required",
                message=(
                    "review.decision edit requires a replacement value "
                    "(which may be null)"
                ),
                action_kind=bound.action.action_id,
                field="params.value",
            ),
        )
    envelope = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
        row_scope=bound.request.scope,
        confirmation=bound.request.confirmation,
    )
    params_hash = typed_request_hash(bound)
    executor_deps = deps or ExecutorDeps()
    spec = _ActionCoreSpec(
        kind=envelope.kind,
        params_model=terminal.params_model,
        body_kind="plain",
        params_hash_fn=lambda _action: params_hash,
        result_from_existing_fn=_child_sheet_deterministic_result_from_existing(
            _import_change_replay_error
            if terminal.capabilities in ((RowsAppender,), (RowsUpdater,))
            else None
        ),
        plain_perform_in_txn_fn=lambda project_, cur, action, params, **kwargs: (
            _perform(
                project_,
                cur,
                action,
                params,
                terminal=terminal,
                deps=executor_deps,
                **kwargs,
            )
        ),
        plain_exception_error_fn=lambda action: ActionError(
            code="project_write_failed",
            message=f"{action.kind} could not write mutation and receipt",
            action_kind=action.kind,
        ),
        plain_post_commit_fn=(
            (lambda _resolved: _refresh_pending_review_summary(project, project_id))
            if bound.action.action_id == "review.decision"
            else None
        ),
    )
    return _run_action_core_spec(
        project,
        envelope,
        bound.params,
        spec=spec,
        ctx=ExecutorContext(project_id=project_id, deps=executor_deps),
    )
