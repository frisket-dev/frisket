"""Host-owned atomic refresh of existing list and join sheets.

Supported refreshes are deterministic and do not execute a model. Join fanout
requires current scope confirmation. Live lease checks, replay,
row replacement, grounding, watermark and receipt share one transaction.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NoReturn

from frisket.actions.core import CreateSheet, _ProjectAction
from frisket.actions.join_types import JoinedTablesReader
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionRequest,
    ListTableReader,
    RefreshedSheet,
    SheetRefresher,
    TableError,
)
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
    canonical_column_type,
)
from frisket.engine.executor.action_families.imports import _preflight_table_rows
from frisket.engine.executor.action_receipts import _result_from_receipt
from frisket.engine.executor.action_reservations import _receipt_for_idempotency
from frisket.engine.executor.action_support import _failed_result, _new_id
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.executor.list_table_grounding import propagate_list_item_evidence
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.engine.store import Project
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    delete_sheet_rows,
    initialize_base_cells,
)
from frisket.engine.store.leases import lease_held
from frisket.engine.store.materialization import (
    MaterializedColumnSpec,
    SingleParentMaterializedRow,
    _insert_materialized_row_source,
)
from frisket.engine.store.receipts import ReceiptStore

logger = logging.getLogger("frisket.executor")


class _RefreshRefused(Exception):
    def __init__(self, error: ActionError):
        self.error = error


@dataclass(frozen=True)
class _RefreshFacts:
    sheet_id: int
    parent_sheet_id: int
    name: str
    op_id: int
    row_count: int
    match_stats: dict[str, Any] | None = None
    reads: tuple[dict[str, Any], ...] = ()


def _fail(
    *,
    project_id: str,
    code: str,
    message: str,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    raise _RefreshRefused(
        ActionError(
            code=code,
            message=message,
            field=field,
            details=details or {},
        )
    )


def supports_typed_sheet_refresh_action(terminal: Any) -> bool:
    return isinstance(terminal, _ProjectAction) and terminal.capabilities == (
        SheetRefresher,
    )


class _SheetRefresher:
    def __init__(
        self,
        project: Project,
        project_id: str,
        bound: BoundTypedActionRequest,
        request_hash: str,
    ) -> None:
        self.project = project
        self.project_id = project_id
        self.bound = bound
        self.request_hash = request_hash
        self.calls = 0
        self.facts: _RefreshFacts | None = None

    def refresh(self, sheet_id: int) -> RefreshedSheet:
        self.calls += 1
        if self.calls != 1:
            raise ValueError("a refresh action must refresh exactly one sheet")
        if type(sheet_id) is not int or sheet_id <= 0:
            raise ValueError("refresh sheet_id must be a positive integer")
        self.facts = _refresh_sheet(
            self.project, sheet_id, self.bound, self.request_hash, self.project_id
        )
        return RefreshedSheet(
            sheet_id=self.facts.sheet_id, refreshed_row_count=self.facts.row_count
        )


def run_typed_sheet_refresh_action(
    project: Project, project_id: str, bound: BoundTypedActionRequest
) -> ActionResult:
    terminal = bound.action.definition.run
    if not supports_typed_sheet_refresh_action(terminal):
        raise TypeError("typed refresh requires a sheet refresher action")
    kind = bound.action.action_id
    request_hash = typed_request_hash(bound)

    def replay() -> ActionResult | None:
        existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
        if existing is None:
            return None
        if existing["params_hash"] != request_hash:
            _fail(
                project_id=project_id,
                code="idempotency_conflict",
                message="idempotency_key was already used with a different request",
                field="idempotency_key",
            )
        return _result_from_receipt(
            Receipt.model_validate(json.loads(existing["body"]))
        )

    try:
        existing = replay()
        if existing is not None:
            return existing
        project.db.execute("BEGIN IMMEDIATE")
        existing = replay()
        if existing is not None:
            project.db.rollback()
            return existing
        capability = _SheetRefresher(project, project_id, bound, request_hash)
        returned = terminal.handler(bound.params, capability)
        if (
            capability.calls != 1
            or capability.facts is None
            or not isinstance(returned, RefreshedSheet)
        ):
            raise ValueError(
                "refresh action must refresh one sheet and return a domain result"
            )
        facts = capability.facts
        action_id, receipt_id = _new_id("act"), _new_id("receipt")
        sheet_ref = {
            "kind": "materialized_sheet",
            "sheet_id": facts.sheet_id,
            "parent_sheet_id": facts.parent_sheet_id,
            "op_id": facts.op_id,
        }
        refresh_ref = {
            "kind": "sheet_refresh",
            "sheet_id": facts.sheet_id,
            "op_id": facts.op_id,
            "refreshed_row_count": facts.row_count,
        }
        evidence = [ReceiptEvidence(ref=refresh_ref)]
        if facts.match_stats is not None:
            refresh_ref["parent_op_kind"] = "derive.join"
            evidence = [
                ReceiptEvidence(ref=refresh_ref),
                ReceiptEvidence(ref=facts.match_stats, retention="pinned"),
            ]
        evidence.extend(
            ReceiptEvidence(ref=fact, retention="pinned") for fact in facts.reads
        )
        receipt = Receipt(
            receipt_id=receipt_id,
            project_id=project_id,
            action_id=action_id,
            action_kind=kind,
            op_ids=[facts.op_id],
            idempotency_key=bound.request.idempotency_key,
            params_hash=request_hash,
            status="completed",
            inputs=[
                ReceiptIO(
                    name="target_sheet",
                    ref={
                        "kind": "sheet",
                        "sheet_id": facts.sheet_id,
                    },
                )
            ],
            outputs=[ReceiptIO(name=facts.name, ref=sheet_ref)],
            evidence=evidence,
        )
        result = ActionResult(
            action=ActionIdentity(kind=kind, action_id=action_id),
            status="completed",
            project_id=project_id,
            op_ids=[facts.op_id],
            receipt_id=receipt_id,
            outputs=[
                ActionOutput(
                    kind="sheet",
                    name=facts.name,
                    sheet_id=facts.sheet_id,
                    ref=sheet_ref,
                )
            ],
        )
        ReceiptStore(project).insert_completed(receipt, commit=False)
        project.db.commit()
        return result
    except (_RefreshRefused, TableReadRefused) as exc:
        project.db.rollback()
        error = exc.error.model_copy(update={"action_kind": kind})
    except TableError as exc:
        project.db.rollback()
        error = ActionError(
            code=exc.code,
            message=str(exc),
            action_kind=kind,
            details=exc.details,
        )
    except Exception:
        project.db.rollback()
        # Author exceptions may include private input. Log only static facts.
        logger.debug("action_failed", extra={"action_kind": kind})
        error = ActionError(
            code="project_write_failed",
            message="project write failed",
            action_kind=kind,
        )
    except BaseException:
        # Cancellation must propagate without leaving uncommitted refresh writes.
        project.db.rollback()
        raise
    if error.needs_confirmation:
        return ActionResult(
            action=ActionIdentity(kind=kind, action_id=_new_id("act")),
            status="needs_confirmation",
            project_id=project_id,
            errors=[error],
        )
    return _failed_result(project_id=project_id, action_kind=kind, error=error)


def _refresh_sheet(
    project: Project,
    sheet_id: int,
    bound: BoundTypedActionRequest,
    params_hash: str,
    project_id: str,
) -> _RefreshFacts:
    now = datetime.now(timezone.utc)
    cur = project.db.cursor()
    sheet = cur.execute(
        "SELECT * FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    if sheet is None:
        return _fail(
            project_id=project_id,
            code="sheet_not_found",
            message="sheet.refresh target sheet does not exist",
            field="sheet_id",
            details={"sheet_id": sheet_id},
        )
    if sheet["parent_sheet_id"] is None or sheet["parent_op_id"] is None:
        return _fail(
            project_id=project_id,
            code="refresh_root_sheet_unsupported",
            message="root sheets have no parent op to refresh from",
            field="sheet_id",
            details={"sheet_id": sheet_id},
        )
    managed_column = cur.execute(
        "SELECT generation.column_id FROM run_output_generations generation "
        "JOIN columns column ON column.id=generation.column_id "
        "WHERE column.sheet_id=? ORDER BY generation.column_id LIMIT 1",
        (sheet_id,),
    ).fetchone()
    if managed_column is not None:
        return _fail(
            project_id=project_id,
            code="refresh_unsupported",
            message="generation-managed sheets cannot be refreshed in place",
            field="sheet_id",
            details={
                "sheet_id": sheet_id,
                "column_id": int(managed_column["column_id"]),
                "reason": "generation_managed_rows",
            },
        )

    # Live refresh lease (embedding_indexes idiom): a non-expired token means
    # another refresh of this sheet is in flight. Expiry compares PARSED
    # datetimes (`lease_held`) — a raw string compare breaks under writer
    # format skew (SQLite datetime('now') vs isoformat).
    if lease_held(sheet["refresh_claim_token"], sheet["refresh_lease_expires_at"], now):
        return _fail(
            project_id=project_id,
            code="sheet_refresh_busy",
            message="another refresh of this sheet is already in progress",
            field="sheet_id",
            details={"sheet_id": sheet_id},
        )

    parent_op = cur.execute(
        "SELECT * FROM ops WHERE id=?", (int(sheet["parent_op_id"]),)
    ).fetchone()
    parent_op_kind = parent_op["kind"] if parent_op is not None else None

    # Saved canonical authoring determines refresh support even for an empty
    # join. Membership rows describe outputs, not the operation that made them.
    derive_request = _reconstruct_derive_request(parent_op, bound.request)
    saved_action = (
        ACTION_REGISTRY.get(derive_request.action_id)
        if derive_request is not None
        else None
    )
    if (
        saved_action is not None
        and isinstance(saved_action.definition.run, CreateSheet)
        and JoinedTablesReader in saved_action.definition.run.capabilities
    ):
        return _refresh_join(
            project,
            cur,
            sheet,
            parent_op,
            derive_request,
            bound,
            params_hash,
            project_id,
        )

    # Other multi-parent families have no admitted in-place refresh primitive.
    is_multi_parent = (
        cur.execute(
            "SELECT 1 FROM materialized_row_sources mrs "
            "JOIN rows r ON mrs.materialized_row_id = r.id "
            "WHERE r.sheet_id=? LIMIT 1",
            (sheet_id,),
        ).fetchone()
        is not None
    )
    if is_multi_parent:
        return _fail(
            project_id=project_id,
            code="refresh_unsupported",
            message=(
                "multi-parent (resolve/reduce/link_table) sheets cannot be "
                "refreshed in place in v1"
            ),
            field="sheet_id",
            details={
                "sheet_id": sheet_id,
                "parent_op_kind": parent_op_kind,
            },
        )

    if parent_op is None or parent_op_kind != "derive.table_from_list":
        return _fail(
            project_id=project_id,
            code="refresh_unsupported",
            message="only single-parent derive.table_from_list sheets refresh in v1",
            field="sheet_id",
            details={
                "sheet_id": sheet_id,
                "parent_op_kind": parent_op_kind,
            },
        )

    if derive_request is None:
        return _fail(
            project_id=project_id,
            code="refresh_unsupported",
            message="stored derive request could not be reconstructed",
            field="sheet_id",
        )
    from frisket.engine.executor.table_action import prepare_table_producer

    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(derive_request.action_id), derive_request
    )
    with prepare_table_producer(project, bound, check_sheet_name=False) as prepared:
        reader = prepared.readers[ListTableReader]
        if prepared.parent_sheet_id != int(sheet["parent_sheet_id"]):
            raise TableError(
                "invalid_input_ref", "refreshed list must retain its parent sheet"
            )
        rows = []
        sources = []
        for values, lineage, _files in prepared.rows:
            source = lineage.parent
            if source is None or source not in reader.item_associations:
                raise TableError(
                    "invalid_input_ref", "list-item source was not admitted"
                )
            rows.append(values)
            sources.append(source)
        checked_rows = _preflight_table_rows(
            project,
            prepared.table.model_copy(update={"rows": rows}),
            action_kind=derive_request.action_id,
        )
        if isinstance(checked_rows, ActionError):
            raise TableReadRefused(checked_rows)
        resolved = {
            "columns": [
                MaterializedColumnSpec(
                    name=column.name,
                    type=column.type,
                    ai_generated=reader.source_ai_generated,
                    hidden=column.hidden,
                    format=column.format,
                )
                for column in prepared.table.columns
            ],
            "rows": [
                SingleParentMaterializedRow(source.row_id, values)
                for source, values in zip(sources, checked_rows, strict=True)
            ],
        }
        op_id, child_row_ids = _rewrite_child_sheet_in_place(
            cur,
            sheet_id=sheet_id,
            parent_sheet_id=int(sheet["parent_sheet_id"]),
            resolved=resolved,
            params_hash=params_hash,
        )
        propagate_list_item_evidence(
            project,
            sheet_id=sheet_id,
            op_id=op_id,
            child_row_ids=child_row_ids,
            sources=sources,
            item_associations=reader.item_associations,
        )
    refreshed_rows = len(child_row_ids)
    cur.execute(
        "UPDATE sheets SET last_verified_op_cursor=?, "
        "refresh_claim_token=NULL, refresh_lease_expires_at=NULL WHERE id=?",
        (op_id, sheet_id),
    )

    return _RefreshFacts(
        sheet_id=sheet_id,
        parent_sheet_id=int(sheet["parent_sheet_id"]),
        name=sheet["name"],
        op_id=op_id,
        row_count=refreshed_rows,
    )


def _refresh_join(
    project: Project,
    cur: Any,
    sheet: Any,
    parent_op: Any,
    derive_request: ActionRequest,
    bound: BoundTypedActionRequest,
    params_hash: str,
    project_id: str,
) -> _RefreshFacts:
    from frisket.engine.executor.joined_tables_read import JoinRefreshAdmission
    from frisket.engine.executor.table_action import prepare_table_producer

    join_bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(derive_request.action_id),
        derive_request.model_copy(update={"sheet_name": str(sheet["name"])}),
    )
    with prepare_table_producer(
        project,
        join_bound,
        check_sheet_name=False,
        join_refresh=JoinRefreshAdmission(
            action_kind=bound.action.action_id,
            request_hash=params_hash,
            sheet_id=int(sheet["id"]),
            parent_op_id=int(parent_op["id"]),
            confirmation=bound.request.confirmation,
        ),
    ) as prepared:
        reader = prepared.readers[JoinedTablesReader]
        if prepared.parent_sheet_id != int(sheet["parent_sheet_id"]):
            raise TableError(
                "invalid_input_ref", "refreshed join must retain its parent sheet"
            )
        rows = []
        for values, lineage, files in prepared.rows:
            if files:
                raise TableError(
                    "invalid_input_ref", "join refresh cannot publish new file handles"
                )
            reader.validate_lineage(lineage.sources, lineage.parent)
            rows.append((values, lineage))
        reads = tuple(deepcopy(reader.facts))
        op_id, row_count = _rewrite_join_sheet_in_place(
            cur,
            sheet_id=int(sheet["id"]),
            columns=prepared.table.columns,
            rows=rows,
            source_roles=reader.source_roles,
            reads=reads,
            params_hash=params_hash,
        )
    cur.execute(
        "UPDATE sheets SET last_verified_op_cursor=?, "
        "refresh_claim_token=NULL, refresh_lease_expires_at=NULL WHERE id=?",
        (op_id, int(sheet["id"])),
    )
    stats = reads[0]["stats"]
    return _RefreshFacts(
        sheet_id=int(sheet["id"]),
        parent_sheet_id=int(sheet["parent_sheet_id"]),
        name=sheet["name"],
        op_id=op_id,
        row_count=row_count,
        match_stats={
            "kind": "derive_join_match_stats",
            "how": reads[0]["how"],
            **stats,
        },
        reads=reads,
    )


def _reconstruct_derive_request(
    parent_op: Any, refresh_request: ActionRequest
) -> ActionRequest | None:
    if parent_op is None:
        return None
    try:
        spec = json.loads(parent_op["spec"] or "{}")
        if not isinstance(spec, dict) or spec.get("action_id") != parent_op["kind"]:
            return None
        ACTION_REGISTRY.get(spec["action_id"])
        # Only saved authoring intent survives. Original execution consent and
        # idempotency never authorize a new refresh.
        return ActionRequest.model_validate(
            {
                **{
                    key: spec[key]
                    for key in ActionRequest.model_fields
                    if key in spec
                    and key
                    not in {"idempotency_key", "confirmation", "replace_existing"}
                },
                "idempotency_key": refresh_request.idempotency_key,
            }
        )
    except (KeyError, ValueError, TypeError):
        return None


def _rewrite_join_sheet_in_place(
    cur: Any,
    *,
    sheet_id: int,
    columns: Any,
    rows: list[Any],
    source_roles: dict[Any, str],
    reads: tuple[dict[str, Any], ...],
    params_hash: str,
) -> tuple[int, int]:
    """Replace joined rows and membership, preserving the sheet and column IDs.

    The enclosing refresh transaction already refuses generation-managed
    columns. Their ordinary descriptors can therefore follow the current schema
    atomically with the complete replacement of their cells.
    """
    cur.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
    op_spec = {
        "kind": "sheet.refresh",
        "sheet_id": sheet_id,
        "params_hash": params_hash,
        "materialized_row_count": len(rows),
        "reads": reads,
    }
    cur.execute(
        "INSERT INTO ops (kind, label, spec, undo_info, barrier) "
        "VALUES ('sheet.refresh', ?, ?, '{}', 0)",
        (f"refresh join sheet {sheet_id}", json.dumps(op_spec, sort_keys=True)),
    )
    op_id = int(cur.lastrowid)
    cur.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    producer_id = create_base_cell_producer(
        cur.connection, stage_id=f"op:{op_id}", op_id=op_id
    )
    column_ids = {
        row["name"]: int(row["id"])
        for row in cur.execute(
            "SELECT id, name FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    max_pos = cur.execute(
        "SELECT COALESCE(MAX(position), 0) FROM columns WHERE sheet_id=?", (sheet_id,)
    ).fetchone()[0]
    for column in columns:
        name = column.name
        if name in column_ids:
            cur.execute(
                "UPDATE columns SET type=?, format=?, hidden=? WHERE id=?",
                (
                    canonical_column_type(column.type),
                    column.format,
                    column.hidden,
                    column_ids[name],
                ),
            )
        else:
            max_pos += 1
            cur.execute(
                "INSERT INTO columns (sheet_id, name, type, position, ai_generated, hidden, format) "
                "VALUES (?, ?, ?, ?, 0, ?, ?)",
                (
                    sheet_id,
                    name,
                    canonical_column_type(column.type),
                    max_pos,
                    column.hidden,
                    column.format,
                ),
            )
            column_ids[name] = int(cur.lastrowid)

    # Cells, visible projections, and old membership cascade with the rows.
    delete_sheet_rows(cur.connection, sheet_id=sheet_id, producer_id=producer_id)
    cell_writes: list[BaseCellWrite] = []
    for position, (values, lineage) in enumerate(rows, start=1):
        cur.execute(
            "INSERT INTO rows (sheet_id, position, parent_row_id) VALUES (?, ?, ?)",
            (sheet_id, position, lineage.parent.row_id if lineage.parent else None),
        )
        row_id = int(cur.lastrowid)
        cell_writes.extend(
            BaseCellWrite(row_id, column_ids[name], value)
            for name, value in values.items()
            if value is not None
        )
        for source in lineage.sources:
            _insert_materialized_row_source(
                cur,
                materialized_row_id=row_id,
                source_row_id=source.row_id,
                op_id=op_id,
                role=source_roles[source],
            )
    initialize_base_cells(cur.connection, producer_id=producer_id, cells=cell_writes)
    return op_id, len(rows)


def _rewrite_child_sheet_in_place(
    cur: Any,
    *,
    sheet_id: int,
    parent_sheet_id: int,
    resolved: dict[str, Any],
    params_hash: str,
) -> tuple[int, list[int]]:
    """Replace the child sheet's rows/cells from the freshly-resolved plan, in
    place. Columns are reconciled by name (missing ones added); existing rows are
    deleted (cells cascade) and re-inserted. Appends the sheet.refresh op."""
    cur.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
    op_spec = {
        "kind": "sheet.refresh",
        "sheet_id": sheet_id,
        "params_hash": params_hash,
        "materialized_row_count": len(resolved["rows"]),
    }
    cur.execute(
        "INSERT INTO ops (kind, label, spec, undo_info, barrier) "
        "VALUES ('sheet.refresh', ?, ?, '{}', 0)",
        (f"refresh sheet {sheet_id}", json.dumps(op_spec, sort_keys=True)),
    )
    op_id = int(cur.lastrowid)
    cur.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    producer_id = create_base_cell_producer(
        cur.connection, stage_id=f"op:{op_id}", op_id=op_id
    )

    # Reconcile columns by name.
    existing_columns = {
        row["name"]: int(row["id"])
        for row in cur.execute(
            "SELECT id, name FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }
    max_pos = cur.execute(
        "SELECT COALESCE(MAX(position), 0) FROM columns WHERE sheet_id=?", (sheet_id,)
    ).fetchone()[0]
    column_ids: dict[str, int] = dict(existing_columns)
    for column in resolved["columns"]:
        if column.name in column_ids:
            continue
        max_pos += 1
        cur.execute(
            "INSERT INTO columns (sheet_id, name, type, position, "
            "ai_generated, hidden, format) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sheet_id,
                column.name,
                canonical_column_type(column.type),
                max_pos,
                int(column.ai_generated),
                int(column.hidden),
                column.format,
            ),
        )
        column_ids[column.name] = int(cur.lastrowid)

    # Evidence subjects are not foreign keys; retire links before row IDs can
    # be reused by the replacement rows. Historical links and spans survive.
    cur.execute(
        "UPDATE evidence_links SET status='stale', stale_reason='sheet_refreshed', "
        "stale_at=datetime('now') WHERE sheet_id=? AND status='active' "
        "AND row_id IN (SELECT id FROM rows WHERE sheet_id=?)",
        (sheet_id, sheet_id),
    )
    # Cells and visible projections cascade with the replaced rows.
    delete_sheet_rows(cur.connection, sheet_id=sheet_id, producer_id=producer_id)
    child_row_ids = []
    cell_writes: list[BaseCellWrite] = []
    for idx, row in enumerate(resolved["rows"], start=1):
        cur.execute(
            "INSERT INTO rows (sheet_id, position, parent_row_id) VALUES (?, ?, ?)",
            (sheet_id, idx, int(row.parent_row_id)),
        )
        row_id = int(cur.lastrowid)
        child_row_ids.append(row_id)
        cell_writes.extend(
            BaseCellWrite(row_id, column_ids[name], value)
            for name, value in row.values.items()
            if value is not None and name in column_ids
        )
    initialize_base_cells(cur.connection, producer_id=producer_id, cells=cell_writes)
    return op_id, child_row_ids
