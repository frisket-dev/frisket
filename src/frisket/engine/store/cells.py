"""Sheet/column/row store and cell value resolution for a project bundle.

Normal reads use the transactionally maintained current-cell projection.
Explicit candidate reads omit manual edits, resolving exact result heads
over source cells.

Also hosts the replay preserve+surface derivation (hand-edited ai_generated
cells whose current-run value differs) and the column type/format registries.
Free functions over the facade's per-thread SQLite connection; ``project``
stays duck-typed (``Any``) so this leaf never re-imports the facade module."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from typing import Any

from frisket.authoring import column_types
from frisket.engine.store.cell_writes import (
    BaseCellWrite,
    EditCellWrite,
    create_base_cell_producer,
    initialize_base_cells,
    insert_edits,
)
from frisket.engine.store.result_generations import ResultGenerationStore

# Column types live in the pluggable registry (frisket.column_types) — the
# core types are registered there on the same seam plugins use. COLUMN_TYPES
# stays exported as a LIVE view over the registry for backward compatibility
# (`t in COLUMN_TYPES` now also matches plugin-registered types).
COLUMN_TYPES = column_types.TypeNamesView()
_SQLITE_ID_CHUNK_SIZE = 900


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _decode_stored_value(encoded: str | None, *, tolerate_errors: bool) -> Any:
    """Decode one stored source/edit value, optionally failing closed to null."""
    if encoded is None:
        return None
    if not tolerate_errors:
        return json.loads(encoded)
    try:
        decoded = json.loads(encoded, parse_constant=_reject_non_json_constant)
        # ``1e400`` becomes infinity without invoking parse_constant, while a
        # deeply nested value can decode but remain unsafe to re-encode.
        json.dumps(decoded, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return decoded


class MixedOriginReplayUnsupported(ValueError):
    """A column-level replay surface cannot represent mixed exact heads."""

    code = "mixed_origin_replay_unsupported"

    def __init__(self, column_id: int):
        self.column_id = int(column_id)
        super().__init__(
            "replay pending values are unavailable for mixed-origin column "
            f"{self.column_id}"
        )


def _managed_replay_origin_run_id(
    project: Any, column_id: int
) -> tuple[bool, int | None]:
    """Return the sole exact-head origin for a managed replay column.

    A generated column with no heads has no replay source; multiple origins
    cannot fit replay's column-level provenance contract.
    """

    generation_store = ResultGenerationStore(project)
    if not generation_store.is_generation_managed(column_id):
        return False, None
    origins = generation_store.origin_run_ids(column_id, limit=2)
    if len(origins) > 1:
        raise MixedOriginReplayUnsupported(column_id)
    return True, origins[0] if origins else None


def _replay_source(
    project: Any, sheet_id: int, column_id: int
) -> tuple[bool, int | None]:
    column = project.db.execute(
        "SELECT c.current_run_id FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        "WHERE c.id=? AND c.sheet_id=? AND c.ai_generated=1 "
        "AND c.hidden=0 AND s.hidden=0",
        (column_id, sheet_id),
    ).fetchone()
    if column is None:
        raise ValueError("replay target is not a visible generated column")
    managed, origin = _managed_replay_origin_run_id(project, column_id)
    current = origin if managed else column["current_run_id"]
    return managed, int(current) if current is not None else None


def replay_origin_run_id(project: Any, sheet_id: int, column_id: int) -> int | None:
    """Return the sole current origin for a visible generated column."""

    return _replay_source(project, sheet_id, column_id)[1]


def replay_generated_snapshot(
    project: Any, sheet_id: int, row_id: int, column_id: int
) -> dict[str, Any]:
    """Read the exact current generated value and its stable identity."""

    managed, run_id = _replay_source(project, sheet_id, column_id)
    if run_id is None:
        return {"run_id": None}
    head_join = (
        "JOIN cell_result_heads head ON head.column_id=res.column_id "
        "AND head.row_id=res.row_id AND head.run_id=res.run_id "
        if managed
        else ""
    )
    row = project.db.execute(
        "SELECT res.value FROM results res "
        + head_join
        + "JOIN rows r ON r.id=res.row_id AND r.sheet_id=? AND r.hidden=0 "
        "WHERE res.run_id=? AND res.row_id=? AND res.column_id=? "
        "AND res.error IS NULL AND res.value IS NOT NULL",
        (sheet_id, run_id, row_id, column_id),
    ).fetchone()
    if row is None:
        return {"run_id": run_id}
    value = json.loads(row["value"])
    return {
        "run_id": run_id,
        "value": value,
        "generated_value_hash": replay_generated_value_hash(value),
    }


# Display-format hints a column can carry (columns.format). The format is a
# presentation contract for the frontend, orthogonal to the storage type:
# a 'text' column with format='markdown' renders markdown in the grid preview
# and the row drawer; an 'integer' with format='filesize' renders "1.2 MB".
COLUMN_FORMATS = {
    "markdown",
    "filesize",
    "currency",
    "percent",
}


def replay_generated_value_hash(value: Any) -> str:
    """Canonical value-identity of a fresh generated cell value
    (replay-accept-surface-v1). The durable-dismissal key and the accept op's
    spec both use this so an identical future value never re-nags. Lives in the
    store layer (not frisket.sdk) so Project can call it without an upward
    dependency."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def add_sheet(
    project: Any,
    name: str,
    parent_sheet_id: int | None = None,
    parent_op_id: int | None = None,
    *,
    commit: bool = True,
) -> int:
    cur = project.db.execute(
        "INSERT INTO sheets (name, position, parent_sheet_id, parent_op_id) "
        "VALUES (?, (SELECT COALESCE(MAX(position),0)+1 FROM sheets), ?, ?)",
        (name, parent_sheet_id, parent_op_id),
    )
    if commit:
        project.db.commit()
    return cur.lastrowid


def sheets(project: Any, include_hidden: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM sheets"
    if not include_hidden:
        q += " WHERE hidden=0"
    return project.db.execute(q + " ORDER BY position").fetchall()


def set_sheet_title_column(
    project: Any, sheet_id: int, title_column_id: int | None
) -> None:
    """Set (or clear, with None) the sheet-level row-title override
    (sheet-title-column-v1). The '...' column menu's "Use as row title"
    is the setter; clearing restores the frontend's default (first
    column per the grid's current drag order, falling back to canonical
    order) — that default is computed client-side, so this store only
    persists the explicit override itself."""
    sheet_row = project.db.execute(
        "SELECT id FROM sheets WHERE id=?", (sheet_id,)
    ).fetchone()
    if sheet_row is None:
        raise KeyError(f"no sheet {sheet_id}")
    if title_column_id is not None:
        col = project.db.execute(
            "SELECT id FROM columns WHERE id=? AND sheet_id=?",
            (title_column_id, sheet_id),
        ).fetchone()
        if col is None:
            raise ValueError(f"column {title_column_id} is not in sheet {sheet_id}")
    project.db.execute(
        "UPDATE sheets SET title_column_id=? WHERE id=?",
        (title_column_id, sheet_id),
    )
    project.db.commit()


def add_column(
    project: Any,
    sheet_id: int,
    name: str,
    type: str = "text",
    ai_generated: bool = False,
    format: str | None = None,
    hidden: bool = False,
    default_hidden: bool = False,
    semantic_type: str | None = None,
    *,
    commit: bool = True,
) -> int:
    """Add a project column.

    ``hidden`` is a storage/lifecycle flag: normal readers do not see the
    column. ``default_hidden`` is presentation-only: every data API still
    exposes the column, while clients may initially collapse it. Actions
    that produce useful-but-secondary fields should use ``default_hidden``.

    ``semantic_type`` is an explicit marker for columns whose contents follow
    a named contract (e.g. ``'entity_mentions'``, written by map.ner). It is
    NEVER inferred from cell contents — callers declare it or it stays NULL.
    """
    if type not in COLUMN_TYPES:
        raise ValueError(f"unknown column type: {type}")
    # a column hidden by undo blocks its name; re-creating it revives it
    # (its old runs remain in history; the new run will repoint it). Intentionally
    # hidden output columns (e.g. map.python's __result_/__evidence_ plumbing) pass
    # hidden=True so a revive keeps them hidden instead of forcing them visible.
    existing_hidden = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=1",
        (sheet_id, name),
    ).fetchone()
    if existing_hidden:
        _validate_existing_column_values_for_type(
            project, int(existing_hidden["id"]), type
        )
        # semantic_type converges with the rest: a revived column that keeps a
        # marker its new producer never declared would make the marker a lie
        # (a prose column advertising entity_mentions), and one that loses a
        # marker its producer DID declare reads as "no entity column" —
        # indistinguishable from clean data.
        #
        # The pointer clear is LEGACY-ONLY. For a generation-managed column
        # the scalar pointer is a compatibility mirror, not read authority
        # (exact heads are), so clearing it suppresses nothing — and the
        # successor generation's seal fence compares the pointer against the
        # base captured at claim time, so a mid-run clear would refuse the
        # very publication that replaces the old values. Revival therefore
        # preserves the pointer wherever it stands (NULL after undo-revival,
        # the prior run after a zero-success hide); terminal seal moves it
        # atomically.
        generation_managed = (
            project.db.execute(
                "SELECT 1 FROM run_output_generations WHERE column_id=? LIMIT 1",
                (existing_hidden["id"],),
            ).fetchone()
            is not None
        )
        pointer_sql = "" if generation_managed else ", current_run_id=NULL"
        project.db.execute(
            "UPDATE columns SET hidden=?, default_hidden=?, type=?, ai_generated=?, "
            f"format=?, semantic_type=?{pointer_sql} WHERE id=?",
            (
                int(hidden),
                int(default_hidden),
                type,
                int(ai_generated),
                format,
                semantic_type,
                existing_hidden["id"],
            ),
        )
        if commit:
            project.db.commit()
        return existing_hidden["id"]
    cur = project.db.execute(
        "INSERT INTO columns (sheet_id, name, type, ai_generated, format, "
        "hidden, default_hidden, semantic_type, position) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, "
        "(SELECT COALESCE(MAX(position),0)+1 FROM columns WHERE sheet_id=?))",
        (
            sheet_id,
            name,
            type,
            int(ai_generated),
            format,
            int(hidden),
            int(default_hidden),
            semantic_type,
            sheet_id,
        ),
    )
    if commit:
        project.db.commit()
    return cur.lastrowid


def columns(
    project: Any, sheet_id: int, include_hidden: bool = False
) -> list[sqlite3.Row]:
    q = "SELECT * FROM columns WHERE sheet_id=?"
    if not include_hidden:
        q += " AND hidden=0"
    return project.db.execute(q + " ORDER BY position", (sheet_id,)).fetchall()


def get_column(project: Any, column_id: int) -> sqlite3.Row | None:
    return project.db.execute(
        "SELECT * FROM columns WHERE id=?", (column_id,)
    ).fetchone()


def set_column_default_hidden(
    project: Any, column_id: int, hidden: bool = True, *, commit: bool = True
) -> None:
    """Mark a column hide-by-default (presentation-only; still API-exposed).

    Used to RETIRE a support column an action no longer produces on re-run
    instead of stranding stale values — e.g. an old
    clean_column run's `_confidence`/`_justification` columns after those
    outputs were retired, or transcribe's detected_language on a non-detecting engine.
    This is the hide primitive the shared output-retirement mechanism
    (`MapRunner._retire_dropped_output_columns`) calls; recipes declare their
    dropped-role names via `Recipe.retired_output_names`."""
    cur = project.db.execute(
        "UPDATE columns SET default_hidden=? WHERE id=?",
        (int(hidden), column_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"no column {column_id}")
    if commit:
        project.db.commit()


def set_column_type(
    project: Any, column_id: int, type: str, *, commit: bool = True
) -> None:
    """Retype a column onto any REGISTERED type (core or plugin)."""
    if not column_types.is_registered(type):
        raise ValueError(f"unknown column type: {type}")
    _validate_existing_column_values_for_type(project, column_id, type)
    cur = project.db.execute("UPDATE columns SET type=? WHERE id=?", (type, column_id))
    if cur.rowcount == 0:
        raise KeyError(f"no column {column_id}")
    if commit:
        project.db.commit()


def _validate_existing_column_values_for_type(
    project: Any, column_id: int, type: str
) -> None:
    """Validate retained values before any existing-column type mutation."""
    if type != "integer":
        return
    column = project.db.execute(
        "SELECT sheet_id FROM columns WHERE id=?", (column_id,)
    ).fetchone()
    if column is None:
        raise KeyError(f"no column {column_id}")
    invalid = [
        row_id
        for row_id, value in project.get_values(
            int(column["sheet_id"]), column_id
        ).items()
        if not column_types.validate_value("integer", value)
    ]
    if invalid:
        raise ValueError(
            "existing values are invalid integer data: expected signed 64-bit integers"
        )


def set_column_format(
    project: Any,
    column_id: int,
    format: str | None,
    *,
    commit: bool = True,
) -> None:
    """Set (or clear, with None) a column's display-format hint."""
    if format is not None and format not in COLUMN_FORMATS:
        raise ValueError(f"unknown column format: {format}")
    cur = project.db.execute(
        "UPDATE columns SET format=? WHERE id=?", (format, column_id)
    )
    if cur.rowcount == 0:
        raise KeyError(f"no column {column_id}")
    if commit:
        project.db.commit()


def set_column_semantic_type(
    project: Any,
    column_id: int,
    semantic_type: str | None,
    *,
    commit: bool = True,
) -> None:
    """Set (or clear, with None) a column's explicit semantic-contract marker.

    Never inferred — callers (recipe output-field declarations, currently)
    are the only writers."""
    cur = project.db.execute(
        "UPDATE columns SET semantic_type=? WHERE id=?", (semantic_type, column_id)
    )
    if cur.rowcount == 0:
        raise KeyError(f"no column {column_id}")
    if commit:
        project.db.commit()


def add_rows(
    project: Any,
    sheet_id: int,
    records: list[dict[str, Any]],
    column_ids: dict[str, int],
    parent_row_ids: list[int] | None = None,
    *,
    producer_id: int | None = None,
    commit: bool = True,
) -> list[int]:
    """Bulk-insert source rows under one known base-cell producer.

    Callers that already own an admitted operation pass its ``producer_id``
    and retain transaction ownership. The standalone convenience path creates
    one undoable append operation and producer for the complete batch.
    """
    _validate_core_integer_source_values(project, records, column_ids)
    if not records:
        return []
    db = project.db
    nested = db.in_transaction
    savepoint = "add_rows"
    if nested:
        db.execute(f"SAVEPOINT {savepoint}")
    else:
        db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.cursor()
        row_ids: list[int] = []
        cell_writes: list[BaseCellWrite] = []
        base = cur.execute(
            "SELECT COALESCE(MAX(position),0) FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
        for i, rec in enumerate(records):
            parent = parent_row_ids[i] if parent_row_ids else None
            cur.execute(
                "INSERT INTO rows (sheet_id, position, parent_row_id) VALUES (?, ?, ?)",
                (sheet_id, base + i + 1, parent),
            )
            row_id = cur.lastrowid
            row_ids.append(row_id)
            cell_writes.extend(
                BaseCellWrite(row_id, column_ids[name], value)
                for name, value in rec.items()
                if name in column_ids and value is not None
            )
        if producer_id is None:
            op_id = project.append_op(
                "add_rows",
                {"sheet_id": sheet_id, "row_count": len(row_ids)},
                label=f"add {len(row_ids)} rows",
                commit=False,
            )
            project.set_undo_info(op_id, {"created_rows": row_ids}, commit=False)
            producer_id = create_base_cell_producer(
                db, stage_id=f"op:{op_id}", op_id=op_id
            )
        initialize_base_cells(db, producer_id=producer_id, cells=cell_writes)
        if nested:
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        if commit:
            db.commit()
        return row_ids
    except BaseException:
        if nested:
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            db.rollback()
        raise


def add_row_with_undo(
    project: Any, sheet_id: int, record: dict[str, Any], column_ids: dict[str, int]
) -> int:
    """Append one hand-entered row and its undo op atomically."""
    _validate_core_integer_source_values(project, [record], column_ids)
    cur = project.db.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        base = cur.execute(
            "SELECT COALESCE(MAX(position),0) FROM rows WHERE sheet_id=?",
            (sheet_id,),
        ).fetchone()[0]
        cur.execute(
            "INSERT INTO rows (sheet_id, position, parent_row_id) VALUES (?, ?, ?)",
            (sheet_id, base + 1, None),
        )
        row_id = cur.lastrowid
        cur.execute("UPDATE ops SET status='discarded' WHERE status='undone'")
        cur.execute(
            "INSERT INTO ops (kind, label, spec, undo_info, barrier) "
            "VALUES (?, ?, ?, ?, 0)",
            (
                "add_row",
                "add row",
                json.dumps(
                    {"sheet_id": sheet_id, "row_ids": [row_id], "cells": record}
                ),
                json.dumps({"created_rows": [row_id]}),
            ),
        )
        op_id = cur.lastrowid
        cur.execute("UPDATE meta SET value=? WHERE key='op_cursor'", (str(op_id),))
        producer_id = create_base_cell_producer(
            project.db, stage_id=f"op:{op_id}", op_id=op_id
        )
        initialize_base_cells(
            project.db,
            producer_id=producer_id,
            cells=(
                BaseCellWrite(row_id, column_ids[name], value)
                for name, value in record.items()
                if name in column_ids and value is not None
            ),
        )
        project.db.commit()
        return row_id
    except Exception:
        project.db.rollback()
        raise


def _validate_core_integer_source_values(
    project: Any,
    records: list[dict[str, Any]],
    column_ids: dict[str, int],
) -> None:
    """Enforce the core signed-int64 contract at the raw storage boundary."""
    if not records or not column_ids:
        return
    placeholders = ",".join("?" for _ in column_ids)
    integer_ids = {
        int(row["id"])
        for row in project.db.execute(
            f"SELECT id FROM columns WHERE id IN ({placeholders}) AND type='integer'",
            list(column_ids.values()),
        ).fetchall()
    }
    integer_names = {
        name for name, column_id in column_ids.items() if column_id in integer_ids
    }
    for row_index, record in enumerate(records):
        for name in integer_names & record.keys():
            value = record[name]
            if value is not None and not column_types.validate_value("integer", value):
                raise ValueError(
                    "invalid integer data at "
                    f"records[{row_index}].{name}: expected a signed 64-bit integer"
                )


def row_count(project: Any, sheet_id: int) -> int:
    return project.db.execute(
        "SELECT COUNT(*) FROM rows WHERE sheet_id=? AND hidden=0", (sheet_id,)
    ).fetchone()[0]


def visible_row_ids(
    project: Any, sheet_id: int, row_ids: list[int] | None = None
) -> list[int]:
    if row_ids is None:
        return [
            r["id"]
            for r in project.db.execute(
                "SELECT id FROM rows WHERE sheet_id=? AND hidden=0 ORDER BY position",
                (sheet_id,),
            )
        ]
    if not row_ids:
        return []

    matched: dict[int, int] = {}
    for offset in range(0, len(row_ids), _SQLITE_ID_CHUNK_SIZE):
        chunk = row_ids[offset : offset + _SQLITE_ID_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows = project.db.execute(
            "SELECT id, position FROM rows NOT INDEXED "
            f"WHERE sheet_id=? AND hidden=0 AND id IN ({placeholders})",
            [sheet_id, *chunk],
        ).fetchall()
        matched.update({int(row["id"]): int(row["position"]) for row in rows})
    return [row_id for row_id, _ in sorted(matched.items(), key=lambda item: item[1])]


def _current_cell_rows(
    project: Any,
    sheet_id: int,
    column_id: int,
    row_ids: list[int] | None,
    *,
    with_refs: bool,
) -> Iterator[sqlite3.Row]:
    """Page the current read representation, including never-populated blanks."""
    if row_ids is not None and not row_ids:
        return
    chunks = (
        [None]
        if row_ids is None
        else [
            row_ids[offset : offset + _SQLITE_ID_CHUNK_SIZE]
            for offset in range(0, len(row_ids), _SQLITE_ID_CHUNK_SIZE)
        ]
    )
    fields = "r.id, c.value"
    if with_refs:
        fields += ", c.origin_kind, c.origin_op_id, c.origin_run_id"
    for chunk in chunks:
        params: list[Any] = [column_id, sheet_id]
        row_filter = ""
        if chunk is not None:
            row_filter = f" AND r.id IN ({','.join('?' * len(chunk))})"
            params.extend(chunk)
        rows_source = "rows r NOT INDEXED" if chunk is not None else "rows r"
        yield from project.db.execute(
            f"SELECT {fields} FROM {rows_source} LEFT JOIN current_cells c "
            "ON c.column_id=? AND c.row_id=r.id "
            f"WHERE r.sheet_id=? AND r.hidden=0{row_filter}",
            params,
        )


def get_values_with_refs(
    project: Any,
    sheet_id: int,
    column_id: int,
    row_ids: list[int] | None = None,
    *,
    apply_edits: bool = True,
    tolerate_decode_errors: bool = False,
) -> tuple[dict[int, Any], dict[int, dict[str, Any]]]:
    """Read current values, or the explicit generated candidate beneath edits.

    The resolver is row-page aware: callers that pass row_ids should never
    scan full-run result/edit history to answer a viewport-sized request.

    ``apply_edits=False`` drops the manual-edit overlay layer, resolving to
    the underlying source-cell/current-run-result value the run wrote — the
    "fresh generated value" beneath a human edit (replay-accept-surface-v1;
    the read-time pending derivation compares this against the live value).

    ``tolerate_decode_errors=True`` treats malformed/non-strict source and
    edit JSON as null at its normal precedence. Ordinary callers keep the
    historical exception behavior by default.
    """
    if apply_edits:
        values: dict[int, Any] = {}
        refs: dict[int, dict[str, Any]] = {}
        for row in _current_cell_rows(
            project, sheet_id, column_id, row_ids, with_refs=True
        ):
            row_id = int(row["id"])
            values[row_id] = _decode_stored_value(
                row["value"], tolerate_errors=tolerate_decode_errors
            )
            refs[row_id] = {
                "kind": row["origin_kind"] or "missing",
                "op_id": row["origin_op_id"],
                "row_id": row_id,
                "column_id": column_id,
                "run_id": row["origin_run_id"],
            }
        return values, refs
    if row_ids is not None and not row_ids:
        return {}, {}
    if row_ids is not None and len(row_ids) > _SQLITE_ID_CHUNK_SIZE:
        out: dict[int, Any] = {}
        refs: dict[int, dict[str, Any]] = {}
        for offset in range(0, len(row_ids), _SQLITE_ID_CHUNK_SIZE):
            chunk_out, chunk_refs = get_values_with_refs(
                project,
                sheet_id,
                column_id,
                row_ids=row_ids[offset : offset + _SQLITE_ID_CHUNK_SIZE],
                apply_edits=apply_edits,
                tolerate_decode_errors=tolerate_decode_errors,
            )
            out.update(chunk_out)
            refs.update(chunk_refs)
        return out, refs
    generation_store = ResultGenerationStore(project)
    generation_managed = generation_store.is_generation_managed(column_id)
    out: dict[int, Any] = {}
    refs: dict[int, dict[str, Any]] = {}
    params: list[Any] = [column_id, sheet_id]
    row_filter = ""
    if row_ids is not None:
        row_filter = f" AND r.id IN ({','.join('?' * len(row_ids))})"
        params.extend(row_ids)
    rows_source = "rows r NOT INDEXED" if row_ids is not None else "rows r"
    # 3) source cells
    for r in project.db.execute(
        f"SELECT r.id, c.row_id AS source_row_id, c.value "
        f"FROM {rows_source} LEFT JOIN cells c "
        f"ON c.row_id = r.id AND c.column_id = ? "
        f"WHERE r.sheet_id=? AND r.hidden=0{row_filter}",
        params,
    ):
        out[r["id"]] = _decode_stored_value(
            r["value"], tolerate_errors=tolerate_decode_errors
        )
        if r["source_row_id"] is not None:
            refs[r["id"]] = {
                "kind": "source_cell",
                "op_id": None,
                "row_id": r["id"],
                "column_id": column_id,
                "run_id": None,
            }
    # 2) exact active heads for generated columns. Head existence is
    # authoritative even when the published effect is null or error: the head
    # read model deliberately returns ``value=None`` for both, so an older
    # result or source cell can never bleed through.
    if generation_managed:
        for row_id, head in generation_store.read_cell_heads(
            column_id,
            row_ids=row_ids,
        ).items():
            if row_id not in out:
                continue
            out[row_id] = head.value
            refs[row_id] = {
                "kind": "run_result",
                "op_id": head.op_id,
                "row_id": row_id,
                "column_id": column_id,
                "run_id": head.run_id,
            }
    for rid in out:
        refs.setdefault(
            rid,
            {
                "kind": "missing",
                "op_id": None,
                "row_id": rid,
                "column_id": column_id,
                "run_id": None,
            },
        )
    return out, refs


def get_values(
    project: Any,
    sheet_id: int,
    column_id: int,
    row_ids: list[int] | None = None,
    *,
    apply_edits: bool = True,
    tolerate_decode_errors: bool = False,
) -> dict[int, Any]:
    """Read current values without building unused provenance dictionaries.

    ``apply_edits=False`` drops the manual-edit overlay (see
    ``get_values_with_refs``)."""
    if apply_edits:
        return {
            int(row["id"]): _decode_stored_value(
                row["value"], tolerate_errors=tolerate_decode_errors
            )
            for row in _current_cell_rows(
                project, sheet_id, column_id, row_ids, with_refs=False
            )
        }
    out, _refs = project.get_values_with_refs(
        sheet_id,
        column_id,
        row_ids=row_ids,
        apply_edits=apply_edits,
        tolerate_decode_errors=tolerate_decode_errors,
    )
    return out


def apply_edits(
    project: Any,
    edits: list[dict[str, Any]],
    label: str = "manual edit",
    *,
    spec: dict[str, Any] | None = None,
) -> int:
    """An edit batch is an op (undoable by status flip).

    ``spec`` merges honest-provenance fields into the op's spec (e.g. an
    accept-regenerated-value op records ``source_run_id`` /
    ``from_replay_accept`` / ``value_hash``; replay-accept-surface-v1)."""
    row_ids = sorted({int(e["row_id"]) for e in edits if e.get("row_id") is not None})
    if row_ids:
        ph = ",".join("?" * len(row_ids))
        visible = {
            r["id"]
            for r in project.db.execute(
                f"SELECT id FROM rows WHERE id IN ({ph}) AND hidden=0",
                row_ids,
            )
        }
        missing = [rid for rid in row_ids if rid not in visible]
        if missing:
            raise ValueError(f"cannot edit hidden or missing row(s): {missing}")
    op_spec: dict[str, Any] = {"count": len(edits)}
    if spec:
        op_spec.update(spec)
    with project.db:
        op_id = project.append_op("edit", op_spec, label=label, commit=False)
        insert_edits(
            project.db,
            op_id=op_id,
            edits=[
                EditCellWrite(e["row_id"], e["column_id"], e.get("value"))
                for e in edits
            ],
        )
    return op_id


def pending_replay_values(
    project: Any, sheet_id: int, column_id: int, row_ids: list[int] | None = None
) -> dict[int, dict[str, Any]]:
    """The replay-pending cells of a column, keyed by row_id.

    A cell is pending iff its column is ai_generated, its live value is a
    manual-edit overlay, and the published replay-origin ``results.value``
    beneath it differs from the edit and has not been durably dismissed.
    Row-page aware; pass ``row_ids`` for a viewport-scoped set (badges).
    """
    return {
        entry["row_id"]: {
            "fresh_value": entry["fresh_value"],
            "edit_value": entry["edit_value"],
            "run_id": entry["run_id"],
            "generated_value_hash": entry["generated_value_hash"],
        }
        for entry in _replay_pending_candidates(project, sheet_id, column_id, row_ids)
    }


def pending_replay_count(project: Any, sheet_id: int, column_id: int) -> int:
    """Full-column-accurate count of replay-pending cells (dispositions #2).

    The chip count is a promise over the WHOLE column, never derived from a
    loaded row page — this scans the column via the indexed results/edits
    join, not the viewport."""
    return len(_replay_pending_candidates(project, sheet_id, column_id, None))


def _replay_pending_candidates(
    project: Any, sheet_id: int, column_id: int, row_ids: list[int] | None
) -> list[dict[str, Any]]:
    """Rows of an ai_generated column that were hand-edited AND whose
    published replay-origin result carries a DIFFERING fresh generated value.

    Derived at read time (staleness.py idiom): a single indexed query joins
    the replay-origin results, the latest applied manual edit per row, and the
    durable dismissals, then Python compares the parsed values so object
    values diff correctly. ``row_ids=None`` scans the whole column (the
    full-column count promise, dispositions #2); a page list scopes it.
    """
    col = project.db.execute(
        "SELECT id, sheet_id, current_run_id, ai_generated FROM columns WHERE id=?",
        (column_id,),
    ).fetchone()
    if col is None or not col["ai_generated"]:
        return []
    generation_managed, managed_origin_run_id = _managed_replay_origin_run_id(
        project, column_id
    )
    source_run_id = (
        managed_origin_run_id if generation_managed else col["current_run_id"]
    )
    if source_run_id is None:
        return []
    run_id = int(source_run_id)
    head_join = (
        "JOIN cell_result_heads head ON head.column_id=res.column_id "
        "AND head.row_id=res.row_id AND head.run_id=res.run_id "
        if generation_managed
        else ""
    )
    row_filter = ""
    if row_ids is not None:
        if not row_ids:
            return []
        row_filter = f" AND res.row_id IN ({','.join('?' * len(row_ids))})"
    rows = project.db.execute(
        "SELECT res.row_id AS row_id, res.value AS result_value, "
        "e.value AS edit_value "
        "FROM results res "
        + head_join
        + "JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = ? AND rr.hidden = 0 "
        "JOIN current_cells current ON current.row_id=res.row_id "
        "AND current.column_id=res.column_id AND current.origin_kind='manual_edit' "
        "JOIN edits e ON e.op_id=current.origin_op_id "
        "AND e.row_id=current.row_id AND e.column_id=current.column_id "
        "WHERE res.run_id = ? AND res.column_id = ? "
        "AND res.error IS NULL AND res.value IS NOT NULL" + row_filter,
        [
            col["sheet_id"],
            run_id,
            column_id,
            *(row_ids or []),
        ],
    ).fetchall()
    dismissed = {
        (int(d["row_id"]), str(d["generated_value_hash"]))
        for d in project.db.execute(
            "SELECT row_id, generated_value_hash FROM replay_edit_dismissals "
            "WHERE column_id=?",
            (column_id,),
        )
    }
    candidates: list[dict[str, Any]] = []
    for row in rows:
        fresh = json.loads(row["result_value"])
        edit_value = (
            json.loads(row["edit_value"]) if row["edit_value"] is not None else None
        )
        if fresh == edit_value:
            continue  # edit matches the fresh value -> nothing to surface
        value_hash = replay_generated_value_hash(fresh)
        if (int(row["row_id"]), value_hash) in dismissed:
            continue  # durable, value-identity dismissal
        candidates.append(
            {
                "row_id": int(row["row_id"]),
                "fresh_value": fresh,
                "edit_value": edit_value,
                "run_id": run_id,
                "generated_value_hash": value_hash,
            }
        )
    return candidates
