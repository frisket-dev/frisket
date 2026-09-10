from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from frisket.contracts.plugin_write_plan import (
    PROJECT_WRITES_CAPABILITY,
    AppendRowsOp,
    CreateColumnOp,
    CreateSheetOp,
    WritePlan,
)
from frisket.engine.store.artifact_timeline import TimelineError
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
)
from frisket.features.temporal_ingress import validate_project_contextual_value


class WritePlanError(Exception):
    """Base for all WritePlan rejections."""


class WritePlanCapabilityError(WritePlanError):
    """The plugin did not declare ``plugin:project_writes`` (§2d, loud rejection)."""


class WritePlanValidationError(WritePlanError):
    """The plan failed schema/referential/collision validation before any write."""


class WritePlanApplyError(WritePlanError):
    """A mid-apply failure occurred; the transaction was rolled back (zero state)."""


@dataclass(frozen=True)
class WritePlanReceipt:
    """Success receipt carried into the action receipt (§2c): plugin id, manifest
    sha, plan hash, and the applied counts."""

    plugin_id: str
    manifest_sha: str
    plan_hash: str
    counts: dict[str, int]
    op_id: int
    status: str = "applied"


@dataclass(frozen=True)
class AppliedTables:
    op_id: int
    sheet_ids: dict[str, int]
    column_ids: dict[str, int]
    row_ids: dict[str, list[int]]
    counts: dict[str, int]


def _coerce(plan: WritePlan | dict[str, Any]) -> WritePlan:
    if isinstance(plan, WritePlan):
        return plan
    try:
        return WritePlan.model_validate(plan)
    except Exception as exc:  # pydantic ValidationError or ValueError from the model
        raise WritePlanValidationError(str(exc)) from exc


def plan_hash(plan: WritePlan | dict[str, Any]) -> str:
    """Canonical, key-order-stable content hash of a plan (receipts + idempotency)."""
    model = _coerce(plan)
    canonical = json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_live_collisions(project: Any, model: WritePlan) -> None:
    """Disposition #4: create_sheet on an existing sheet name hard-fails. (Columns
    are only ever created on plan-new sheets, so no live column collision exists in
    v1; in-plan name dupes are already rejected by the schema validator.)"""
    for op in model.ops:
        if isinstance(op, CreateSheetOp):
            row = project.db.execute(
                "SELECT 1 FROM sheets WHERE name=?", (op.name,)
            ).fetchone()
            if row is not None:
                raise WritePlanValidationError(
                    f"create_sheet collides with an existing sheet name: {op.name}"
                )


def _validate_temporal_values(project: Any, model: WritePlan) -> None:
    """Resolve temporal WritePlan values in the target project's namespace.

    The additive WritePlan schema intentionally leaves non-temporal plugin
    column types and values open.  This preflight therefore observes only the
    four core temporal types and leaves every other plan byte-for-byte on its
    existing path.
    """

    types_by_column_ref: dict[str, str] = {}
    for op in model.ops:
        if isinstance(op, CreateColumnOp):
            types_by_column_ref[op.column_ref] = op.type
            continue
        if not isinstance(op, AppendRowsOp):
            continue
        for row_index, row in enumerate(op.rows):
            for column_ref, value in row.items():
                type_name = types_by_column_ref[column_ref]
                try:
                    validate_project_contextual_value(
                        project,
                        type_name=type_name,
                        value=value,
                    )
                except TimelineError as exc:
                    raise WritePlanValidationError(
                        "append_rows temporal value failed validation "
                        f"(row {row_index + 1}, column_ref {column_ref!r}, "
                        f"reason {exc.code})"
                    ) from exc


def apply_write_plan(
    project: Any,
    plan: WritePlan | dict[str, Any],
    *,
    plugin_id: str,
    manifest_sha: str,
    declared_capabilities: tuple[str, ...] | list[str] | set[str],
) -> WritePlanReceipt:
    """Validate and atomically apply a plugin WritePlan on the host.

    Raises ``WritePlanCapabilityError`` when project_writes is undeclared,
    ``WritePlanValidationError`` for a bad/colliding plan (before any write), and
    ``WritePlanApplyError`` on a rolled-back mid-apply failure. Returns a
    ``WritePlanReceipt`` on success.
    """
    caps = set(declared_capabilities or ())
    if PROJECT_WRITES_CAPABILITY not in caps:
        raise WritePlanCapabilityError(
            f"plugin {plugin_id!r} did not declare {PROJECT_WRITES_CAPABILITY}"
        )

    model = _coerce(plan)
    digest = plan_hash(model)

    applied = apply_additive_tables(
        project,
        model,
        operation_kind="plugin.write_plan",
        label=f"{plugin_id} write plan",
        provenance={"plugin_id": plugin_id, "manifest_sha": manifest_sha},
    )
    return WritePlanReceipt(
        plugin_id, manifest_sha, digest, applied.counts, applied.op_id
    )


def apply_additive_tables(
    project: Any,
    model: WritePlan,
    *,
    operation_kind: str,
    label: str,
    provenance: dict[str, Any],
) -> AppliedTables:
    """Apply the existing closed additive plan, including in a host transaction."""
    model = _coerce(model)
    _check_live_collisions(project, model)
    plan_digest = plan_hash(model)
    db = project.db
    nested = bool(getattr(db, "in_transaction", False))
    savepoint = "plugin_write_plan"
    cur = db.cursor()
    if nested:
        cur.execute(f"SAVEPOINT {savepoint}")
    else:
        cur.execute("BEGIN IMMEDIATE")
    try:
        _validate_temporal_values(project, model)
        # Truncate any redo stack, mirroring the core write idiom.
        cur.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
        cur.execute(
            "INSERT INTO ops (kind, label, spec, undo_info, barrier) "
            "VALUES (?, ?, '{}', '{}', 0)",
            (operation_kind, label),
        )
        op_id = int(cur.lastrowid)
        cur.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
        producer_id = create_base_cell_producer(db, stage_id=f"op:{op_id}", op_id=op_id)

        sheet_ids: dict[str, int] = {}
        column_ids: dict[str, int] = {}
        row_ids: dict[str, list[int]] = {}
        created_sheets: list[int] = []
        created_columns: list[int] = []
        created_rows: list[int] = []
        cell_writes: list[BaseCellWrite] = []

        for op in model.ops:
            if isinstance(op, CreateSheetOp):
                cur.execute(
                    "INSERT INTO sheets (name, position) VALUES "
                    "(?, (SELECT COALESCE(MAX(position),0)+1 FROM sheets))",
                    (op.name,),
                )
                sheet_id = int(cur.lastrowid)
                sheet_ids[op.sheet_ref] = sheet_id
                row_ids[op.sheet_ref] = []
                created_sheets.append(sheet_id)
            elif isinstance(op, CreateColumnOp):
                sheet_id = sheet_ids[op.sheet_ref]
                cur.execute(
                    "INSERT INTO columns (sheet_id, name, type, ai_generated, format, "
                    "hidden, position) VALUES (?, ?, ?, 0, ?, ?, "
                    "(SELECT COALESCE(MAX(position),0)+1 FROM columns WHERE sheet_id=?))",
                    (sheet_id, op.name, op.type, op.format, int(op.hidden), sheet_id),
                )
                column_ids[op.column_ref] = int(cur.lastrowid)
                created_columns.append(int(cur.lastrowid))
            elif isinstance(op, AppendRowsOp):
                sheet_id = sheet_ids[op.sheet_ref]
                base = cur.execute(
                    "SELECT COALESCE(MAX(position),0) FROM rows WHERE sheet_id=?",
                    (sheet_id,),
                ).fetchone()[0]
                for i, row in enumerate(op.rows):
                    cur.execute(
                        "INSERT INTO rows (sheet_id, position) VALUES (?, ?)",
                        (sheet_id, base + i + 1),
                    )
                    row_id = int(cur.lastrowid)
                    created_rows.append(row_id)
                    row_ids[op.sheet_ref].append(row_id)
                    cell_writes.extend(
                        BaseCellWrite(row_id, column_ids[ref], value)
                        for ref, value in row.items()
                        if value is not None
                    )

        initialize_base_cells(db, producer_id=producer_id, cells=cell_writes)
        counts = {
            "sheets": len(created_sheets),
            "columns": len(created_columns),
            "rows": len(created_rows),
        }
        op_spec = {
            **provenance,
            "plan_hash": plan_digest,
            "counts": counts,
        }
        undo_info = {
            "created_sheets": created_sheets,
            "created_columns": created_columns,
            "created_rows": created_rows,
        }
        cur.execute(
            "UPDATE ops SET spec=?, undo_info=? WHERE id=?",
            (
                json.dumps(op_spec, sort_keys=True),
                json.dumps(undo_info, sort_keys=True),
                op_id,
            ),
        )

        if nested:
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            db.commit()
    except WritePlanError:
        _rollback(db, cur, nested, savepoint)
        raise
    except BaseException as exc:
        _rollback(db, cur, nested, savepoint)
        if not isinstance(exc, Exception):
            raise
        raise WritePlanApplyError(str(exc)) from exc

    return AppliedTables(op_id, sheet_ids, column_ids, row_ids, counts)


def _rollback(db: Any, cur: Any, nested: bool, savepoint: str) -> None:
    try:
        if nested:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            db.rollback()
    except Exception:
        # A failed rollback must not mask the original error.
        pass
