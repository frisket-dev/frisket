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
from frisket.engine.store.import_sessions import active_import_session, ImportInProgress
from frisket.engine.store.current_cells import refresh_current_cell_pairs
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore


def _parse_review_state_key(key: object) -> tuple[int, int, int]:
    parts = str(key).split(":")
    if len(parts) != 3:
        raise ValueError(f"invalid review state key: {key!r}")
    run_id, row_id, column_id = (int(part) for part in parts)
    if run_id <= 0 or row_id <= 0 or column_id <= 0:
        raise ValueError(f"invalid review state key: {key!r}")
    return run_id, row_id, column_id


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
    """Append an op, discarding the undone redo branch."""
    # appending while ops are undone discards that redo branch forever
    # (otherwise a later undo could resurrect it out of order)
    project.db.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
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
    project.db.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
    if commit:
        project.db.commit()
    return op_id


def set_undo_info(
    project: Any, op_id: int, undo_info: dict[str, Any], *, commit: bool = True
) -> None:
    project.db.execute(
        "UPDATE ops SET undo_info=? WHERE id=?", (json.dumps(undo_info), op_id)
    )
    if commit:
        project.db.commit()


def history(project: Any) -> list[sqlite3.Row]:
    return project.db.execute("SELECT * FROM ops ORDER BY id").fetchall()


def history_page(project: Any, offset: int, limit: int) -> list[sqlite3.Row]:
    return project.db.execute(
        "SELECT * FROM ops ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def history_total(project: Any) -> int:
    return int(project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] or 0)


def history_cursor_index(project: Any) -> int:
    cursor = project.op_cursor
    if cursor <= 0:
        return -1
    count = project.db.execute(
        "SELECT COUNT(*) FROM ops WHERE id <= ?",
        (cursor,),
    ).fetchone()[0]
    return int(count or 0) - 1


@dataclass(frozen=True)
class OperationTransition:
    target: sqlite3.Row
    undo_info: dict[str, Any]
    column_ids: frozenset[int]
    direction: Literal["undo", "redo"]
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
    direction: Literal["undo", "redo"],
    *,
    expected_op_id: int | None = None,
) -> OperationTransition:
    """Apply one undo/redo transition inside a caller-owned transaction."""

    cursor_before = project.op_cursor
    if direction == "undo":
        target = project.db.execute(
            "SELECT * FROM ops WHERE id <= ? AND status='applied' "
            "ORDER BY id DESC LIMIT 1",
            (cursor_before,),
        ).fetchone()
    else:
        target = project.db.execute(
            "SELECT * FROM ops WHERE id > ? AND status='undone' ORDER BY id LIMIT 1",
            (cursor_before,),
        ).fetchone()
    if target is None:
        raise OperationUnavailable(direction)
    target_op_id = int(target["id"])
    if expected_op_id is not None and expected_op_id != target_op_id:
        raise OperationMismatch(expected_op_id, target_op_id)
    session = project.db.execute(
        "SELECT sheet_id FROM import_sessions WHERE op_id=? "
        "AND state IN ('active','paused','cancelled')",
        (target_op_id,),
    ).fetchone()
    if session is not None:
        active = active_import_session(project.db, int(session["sheet_id"]))
        if active is not None:
            raise ImportInProgress(active)
    if direction == "undo" and target["barrier"]:
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
    column_ids = operation_touched_column_ids(project, target_op_id, undo_info)
    claims = OutputColumnClaimStore(project)
    claim = claims.active_for_columns(column_ids)
    if claim is not None:
        raise ClaimedOperation(target_op_id, dict(claim))
    if direction == "redo":
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
    if direction == "undo":
        if undo_info.get("created_columns"):
            # A live column may be intentionally hidden. Undo changes lifecycle;
            # redo must restore its visibility, not expose internal outputs.
            undo_info["column_hidden_after"] = {
                str(column_id): bool(row["hidden"])
                for column_id in undo_info["created_columns"]
                if (row := project.get_column(column_id)) is not None
            }
            set_undo_info(project, target_op_id, undo_info, commit=False)
        project._unapply(target)
        status_after = "undone"
        project.db.execute("UPDATE ops SET status='undone' WHERE id=?", (target_op_id,))
        cursor_after = int(
            project.db.execute(
                "SELECT COALESCE(MAX(id),0) FROM ops WHERE id < ? AND status='applied'",
                (target_op_id,),
            ).fetchone()[0]
        )
    else:
        project._reapply(target)
        status_after = "applied"
        project.db.execute(
            "UPDATE ops SET status='applied' WHERE id=?", (target_op_id,)
        )
        cursor_after = target_op_id
    projection_column_ids = operation_projection_column_ids(
        project, target_op_id, undo_info
    )
    descriptor_replacement = any(
        undo_info.get(field_name)
        for field_name in (
            "column_formats",
            "column_formats_after",
            "column_types",
            "column_types_after",
            "column_semantic_types",
            "column_semantic_types_after",
        )
    )
    generations = ResultGenerationStore(project)
    with generations.affected_key_table(
        target_op_id, projection_column_ids
    ) as affected_key_table:
        if descriptor_replacement or generations.key_table_covers_columns(
            affected_key_table, projection_column_ids
        ):
            # Complete coverage keeps the proven bulk projector. Descriptor
            # changes require it because validity and edit-boundary precedence
            # can change at coordinates absent from the run.
            generations.rebuild_heads(projection_column_ids, commit=False)
        else:
            generations.rebuild_heads_from_key_table(affected_key_table, commit=False)
    refresh_current_cell_pairs(
        project.db,
        [
            (int(row["row_id"]), int(row["column_id"]))
            for row in project.db.execute(
                "SELECT row_id,column_id FROM edits WHERE op_id=?", (target_op_id,)
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
    for sheet_id in undo_info.get("created_sheets") or ():
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
            if len(parts) == 3 and parts[2].isdecimal() and int(parts[2]) > 0:
                column_ids.add(int(parts[2]))
    column_ids.update(
        int(row["column_id"])
        for row in project.db.execute(
            "SELECT DISTINCT column_id FROM edits WHERE op_id=?", (op_id,)
        ).fetchall()
    )
    return frozenset(column_ids)


def operation_projection_column_ids(
    project: Any, op_id: int, undo_info: dict[str, Any]
) -> frozenset[int]:
    """Columns whose visible value layer can change when an op changes status."""

    column_ids = set(ResultGenerationStore(project).column_ids_for_op(op_id))
    for sheet_id in undo_info.get("created_sheets") or ():
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
    return None


def operation_affected_refs(undo_info: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for sheet_id in undo_info.get("created_sheets", []):
        refs.append({"kind": "sheet", "sheet_id": int(sheet_id)})
    for column_id in undo_info.get("created_columns", []):
        refs.append({"kind": "column", "column_id": int(column_id)})
    row_ids = [int(row_id) for row_id in undo_info.get("created_rows", [])]
    if row_ids:
        refs.append({"kind": "rows", "row_ids": row_ids})
    deleted_row_ids = [int(row_id) for row_id in undo_info.get("deleted_rows", [])]
    if deleted_row_ids:
        refs.append({"kind": "rows", "row_ids": deleted_row_ids})
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
        run_id, row_id, column_id = (int(part) for part in str(key).split(":"))
        refs.append(
            {
                "kind": "review_state",
                "run_id": run_id,
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
    parts = value.split(":")
    return len(parts) == 3 and all(_is_intish_id(part) for part in parts)


def _unapply(project: Any, op: sqlite3.Row) -> None:
    """Restore pointer state captured in undo_info. Never deletes data."""
    info = json.loads(op["undo_info"] or "{}")
    for col_id, prev_run in info.get("column_pointers", {}).items():
        project.db.execute(
            "UPDATE columns SET current_run_id=? WHERE id=?",
            (prev_run, int(col_id)),
        )
    for col_id in info.get("created_columns", []):
        project.db.execute("UPDATE columns SET active=0,hidden=1 WHERE id=?", (col_id,))
    for col_id, prev_fmt in info.get("column_formats", {}).items():
        project.db.execute(
            "UPDATE columns SET format=? WHERE id=?", (prev_fmt, int(col_id))
        )
    for col_id, prev_type in info.get("column_types", {}).items():
        project.db.execute(
            "UPDATE columns SET type=? WHERE id=?", (prev_type, int(col_id))
        )
    for col_id, prev_semantic_type in info.get("column_semantic_types", {}).items():
        project.db.execute(
            "UPDATE columns SET semantic_type=? WHERE id=?",
            (prev_semantic_type, int(col_id)),
        )
    for sheet_id in info.get("created_sheets", []):
        # hide derived sheets on undo; tombstone, never delete
        project.db.execute(
            "UPDATE sheets SET hidden=1, name = name || ' (undone:' || id || ')' "
            "WHERE id=? AND hidden=0 AND name NOT LIKE '%(undone:%'",
            (sheet_id,),
        )
    for row_id in info.get("created_rows", []):
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_id,))
    for row_id in info.get("deleted_rows", []):
        # undo of a row delete restores the soft-deleted row
        project.db.execute("UPDATE rows SET hidden=0 WHERE id=?", (row_id,))
    for key, previous_state in info.get("review_states", {}).items():
        run_id, row_id, column_id = _parse_review_state_key(key)
        updated = RunResultStore(project).set_result_review_state(
            run_id, row_id, column_id, previous_state, commit=False
        )
        if updated != 1:
            raise ValueError(f"review state target not found: {key!r}")
    for key, previous in info.get("review_metadata", {}).items():
        run_id, row_id, column_id = _parse_review_state_key(key)
        updated = RunResultStore(project).set_result_review_metadata(
            run_id,
            row_id,
            column_id,
            previous.get("decision"),
            previous.get("note"),
            commit=False,
        )
        if updated != 1:
            raise ValueError(f"review metadata target not found: {key!r}")
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
    for col_id, fmt in info.get("column_formats_after", {}).items():
        project.db.execute("UPDATE columns SET format=? WHERE id=?", (fmt, int(col_id)))
    for col_id, t in info.get("column_types_after", {}).items():
        project.db.execute("UPDATE columns SET type=? WHERE id=?", (t, int(col_id)))
    for col_id, semantic_type in info.get("column_semantic_types_after", {}).items():
        project.db.execute(
            "UPDATE columns SET semantic_type=? WHERE id=?",
            (semantic_type, int(col_id)),
        )
    for sheet_id in info.get("created_sheets", []):
        project.db.execute(
            "UPDATE sheets SET hidden=0, name = replace(name, ' (undone:' || id || ')', '') WHERE id=?",
            (sheet_id,),
        )
    for row_id in info.get("created_rows", []):
        project.db.execute("UPDATE rows SET hidden=0 WHERE id=?", (row_id,))
    for row_id in info.get("deleted_rows", []):
        # redo of a row delete re-hides the row
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_id,))
    for key, next_state in info.get("review_states_after", {}).items():
        run_id, row_id, column_id = _parse_review_state_key(key)
        updated = RunResultStore(project).set_result_review_state(
            run_id, row_id, column_id, next_state, commit=False
        )
        if updated != 1:
            raise ValueError(f"review state target not found: {key!r}")
    for key, next_value in info.get("review_metadata_after", {}).items():
        run_id, row_id, column_id = _parse_review_state_key(key)
        updated = RunResultStore(project).set_result_review_metadata(
            run_id,
            row_id,
            column_id,
            next_value.get("decision"),
            next_value.get("note"),
            commit=False,
        )
        if updated != 1:
            raise ValueError(f"review metadata target not found: {key!r}")
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
