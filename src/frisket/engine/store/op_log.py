"""Op log for a project bundle: append, undo/redo by op-status flip and
pointer restore, and history reads. Undo/redo move op-log pointers and column
run-pointers; nothing is copied and nothing is deleted. Free functions over
the facade's per-thread SQLite connection; ``project`` stays duck-typed
(``Any``) so this leaf never re-imports the facade module."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.import_sessions import (
    ImportSessionConflict,
    invalidate_pdf_split_generation,
    set_import_session_rows_hidden,
    set_import_session_replaced_rows_hidden,
)
from frisket.engine.store.current_cells import (
    refresh_current_cell_pairs,
    refresh_current_cells,
)
from frisket.engine.store.result_generations import ResultGenerationStore


def _valid_review_metadata(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("decision") in {None, "accept", "reject", "reject_clear", "edit"}
        and (value.get("note") is None or isinstance(value.get("note"), str))
        and set(value) <= {"decision", "note"}
    )


def append_op(
    project: Any,
    kind: str,
    spec: dict[str, Any] | None = None,
    label: str | None = None,
    barrier: bool = False,
    *,
    undo_info: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> int:
    """Prepare an operation; its first mutation starts Change navigation."""
    cur = project.db.execute(
        "INSERT INTO ops (kind, label, spec, undo_info, barrier) VALUES (?, ?, ?, ?, ?)",
        (
            kind,
            label,
            json.dumps(spec or {}),
            json.dumps(dict(undo_info or {})),
            int(barrier),
        ),
    )
    op_id = cur.lastrowid
    if undo_info:
        from .column_descriptors import record_descriptor_changes
        from .structural_changes import record_structural_changes

        record_descriptor_changes(project.db, op_id, undo_info)
        from .review_judgments import record_judgment_changes

        record_judgment_changes(project.db, op_id, undo_info)
        record_structural_changes(project.db, op_id, undo_info)
    if commit:
        project.db.commit()
    return op_id


def set_undo_info(
    project: Any, op_id: int, undo_info: dict[str, Any], *, commit: bool = True
) -> None:
    project.db.execute(
        "UPDATE ops SET undo_info=? WHERE id=?", (json.dumps(undo_info), op_id)
    )
    from .column_descriptors import record_descriptor_changes

    record_descriptor_changes(project.db, op_id, undo_info)
    from .review_judgments import record_judgment_changes

    record_judgment_changes(project.db, op_id, undo_info)
    from .structural_changes import record_structural_changes

    record_structural_changes(project.db, op_id, undo_info)
    if commit:
        project.db.commit()


HISTORY_ORDER = "COALESCE(change_order,0),id"


def history(project: Any) -> list[sqlite3.Row]:
    return project.db.execute(f"SELECT * FROM ops ORDER BY {HISTORY_ORDER}").fetchall()


def history_page(project: Any, offset: int, limit: int) -> list[sqlite3.Row]:
    return project.db.execute(
        f"SELECT * FROM ops ORDER BY {HISTORY_ORDER} LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()


def history_total(project: Any) -> int:
    return int(project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] or 0)


def history_index(project: Any, op_id: int) -> int:
    row = project.db.execute(
        "SELECT COUNT(*) FROM ops WHERE (COALESCE(change_order,0),id) <= "
        "(SELECT COALESCE(change_order,0),id FROM ops WHERE id=?)",
        (op_id,),
    ).fetchone()
    return int(row[0]) - 1


def history_cursor_index(project: Any) -> int:
    return history_index(project, project.op_cursor)


def operation_target(project: Any, direction: str, *, op_id: int | None = None):
    """Use the same target selection for execution, HTTP history and navigation."""
    if direction in {"revert", "restore"}:
        status = "applied" if direction == "revert" else "undone"
        return project.db.execute(
            "SELECT * FROM ops WHERE id=? AND status=? AND change_order IS NOT NULL",
            (op_id, status),
        ).fetchone()
    cursor = project.db.execute(
        "SELECT change_order FROM ops WHERE id=?", (project.op_cursor,)
    ).fetchone()
    order = int(cursor[0]) if cursor is not None and cursor[0] is not None else 0
    if direction == "undo":
        return project.db.execute(
            "SELECT * FROM ops WHERE change_order<=? AND status='applied' "
            "ORDER BY change_order DESC LIMIT 1",
            (order,),
        ).fetchone()
    if direction == "redo":
        return project.db.execute(
            "SELECT * FROM ops WHERE change_order>? AND status='undone' "
            "AND redo_eligible=1 ORDER BY change_order LIMIT 1",
            (order,),
        ).fetchone()
    raise ValueError(f"unknown operation transition {direction!r}")


@dataclass(frozen=True)
class OperationTransition:
    target: sqlite3.Row
    undo_info: dict[str, Any]
    column_ids: frozenset[int]
    direction: Literal["undo", "redo", "revert", "restore"]
    cursor_before: int
    cursor_after: int
    status_before: Literal["applied", "undone"]
    status_after: Literal["applied", "undone"]
    pending_review_count_unchanged: bool


class OperationUnavailable(Exception):
    pass


class OperationMismatch(Exception):
    def __init__(self, expected_op_id: int, actual_op_id: int):
        self.expected_op_id = expected_op_id
        self.actual_op_id = actual_op_id


class IrreversibleOperation(Exception):
    pass


class RestorationUnavailable(ValueError):
    """Retained results exist, but restoring their destinations would conflict."""


def _require_restoration_targets(project, op_id, info, *, reverting):
    restoring_sheets = info.get("deleted_sheets" if reverting else "created_sheets", [])
    restoring_columns = info.get(
        "deleted_columns" if reverting else "created_columns", []
    )
    restoring_rows = info.get("deleted_rows" if reverting else "created_rows", [])
    if not reverting:
        restoring_rows = [*restoring_rows, *info.get("restored_rows", [])]
    db = project.db
    for sheet_id in restoring_sheets:
        sheet = db.execute("SELECT name FROM sheets WHERE id=?", (sheet_id,)).fetchone()
        if sheet is None:
            raise RestorationUnavailable("The original sheet is no longer available")
        if db.execute(
            "SELECT 1 FROM sheets WHERE name=? AND hidden=0 AND id<>?",
            (sheet[0], sheet_id),
        ).fetchone():
            raise RestorationUnavailable(f"A sheet named {sheet[0]!r} already exists")
    for column_id in restoring_columns:
        column = db.execute(
            "SELECT col.name,col.sheet_id,sheet.hidden FROM columns col JOIN sheets sheet ON sheet.id=col.sheet_id WHERE col.id=?",
            (column_id,),
        ).fetchone()
        if column is None or (column[2] and column[1] not in restoring_sheets):
            raise RestorationUnavailable(
                "The original destination sheet is unavailable"
            )
        if db.execute(
            "SELECT 1 FROM columns WHERE sheet_id=? AND name=? AND active=1 AND id<>?",
            (column[1], column[0], column_id),
        ).fetchone():
            raise RestorationUnavailable(f"A column named {column[0]!r} already exists")
    if (
        restoring_rows
        and db.execute(
            "SELECT 1 FROM json_each(?) wanted LEFT JOIN rows row ON row.id=wanted.value LEFT JOIN sheets sheet ON sheet.id=row.sheet_id "
            "WHERE row.id IS NULL OR (sheet.hidden=1 AND sheet.id NOT IN (SELECT value FROM json_each(?))) LIMIT 1",
            (json.dumps(restoring_rows), json.dumps(restoring_sheets)),
        ).fetchone()
    ):
        raise RestorationUnavailable(
            "An original destination row or sheet is unavailable"
        )
    if not reverting:
        # Inputs may have disappeared: retained outputs are independent. Only
        # their destinations must still exist. Keep large scopes in SQLite.
        invalid = db.execute(
            "WITH destinations AS (SELECT row_id,column_id FROM edits WHERE op_id=? UNION "
            "SELECT result.row_id,result.column_id FROM results result JOIN runs run ON run.id=result.run_id WHERE run.op_id=? UNION "
            "SELECT row_id,column_id FROM copied_result_outcomes WHERE op_id=?) "
            "SELECT 1 FROM destinations target LEFT JOIN rows row ON row.id=target.row_id "
            "LEFT JOIN columns col ON col.id=target.column_id LEFT JOIN sheets sheet ON sheet.id=col.sheet_id "
            "WHERE (row.id IS NULL OR col.id IS NULL OR sheet.id IS NULL "
            "OR (row.hidden=1 AND row.id NOT IN (SELECT value FROM json_each(?))) "
            "OR (col.active=0 AND col.id NOT IN (SELECT value FROM json_each(?))) "
            "OR (sheet.hidden=1 AND sheet.id NOT IN (SELECT value FROM json_each(?)))) "
            "AND NOT EXISTS (SELECT 1 FROM project_purge_targets purged WHERE "
            "(purged.kind='row' AND purged.object_id=target.row_id) OR "
            "(purged.kind='column' AND purged.object_id=target.column_id) OR "
            "(purged.kind='sheet' AND purged.object_id=sheet.id)) LIMIT 1",
            (
                op_id,
                op_id,
                op_id,
                json.dumps(restoring_rows),
                json.dumps(restoring_columns),
                json.dumps(restoring_sheets),
            ),
        ).fetchone()
        if invalid:
            raise RestorationUnavailable(
                "An original output destination is unavailable; restore it first"
            )


class CorruptOperation(Exception):
    def __init__(self, op_id: int, message: str):
        self.op_id = op_id
        super().__init__(message)


class ClaimedOperation(Exception):
    def __init__(self, op_id: int, claim: Mapping[str, Any]):
        self.op_id = op_id
        self.claim = claim


def _plain_edit_preserves_pending_review_count(
    project: Any, target: sqlite3.Row, undo_info: Mapping[str, Any]
) -> bool:
    """Prove that a transition only toggles ordinary cell edit overlays."""

    if str(target["kind"]) != "edit":
        return False
    # Evidence staleness follows a value edit but does not participate in the
    # review summary. Any other transition metadata is an unknown until its
    # effect on review state, visibility, or result identity is proven.
    if set(undo_info) - {"evidence_links_staled"}:
        return False
    facts = project.db.execute(
        "SELECT EXISTS(SELECT 1 FROM edits WHERE op_id=?) AS has_edits,"
        "EXISTS(SELECT 1 FROM runs WHERE op_id=?) AS has_runs",
        (int(target["id"]), int(target["id"])),
    ).fetchone()
    return bool(facts["has_edits"]) and not bool(facts["has_runs"])


def step_operation(
    project: Any,
    direction: Literal["undo", "redo", "revert", "restore"],
    *,
    expected_op_id: int | None = None,
    op_id: int | None = None,
) -> OperationTransition:
    """Apply one undo/redo transition inside a caller-owned transaction."""

    cursor_before = project.op_cursor
    selective = direction in {"revert", "restore"}
    reverting = direction in {"undo", "revert"}
    target = operation_target(project, direction, op_id=op_id)
    if target is None:
        raise OperationUnavailable(direction)
    target_op_id = int(target["id"])
    if expected_op_id is not None and expected_op_id != target_op_id:
        raise OperationMismatch(expected_op_id, target_op_id)
    session_row = project.db.execute(
        "SELECT id FROM import_sessions WHERE op_id=? "
        "AND state IN ('active','paused','cancelled')",
        (target_op_id,),
    ).fetchone()
    if session_row is not None and reverting:
        from .import_sessions import ImportSessionStore
        from .streaming_import import StreamingSheetWriter

        session = ImportSessionStore(project).get(str(session_row["id"]))
        StreamingSheetWriter._from_session(project, session).prepare_removal(session)
        target = project.db.execute(
            "SELECT * FROM ops WHERE id=?", (target_op_id,)
        ).fetchone()
    if reverting and target["barrier"]:
        raise IrreversibleOperation(target)
    try:
        undo_info = json.loads(target["undo_info"] or "{}")
    except json.JSONDecodeError as exc:
        raise CorruptOperation(
            target_op_id, "Target operation undo_info is not valid JSON"
        ) from exc
    validation_error = validate_operation_undo_info(undo_info)
    if validation_error is not None:
        raise CorruptOperation(target_op_id, validation_error)
    from .purge import require_restore_available

    structural_scope = any(
        undo_info.get(field)
        for field in (
            "created_sheets",
            "deleted_sheets",
            "created_columns",
            "deleted_columns",
            "created_rows",
            "deleted_rows",
            "restored_rows",
            "created_rows_import_session_id",
            "replaced_rows_import_session_id",
        )
    )
    structural_scope = structural_scope or (
        project.db.execute(
            "SELECT 1 FROM structural_changes effect "
            "JOIN project_purge_targets target ON target.kind=effect.kind "
            "AND target.object_id=effect.object_id "
            "WHERE effect.op_id=? LIMIT 1",
            (target_op_id,),
        ).fetchone()
        is not None
    )
    if structural_scope:
        require_restore_available(project.db, op_ids=(target_op_id,))
    require_restore_available(
        project.db,
        sheet_ids=(
            undo_info.get("deleted_sheets", [])
            if reverting
            else undo_info.get("created_sheets", [])
        ),
        column_ids=(
            undo_info.get("deleted_columns", [])
            if reverting
            else undo_info.get("created_columns", [])
        ),
        row_ids=(
            undo_info.get("deleted_rows", [])
            if reverting
            else [
                *undo_info.get("created_rows", []),
                *undo_info.get("restored_rows", []),
            ]
        ),
    )
    from .structural_changes import record_structural_changes

    record_structural_changes(project.db, target_op_id, undo_info)
    _require_restoration_targets(project, target_op_id, undo_info, reverting=reverting)
    column_ids = operation_touched_column_ids(project, target_op_id, undo_info)
    claims = OutputColumnClaimStore(project)
    if not reverting:
        # A queued job can reserve a released name before creating its column
        # or appending an operation (and thus before discarding this redo).
        for column_id in undo_info.get("created_columns", ()):
            column = project.get_column(column_id)
            if column is not None:
                claim = claims.active_for_output_name(
                    sheet_id=int(column["sheet_id"]), output_name=str(column["name"])
                )
                if claim is not None:
                    raise ClaimedOperation(target_op_id, dict(claim))
    status_before = str(target["status"])
    if reverting:
        if undo_info.get("created_columns"):
            # A live column may be intentionally hidden. Undo changes lifecycle;
            # redo must restore its visibility, not expose internal outputs.
            undo_info["column_hidden_after"] = {
                str(column_id): bool(row["hidden"])
                for column_id in undo_info["created_columns"]
                if (row := project.get_column(column_id)) is not None
            }
            set_undo_info(project, target_op_id, undo_info, commit=False)
        from .changes import ChangeStore

        ChangeStore(project).revert(target_op_id)
        project.db.execute(
            "UPDATE ops SET redo_eligible=? WHERE id=?",
            (int(not selective), target_op_id),
        )
        project.db.execute(
            "UPDATE output_column_claims SET status='released',released_at=datetime('now') "
            "WHERE status='active' AND (op_id=? OR run_id IN (SELECT id FROM runs WHERE op_id=?))",
            (target_op_id, target_op_id),
        )
        try:
            project._unapply(target)
        except ImportSessionConflict as exc:
            raise CorruptOperation(target_op_id, str(exc)) from exc
        status_after = "undone"
        project.db.execute("UPDATE ops SET status='undone' WHERE id=?", (target_op_id,))
        cursor_after = int(
            project.db.execute(
                "SELECT COALESCE((SELECT id FROM ops WHERE change_order < ? "
                "AND status='applied' ORDER BY change_order DESC LIMIT 1),0)",
                (target["change_order"],),
            ).fetchone()[0]
        )
    else:
        from .changes import ChangeStore

        ChangeStore(project).restore(target_op_id)
        try:
            project._reapply(target)
        except ImportSessionConflict as exc:
            raise CorruptOperation(target_op_id, str(exc)) from exc
        status_after = "applied"
        project.db.execute("UPDATE ops SET redo_eligible=0 WHERE id=?", (target_op_id,))
        cursor_after = target_op_id
    if selective:
        cursor_after = cursor_before
    from .column_descriptors import project_column_descriptors

    project_column_descriptors(project.db, column_ids)
    from .structural_changes import project_structural_changes

    project_structural_changes(project.db, target_op_id)
    from .source_dependencies import stop_direct_consumers

    unavailable_sheets = [
        int(row[0])
        for row in project.db.execute(
            "SELECT s.id FROM structural_changes effect JOIN sheets s ON s.id=effect.object_id "
            "WHERE effect.op_id=? AND effect.kind='sheet' AND s.hidden=1",
            (target_op_id,),
        )
    ]
    unavailable_columns = [
        int(row[0])
        for row in project.db.execute(
            "SELECT c.id FROM structural_changes effect JOIN columns c ON c.id=effect.object_id "
            "WHERE effect.op_id=? AND effect.kind='column' AND c.active=0",
            (target_op_id,),
        )
    ]
    stop_direct_consumers(
        project, sheet_ids=unavailable_sheets, column_ids=unavailable_columns
    )
    from .review_judgments import project_judgments

    project_judgments(project, target_op_id)
    projection_column_ids = operation_projection_column_ids(
        project, target_op_id, undo_info
    )
    # Applicability is authoritative. Never scan/rewrite every result head in
    # the foreground: large scopes become dirty atomically and readers resolve
    # exact winners until bounded projection maintenance catches up.
    source_columns = {
        int(row[0])
        for row in project.db.execute(
            "SELECT DISTINCT cell.column_id FROM cells cell "
            "JOIN base_cell_producers producer ON producer.id=cell.producer_id "
            "WHERE producer.op_id=?",
            (target_op_id,),
        )
    }
    refresh_current_cells(
        project.db,
        column_ids=set(projection_column_ids)
        | source_columns
        | operation_membership_column_ids(project, undo_info),
    )
    edits = project.db.execute(
        "SELECT row_id,column_id FROM edits WHERE op_id=? LIMIT 513", (target_op_id,)
    ).fetchall()
    if len(edits) <= 512:
        refresh_current_cell_pairs(
            project.db, [(int(row[0]), int(row[1])) for row in edits]
        )
    else:
        refresh_current_cells(
            project.db,
            column_ids=[
                int(row[0])
                for row in project.db.execute(
                    "SELECT DISTINCT column_id FROM edits WHERE op_id=?",
                    (target_op_id,),
                )
            ],
        )
    project.db.execute(
        "UPDATE meta SET value=? WHERE key='op_cursor'", (str(cursor_after),)
    )
    return OperationTransition(
        target=target,
        undo_info=undo_info,
        column_ids=column_ids,
        direction=direction,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        status_before=status_before,  # type: ignore[arg-type]
        status_after=status_after,
        pending_review_count_unchanged=_plain_edit_preserves_pending_review_count(
            project, target, undo_info
        ),
    )


def undo(project: Any) -> int | None:
    """Step back one applied op. Returns the op id undone, or None."""
    try:
        project.db.execute("BEGIN IMMEDIATE")
        try:
            transition = step_operation(project, "undo")
        except OperationUnavailable:
            project.db.rollback()
            return None
        except IrreversibleOperation as exc:
            project.db.rollback()
            row = exc.args[0]
            raise ValueError(f"op {row['id']} ({row['kind']}) is irreversible")
        except ClaimedOperation as exc:
            project.db.rollback()
            claim = exc.claim
            raise ValueError(
                "output_column_busy: operation "
                f"{exc.op_id} "
                f"touches actively claimed column {claim['column_id']}"
            )
        except CorruptOperation as exc:
            project.db.rollback()
            raise ValueError(str(exc)) from exc
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise
    if not transition.pending_review_count_unchanged:
        project.refresh_pending_review_summary()
    return int(transition.target["id"])


def redo(project: Any) -> int | None:
    try:
        project.db.execute("BEGIN IMMEDIATE")
        try:
            transition = step_operation(project, "redo")
        except OperationUnavailable:
            project.db.rollback()
            return None
        except ClaimedOperation as exc:
            project.db.rollback()
            claim = exc.claim
            raise ValueError(
                "output_column_busy: operation "
                f"{exc.op_id} touches actively claimed column {claim['column_id']}"
            )
        except CorruptOperation as exc:
            project.db.rollback()
            raise ValueError(str(exc)) from exc
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise
    if not transition.pending_review_count_unchanged:
        project.refresh_pending_review_summary()
    return int(transition.target["id"])


def operation_touched_column_ids(
    project: Any, op_id: int, undo_info: dict[str, Any]
) -> frozenset[int]:
    column_ids = set(ResultGenerationStore(project).column_ids_for_op(op_id))
    column_ids.update(
        int(row[0])
        for row in project.db.execute(
            "SELECT DISTINCT column_id FROM copied_result_outcomes WHERE op_id=?",
            (op_id,),
        )
    )
    for sheet_id in [
        *undo_info.get("created_sheets", ()),
        *undo_info.get("deleted_sheets", ()),
    ]:
        column_ids.update(
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=?", (sheet_id,)
            ).fetchall()
        )
    for field_name in (
        "created_columns",
        "column_pointers",
        "column_pointers_after",
        "column_formats",
        "column_formats_after",
        "column_types",
        "column_types_after",
        "column_semantic_types",
        "column_semantic_types_after",
    ):
        value = undo_info.get(field_name)
        if isinstance(value, dict):
            column_ids.update(int(column_id) for column_id in value)
        elif isinstance(value, list):
            column_ids.update(int(column_id) for column_id in value)
    for field_name in (
        "review_states",
        "review_states_after",
        "review_metadata",
        "review_metadata_after",
    ):
        value = undo_info.get(field_name)
        if not isinstance(value, dict):
            continue
        for target_key in value:
            parts = str(target_key).split(":")
            if _is_review_state_key(str(target_key)):
                column_ids.add(int(parts[-1]))
    column_ids.update(
        int(row["column_id"])
        for row in project.db.execute(
            "SELECT DISTINCT column_id FROM edits WHERE op_id=?", (op_id,)
        ).fetchall()
    )
    column_ids.update(operation_membership_column_ids(project, undo_info))
    return frozenset(column_ids)


def operation_membership_column_ids(project: Any, info: Mapping[str, Any]) -> set[int]:
    columns = {
        int(row[0])
        for field in ("created_rows", "deleted_rows", "restored_rows")
        for row in project.db.execute(
            "SELECT DISTINCT col.id FROM json_each(?) ids "
            "JOIN rows row ON row.id=ids.value JOIN columns col ON col.sheet_id=row.sheet_id",
            (json.dumps(info.get(field, [])),),
        )
    }

    for field in ("created_rows_import_session_id", "replaced_rows_import_session_id"):
        if info.get(field) is not None:
            columns.update(
                int(row[0])
                for row in project.db.execute(
                    "SELECT col.id FROM import_sessions session JOIN columns col ON col.sheet_id=session.sheet_id WHERE session.id=?",
                    (info[field],),
                )
            )
    return columns


def operation_projection_column_ids(
    project: Any, op_id: int, undo_info: dict[str, Any]
) -> frozenset[int]:
    """Columns whose visible value layer can change when an op changes status."""

    column_ids = set(ResultGenerationStore(project).column_ids_for_op(op_id))
    column_ids.update(
        int(row[0])
        for row in project.db.execute(
            "SELECT DISTINCT column_id FROM copied_result_outcomes WHERE op_id=?",
            (op_id,),
        )
    )
    for sheet_id in [
        *undo_info.get("created_sheets", ()),
        *undo_info.get("deleted_sheets", ()),
    ]:
        column_ids.update(
            int(row["id"])
            for row in project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=?", (sheet_id,)
            ).fetchall()
        )
    for field_name in (
        "created_columns",
        "column_pointers",
        "column_pointers_after",
        "column_formats",
        "column_formats_after",
        "column_types",
        "column_types_after",
        "column_semantic_types",
        "column_semantic_types_after",
    ):
        value = undo_info.get(field_name)
        if isinstance(value, dict):
            column_ids.update(int(column_id) for column_id in value)
        elif isinstance(value, list):
            column_ids.update(int(column_id) for column_id in value)
    return frozenset(column_ids)


def validate_operation_undo_info(undo_info: Any) -> str | None:
    """Validate the persisted fields interpreted by undo/reapply."""

    if not isinstance(undo_info, dict):
        return "Target operation undo_info must be a JSON object"
    for field_name in (
        "created_sheets",
        "created_columns",
        "created_rows",
        "deleted_rows",
        "restored_rows",
        "deleted_columns",
        "deleted_sheets",
    ):
        value = undo_info.get(field_name)
        if value is not None and (
            not isinstance(value, list)
            or not all(_is_positive_int_id(item) for item in value)
        ):
            return f"Target operation undo_info.{field_name} must be a list of ids"
    for field_name in ("column_pointers", "column_pointers_after"):
        value = undo_info.get(field_name)
        if value is None:
            continue
        if not isinstance(value, dict):
            return f"Target operation undo_info.{field_name} must be an object"
        for column_id, run_id in value.items():
            if not _is_intish_id(column_id) or (
                run_id is not None and not _is_positive_int_id(run_id)
            ):
                return (
                    f"Target operation undo_info.{field_name} must map column ids "
                    "to run ids or null"
                )
    visibility = undo_info.get("column_hidden_after")
    if visibility is not None and (
        not isinstance(visibility, dict)
        or not all(
            _is_intish_id(key) and isinstance(value, bool)
            for key, value in visibility.items()
        )
    ):
        return "Target operation undo_info.column_hidden_after must map column ids to booleans"
    for field_name in ("review_metadata", "review_metadata_after"):
        value = undo_info.get(field_name)
        if value is None:
            continue
        if not isinstance(value, dict) or not all(
            _is_review_state_key(target_key) and _valid_review_metadata(metadata)
            for target_key, metadata in value.items()
        ):
            return (
                f"Target operation undo_info.{field_name} must map result cell "
                "keys to decision/note objects"
            )
    for field_name in ("column_formats", "column_formats_after"):
        value = undo_info.get(field_name)
        if value is not None and (
            not isinstance(value, dict)
            or not all(
                _is_intish_id(column_id)
                and (format_value is None or isinstance(format_value, str))
                for column_id, format_value in value.items()
            )
        ):
            return (
                f"Target operation undo_info.{field_name} must map column ids "
                "to format strings or null"
            )
    for field_name in ("column_types", "column_types_after"):
        value = undo_info.get(field_name)
        if value is not None and (
            not isinstance(value, dict)
            or not all(
                _is_intish_id(column_id) and isinstance(type_value, str)
                for column_id, type_value in value.items()
            )
        ):
            return (
                f"Target operation undo_info.{field_name} must map column ids "
                "to type strings"
            )
    for field_name in ("column_semantic_types", "column_semantic_types_after"):
        value = undo_info.get(field_name)
        if value is not None and (
            not isinstance(value, dict)
            or not all(
                _is_intish_id(column_id)
                and (semantic_type is None or isinstance(semantic_type, str))
                for column_id, semantic_type in value.items()
            )
        ):
            return (
                f"Target operation undo_info.{field_name} must map column ids "
                "to semantic type strings or null"
            )
    for field_name in ("review_states", "review_states_after"):
        value = undo_info.get(field_name)
        if value is None:
            continue
        if not isinstance(value, dict):
            return f"Target operation undo_info.{field_name} must be an object"
        for target_key, review_state in value.items():
            if not _is_review_state_key(target_key) or review_state not in {
                "unreviewed",
                "verified",
                "rejected",
            }:
                return (
                    f"Target operation undo_info.{field_name} must map "
                    "result cell keys to review states"
                )
    evidence_links_staled = undo_info.get("evidence_links_staled")
    if evidence_links_staled is not None and (
        not isinstance(evidence_links_staled, list)
        or not all(
            isinstance(item, dict)
            and set(item) == {"stable_id", "stale_reason"}
            and isinstance(item["stable_id"], str)
            and bool(item["stable_id"])
            and isinstance(item["stale_reason"], str)
            and bool(item["stale_reason"])
            for item in evidence_links_staled
        )
    ):
        return (
            "Target operation undo_info.evidence_links_staled must be a list of "
            "stable-id/reason objects"
        )
    for field in ("created_rows_import_session_id", "replaced_rows_import_session_id"):
        import_session_id = undo_info.get(field)
        if import_session_id is not None and (
            not isinstance(import_session_id, str) or not import_session_id
        ):
            return f"Target operation undo_info.{field} must be a non-empty string"
    return None


def operation_affected_refs(undo_info: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for sheet_id in undo_info.get("created_sheets", []):
        refs.append({"kind": "sheet", "sheet_id": int(sheet_id)})
    for column_id in dict.fromkeys(
        [*undo_info.get("created_columns", []), *undo_info.get("released_columns", [])]
    ):
        refs.append({"kind": "column", "column_id": int(column_id)})
    row_ids = [int(row_id) for row_id in undo_info.get("created_rows", [])]
    if row_ids:
        refs.append({"kind": "rows", "row_ids": row_ids})
    deleted_row_ids = [int(row_id) for row_id in undo_info.get("deleted_rows", [])]
    if deleted_row_ids:
        refs.append({"kind": "rows", "row_ids": deleted_row_ids})
    for field, membership in (
        ("created_rows_import_session_id", "created"),
        ("replaced_rows_import_session_id", "replaced"),
    ):
        if undo_info.get(field):
            refs.append(
                {
                    "kind": "import_session_rows",
                    "session_id": undo_info[field],
                    "membership": membership,
                }
            )
    pointer_after = undo_info.get("column_pointers_after") or {}
    for column_id, before_run_id in undo_info.get("column_pointers", {}).items():
        refs.append(
            {
                "kind": "column_pointer",
                "column_id": int(column_id),
                "before_run_id": before_run_id,
                "after_run_id": pointer_after.get(str(column_id)),
            }
        )
    state_after = undo_info.get("review_states_after") or {}
    for key, before_state in undo_info.get("review_states", {}).items():
        from .review_judgments import parse_result_key

        kind, owner, row_id, column_id = parse_result_key(str(key))
        refs.append(
            {
                "kind": "review_state",
                "run_id" if kind == "run" else "copied_op_id": owner,
                "row_id": row_id,
                "column_id": column_id,
                "before": before_state,
                "after": state_after.get(key),
            }
        )
    return refs


def _is_positive_int_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_intish_id(value: Any) -> bool:
    return _is_positive_int_id(value) or (
        isinstance(value, str) and value.isdecimal() and int(value) > 0
    )


def _is_review_state_key(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    from .review_judgments import parse_result_key

    try:
        parse_result_key(value)
        return True
    except ValueError:
        return False


def _unapply(project: Any, op: sqlite3.Row) -> None:
    """Restore pointer state captured in undo_info. Never deletes data."""
    info = json.loads(op["undo_info"] or "{}")
    if op["kind"] == "pdf.split":
        invalidate_pdf_split_generation(project.db, op_id=int(op["id"]))
    for col_id, prev_run in info.get("column_pointers", {}).items():
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?",
            (prev_run, int(col_id)),
        )
    import_session_id = info.get("created_rows_import_session_id")
    if import_session_id is not None:
        set_import_session_rows_hidden(
            project.db,
            op_id=int(op["id"]),
            session_id=import_session_id,
            hidden=True,
        )
    if info.get("replaced_rows_import_session_id") is not None:
        set_import_session_replaced_rows_hidden(
            project.db,
            op_id=int(op["id"]),
            session_id=info["replaced_rows_import_session_id"],
            hidden=False,
        )
    for transition in info.get("evidence_links_staled", []):
        project.db.execute(
            "UPDATE evidence_links SET status='active',stale_reason=NULL,stale_at=NULL "
            "WHERE stable_id=? AND status='stale' AND stale_reason=?",
            (transition["stable_id"], transition["stale_reason"]),
        )


def _reapply(project: Any, op: sqlite3.Row) -> None:
    info = json.loads(op["undo_info"] or "{}")
    for col_id, runs in info.get("column_pointers_after", {}).items():
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?", (runs, int(col_id))
        )
    zero_success_columns = set(info.get("zero_success_columns", []))
    for col_id in info.get("created_columns", []):
        hidden = col_id in zero_success_columns or info.get(
            "column_hidden_after", {}
        ).get(str(col_id), False)
        project.db.execute(
            "UPDATE columns SET active=1,hidden=? WHERE id=?", (int(hidden), col_id)
        )
    import_session_id = info.get("created_rows_import_session_id")
    if import_session_id is not None:
        set_import_session_rows_hidden(
            project.db,
            op_id=int(op["id"]),
            session_id=import_session_id,
            hidden=False,
        )
    if info.get("replaced_rows_import_session_id") is not None:
        set_import_session_replaced_rows_hidden(
            project.db,
            op_id=int(op["id"]),
            session_id=info["replaced_rows_import_session_id"],
            hidden=True,
        )
    for transition in info.get("evidence_links_staled", []):
        project.db.execute(
            "UPDATE evidence_links SET status='stale',stale_reason=?,"
            "stale_at=datetime('now') WHERE stable_id=? AND status='active'",
            (transition["stale_reason"], transition["stable_id"]),
        )


def ops_meta(project: Any, op_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Batch {op_id: {"kind","label"}} for parent-op provenance labels.

    Store-owned so the HTTP projection layer stays SQL-free (Workbench IA
    inc 7: derived-sheet "how it's made")."""
    if not op_ids:
        return {}
    placeholders = ",".join("?" * len(op_ids))
    rows = project.db.execute(
        f"SELECT id, kind, label FROM ops WHERE id IN ({placeholders})",
        tuple(op_ids),
    ).fetchall()
    return {int(r["id"]): {"kind": r["kind"], "label": r["label"]} for r in rows}
