"""Stored-once text snapshots for source-artifact citations.

Citation artifacts normally point at the exact authoritative cell value that
was visible to their producer.  The small reference stays live until that
authority is changed or removed; SQLite triggers then copy the old text into
the hash-deduplicated snapshot table in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal


_AuthorityKind = Literal["source_cell", "run_result", "manual_edit"]
_HASH_PREFIX = "sha256:"


@dataclass(frozen=True)
class CapturedTextRef:
    """One captured string, either at an exact authority or already frozen."""

    content_hash: str
    sheet_id: int | None
    row_id: int | None
    column_id: int | None
    authority_kind: _AuthorityKind | None = None
    authority_id: int | None = None
    frozen_hash: str | None = None


_CREATE_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS citation_texts (
      content_hash TEXT PRIMARY KEY,
      text TEXT NOT NULL,
      CHECK (
        length(content_hash) = 71
        AND substr(content_hash, 1, 7) = 'sha256:'
        AND substr(content_hash, 8) NOT GLOB '*[^0-9a-f]*'
      )
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS citation_text_contexts (
      source_artifact_id INTEGER PRIMARY KEY
        REFERENCES source_artifacts(id) ON DELETE CASCADE,
      content_hash TEXT NOT NULL,
      sheet_id INTEGER,
      row_id INTEGER,
      column_id INTEGER,
      authority_kind TEXT CHECK (
        authority_kind IN ('source_cell', 'run_result', 'manual_edit')
      ),
      authority_id INTEGER,
      frozen_hash TEXT REFERENCES citation_texts(content_hash) ON DELETE RESTRICT,
      CHECK (
        length(content_hash) = 71
        AND substr(content_hash, 1, 7) = 'sha256:'
        AND substr(content_hash, 8) NOT GLOB '*[^0-9a-f]*'
      ),
      CHECK (
        (frozen_hash IS NOT NULL
          AND authority_kind IS NULL AND authority_id IS NULL)
        OR
        (frozen_hash IS NULL
          AND authority_kind IS NOT NULL AND authority_id IS NOT NULL
          AND sheet_id IS NOT NULL AND row_id IS NOT NULL AND column_id IS NOT NULL)
      ),
      CHECK (frozen_hash IS NULL OR frozen_hash = content_hash)
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_citation_text_contexts_authority
      ON citation_text_contexts(
        authority_kind, authority_id, row_id, column_id
      ) WHERE frozen_hash IS NULL
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_citation_texts_immutable_update
    BEFORE UPDATE ON citation_texts
    BEGIN
      SELECT RAISE(ABORT, 'citation text snapshots are immutable');
    END
    """,
)


def _freeze_old_trigger(
    *,
    name: str,
    event: str,
    table: str,
    authority_kind: _AuthorityKind,
    id_expr: str,
    when: str | None = None,
) -> str:
    old_matches = (
        "context.frozen_hash IS NULL "
        f"AND context.authority_kind='{authority_kind}' "
        f"AND context.authority_id={id_expr} "
        "AND context.row_id=OLD.row_id AND context.column_id=OLD.column_id"
    )
    update_matches = (
        "frozen_hash IS NULL "
        f"AND authority_kind='{authority_kind}' "
        f"AND authority_id={id_expr} "
        "AND row_id=OLD.row_id AND column_id=OLD.column_id"
    )
    return f"""
    CREATE TRIGGER IF NOT EXISTS {name}
    BEFORE {event} ON {table}
    {f"WHEN {when}" if when is not None else ""}
    BEGIN
      INSERT OR IGNORE INTO citation_texts(content_hash,text)
      SELECT context.content_hash,OLD.value
      FROM citation_text_contexts context
      WHERE {old_matches};
      SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM citation_text_contexts context
        JOIN citation_texts snapshot
          ON snapshot.content_hash=context.content_hash
        WHERE {old_matches} AND snapshot.text IS NOT OLD.value
      ) THEN RAISE(ABORT, 'citation text hash collision') END;
      UPDATE citation_text_contexts
      SET frozen_hash=content_hash,authority_kind=NULL,authority_id=NULL
      WHERE {update_matches};
    END
    """


def _freeze_replaced_trigger(
    *,
    name: str,
    table: str,
    authority_kind: _AuthorityKind,
    id_column: str,
) -> str:
    incumbent_matches = (
        "incumbent.row_id=NEW.row_id AND incumbent.column_id=NEW.column_id"
        if table == "cells"
        else (
            f"incumbent.{id_column}=NEW.{id_column} "
            "AND incumbent.row_id=NEW.row_id "
            "AND incumbent.column_id=NEW.column_id"
        )
    )
    context_matches = (
        "context.frozen_hash IS NULL "
        f"AND context.authority_kind='{authority_kind}' "
        f"AND context.authority_id=incumbent.{id_column} "
        "AND context.row_id=incumbent.row_id "
        "AND context.column_id=incumbent.column_id"
    )
    changed = (
        "(incumbent.value_kind IS NOT NEW.value_kind "
        "OR incumbent.value IS NOT NEW.value"
        + (
            " OR incumbent.producer_id IS NOT NEW.producer_id)"
            if table == "cells"
            else ")"
        )
    )
    return f"""
    CREATE TRIGGER IF NOT EXISTS {name}
    BEFORE INSERT ON {table}
    WHEN EXISTS (
      SELECT 1 FROM {table} incumbent
      JOIN citation_text_contexts context ON {context_matches}
      WHERE {incumbent_matches} AND {changed}
    )
    BEGIN
      INSERT OR IGNORE INTO citation_texts(content_hash,text)
      SELECT context.content_hash,incumbent.value
      FROM {table} incumbent
      JOIN citation_text_contexts context ON {context_matches}
      WHERE {incumbent_matches} AND {changed};
      SELECT CASE WHEN EXISTS (
        SELECT 1 FROM {table} incumbent
        JOIN citation_text_contexts context ON {context_matches}
        JOIN citation_texts snapshot
          ON snapshot.content_hash=context.content_hash
        WHERE {incumbent_matches} AND {changed}
          AND snapshot.text IS NOT incumbent.value
      ) THEN RAISE(ABORT, 'citation text hash collision') END;
      UPDATE citation_text_contexts
      SET frozen_hash=content_hash,authority_kind=NULL,authority_id=NULL
      WHERE source_artifact_id IN (
        SELECT context.source_artifact_id
        FROM {table} incumbent
        JOIN citation_text_contexts context ON {context_matches}
        WHERE {incumbent_matches} AND {changed}
      );
    END
    """


_MUTATION_TRIGGERS = (
    _freeze_old_trigger(
        name="trg_citation_cells_update_freeze",
        event="UPDATE OF value_kind,value,producer_id,row_id,column_id",
        table="cells",
        authority_kind="source_cell",
        id_expr="OLD.producer_id",
        when=(
            "OLD.value_kind IS NOT NEW.value_kind OR OLD.value IS NOT NEW.value "
            "OR OLD.producer_id IS NOT NEW.producer_id "
            "OR OLD.row_id IS NOT NEW.row_id OR OLD.column_id IS NOT NEW.column_id"
        ),
    ),
    _freeze_old_trigger(
        name="trg_citation_cells_delete_freeze",
        event="DELETE",
        table="cells",
        authority_kind="source_cell",
        id_expr="OLD.producer_id",
    ),
    _freeze_replaced_trigger(
        name="trg_citation_cells_replace_freeze",
        table="cells",
        authority_kind="source_cell",
        id_column="producer_id",
    ),
    _freeze_old_trigger(
        name="trg_citation_results_update_freeze",
        event="UPDATE OF value_kind,value,run_id,row_id,column_id",
        table="results",
        authority_kind="run_result",
        id_expr="OLD.run_id",
        when=(
            "OLD.value_kind IS NOT NEW.value_kind OR OLD.value IS NOT NEW.value "
            "OR OLD.run_id IS NOT NEW.run_id OR OLD.row_id IS NOT NEW.row_id "
            "OR OLD.column_id IS NOT NEW.column_id"
        ),
    ),
    _freeze_old_trigger(
        name="trg_citation_results_delete_freeze",
        event="DELETE",
        table="results",
        authority_kind="run_result",
        id_expr="OLD.run_id",
    ),
    _freeze_replaced_trigger(
        name="trg_citation_results_replace_freeze",
        table="results",
        authority_kind="run_result",
        id_column="run_id",
    ),
    _freeze_old_trigger(
        name="trg_citation_edits_update_freeze",
        event="UPDATE OF value_kind,value,op_id,row_id,column_id",
        table="edits",
        authority_kind="manual_edit",
        id_expr="OLD.op_id",
        when=(
            "OLD.value_kind IS NOT NEW.value_kind OR OLD.value IS NOT NEW.value "
            "OR OLD.op_id IS NOT NEW.op_id OR OLD.row_id IS NOT NEW.row_id "
            "OR OLD.column_id IS NOT NEW.column_id"
        ),
    ),
    _freeze_old_trigger(
        name="trg_citation_edits_delete_freeze",
        event="DELETE",
        table="edits",
        authority_kind="manual_edit",
        id_expr="OLD.op_id",
    ),
    _freeze_replaced_trigger(
        name="trg_citation_edits_replace_freeze",
        table="edits",
        authority_kind="manual_edit",
        id_column="op_id",
    ),
)


CITATION_TEXT_SCHEMA_STATEMENTS: tuple[str, ...] = (
    *_CREATE_TABLES,
    *_MUTATION_TRIGGERS,
)
CITATION_TEXT_SCHEMA_SQL = (
    ";\n".join(
        statement.strip().rstrip(";") for statement in CITATION_TEXT_SCHEMA_STATEMENTS
    )
    + ";\n"
)


def install_citation_text_schema(db: sqlite3.Connection) -> None:
    """Install citation storage inside the caller's transaction."""

    for statement in CITATION_TEXT_SCHEMA_STATEMENTS:
        db.execute(statement)


def _text_hash(text: str) -> str:
    return _HASH_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_int(value: Any) -> int | None:
    return (
        int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    )


def _native_text(
    db: sqlite3.Connection,
    *,
    authority_kind: _AuthorityKind,
    authority_id: int,
    sheet_id: int,
    row_id: int,
    column_id: int,
) -> str | None:
    if authority_kind == "source_cell":
        row = db.execute(
            "SELECT cell.value FROM cells cell "
            "JOIN rows row ON row.id=cell.row_id "
            "WHERE cell.producer_id=? AND cell.row_id=? AND cell.column_id=? "
            "AND row.sheet_id=? AND cell.value_kind='text'",
            (authority_id, row_id, column_id, sheet_id),
        ).fetchone()
    elif authority_kind == "run_result":
        row = db.execute(
            "SELECT result.value FROM results result "
            "JOIN runs run ON run.id=result.run_id "
            "WHERE result.run_id=? AND result.row_id=? AND result.column_id=? "
            "AND run.sheet_id=? AND result.value_kind='text'",
            (authority_id, row_id, column_id, sheet_id),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT edit.value FROM edits edit "
            "JOIN rows row ON row.id=edit.row_id "
            "WHERE edit.op_id=? AND edit.row_id=? AND edit.column_id=? "
            "AND row.sheet_id=? AND edit.value_kind='text'",
            (authority_id, row_id, column_id, sheet_id),
        ).fetchone()
    if row is None or not isinstance(row[0], str):
        return None
    return row[0]


def _store_frozen_text(db: sqlite3.Connection, *, content_hash: str, text: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO citation_texts(content_hash,text) VALUES (?,?)",
        (content_hash, text),
    )
    existing = db.execute(
        "SELECT text FROM citation_texts WHERE content_hash=?", (content_hash,)
    ).fetchone()
    if existing is None or existing[0] != text:
        raise sqlite3.IntegrityError("citation text hash collision")


def _authority_for_ref(
    db: sqlite3.Connection,
    *,
    value_ref: dict[str, Any],
    sheet_id: int,
    row_id: int,
    column_id: int,
) -> tuple[_AuthorityKind, int] | None:
    kind = value_ref.get("kind")
    if kind == "source_cell":
        captured_producer_id = _as_int(value_ref.get("producer_id"))
        if captured_producer_id is None:
            captured_producer_id = _as_int(value_ref.get("base_producer_id"))
        if captured_producer_id is not None:
            return ("source_cell", captured_producer_id)
        row = db.execute(
            "SELECT cell.producer_id FROM cells cell "
            "JOIN rows row ON row.id=cell.row_id "
            "WHERE cell.row_id=? AND cell.column_id=? AND row.sheet_id=?",
            (row_id, column_id, sheet_id),
        ).fetchone()
        authority_id = _as_int(row[0]) if row is not None else None
        return ("source_cell", authority_id) if authority_id is not None else None
    if kind == "run_result":
        authority_id = _as_int(value_ref.get("run_id"))
        return ("run_result", authority_id) if authority_id is not None else None
    if kind == "manual_edit":
        authority_id = _as_int(value_ref.get("op_id"))
        return ("manual_edit", authority_id) if authority_id is not None else None
    return None


def freeze_text_ref(
    db: sqlite3.Connection,
    *,
    text: str,
    value_ref: dict[str, Any] | None,
    sheet_id: int | None,
    row_id: int | None,
    column_id: int | None,
) -> CapturedTextRef:
    """Capture text at an exact native authority, freezing only if necessary."""

    if not isinstance(text, str):
        raise TypeError("captured citation text must be a string")
    content_hash = _text_hash(text)
    locator = (_as_int(sheet_id), _as_int(row_id), _as_int(column_id))
    if all(part is not None for part in locator) and isinstance(value_ref, dict):
        exact_locator = (int(locator[0]), int(locator[1]), int(locator[2]))
        authority = _authority_for_ref(
            db,
            value_ref=value_ref,
            sheet_id=exact_locator[0],
            row_id=exact_locator[1],
            column_id=exact_locator[2],
        )
        if authority is not None:
            native = _native_text(
                db,
                authority_kind=authority[0],
                authority_id=authority[1],
                sheet_id=exact_locator[0],
                row_id=exact_locator[1],
                column_id=exact_locator[2],
            )
            if native is not None and _text_hash(native) == content_hash:
                if native != text:
                    raise sqlite3.IntegrityError("citation text hash collision")
                return CapturedTextRef(
                    content_hash=content_hash,
                    sheet_id=exact_locator[0],
                    row_id=exact_locator[1],
                    column_id=exact_locator[2],
                    authority_kind=authority[0],
                    authority_id=authority[1],
                )
    _store_frozen_text(db, content_hash=content_hash, text=text)
    return CapturedTextRef(
        content_hash=content_hash,
        sheet_id=locator[0],
        row_id=locator[1],
        column_id=locator[2],
        frozen_hash=content_hash,
    )


def bind_text_ref(
    db: sqlite3.Connection, *, source_artifact_id: int, ref: CapturedTextRef
) -> None:
    """Bind one captured-text reference to its source artifact."""

    db.execute(
        "INSERT INTO citation_text_contexts ("
        "source_artifact_id,content_hash,sheet_id,row_id,column_id,"
        "authority_kind,authority_id,frozen_hash"
        ") VALUES (?,?,?,?,?,?,?,?)",
        (
            int(source_artifact_id),
            ref.content_hash,
            ref.sheet_id,
            ref.row_id,
            ref.column_id,
            ref.authority_kind,
            ref.authority_id,
            ref.frozen_hash,
        ),
    )


def text_ref_for_artifact(
    db: sqlite3.Connection, source_artifact_id: int
) -> CapturedTextRef | None:
    row = db.execute(
        "SELECT content_hash,sheet_id,row_id,column_id,authority_kind,"
        "authority_id,frozen_hash FROM citation_text_contexts "
        "WHERE source_artifact_id=?",
        (int(source_artifact_id),),
    ).fetchone()
    if row is None:
        return None
    return CapturedTextRef(
        content_hash=str(row[0]),
        sheet_id=_as_int(row[1]),
        row_id=_as_int(row[2]),
        column_id=_as_int(row[3]),
        authority_kind=row[4],
        authority_id=_as_int(row[5]),
        frozen_hash=row[6],
    )


def read_text_ref(db: sqlite3.Connection, ref: CapturedTextRef) -> str | None:
    """Read a capture without returning text from a mismatched authority."""

    if ref.frozen_hash is not None:
        row = db.execute(
            "SELECT text FROM citation_texts WHERE content_hash=?",
            (ref.frozen_hash,),
        ).fetchone()
        text = row[0] if row is not None else None
    elif (
        ref.authority_kind is not None
        and ref.authority_id is not None
        and ref.sheet_id is not None
        and ref.row_id is not None
        and ref.column_id is not None
    ):
        text = _native_text(
            db,
            authority_kind=ref.authority_kind,
            authority_id=ref.authority_id,
            sheet_id=ref.sheet_id,
            row_id=ref.row_id,
            column_id=ref.column_id,
        )
    else:
        return None
    if not isinstance(text, str) or _text_hash(text) != ref.content_hash:
        return None
    return text


def _current_value_ref(
    db: sqlite3.Connection, *, sheet_id: int, row_id: int, column_id: int
) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT current.origin_kind,current.origin_op_id,current.origin_run_id,"
        "current.base_producer_id "
        "FROM current_cell_values current "
        "JOIN rows row ON row.id=current.row_id "
        "WHERE current.row_id=? AND current.column_id=? AND row.sheet_id=?",
        (row_id, column_id, sheet_id),
    ).fetchone()
    if row is None or row[0] not in ("source_cell", "run_result", "manual_edit"):
        return None
    return {
        "kind": row[0],
        "op_id": row[1],
        "row_id": row_id,
        "column_id": column_id,
        "run_id": row[2],
        "producer_id": row[3],
    }


def capture_text_context(
    db: sqlite3.Connection,
    *,
    source_artifact_id: int,
    text: str,
    value_ref: dict[str, Any] | None,
    sheet_id: int | None,
    row_id: int | None,
    column_id: int | None,
) -> CapturedTextRef:
    """Capture and bind text, inferring only the current ref when absent."""

    if (
        value_ref is None
        and _as_int(sheet_id) is not None
        and _as_int(row_id) is not None
        and _as_int(column_id) is not None
    ):
        value_ref = _current_value_ref(
            db,
            sheet_id=int(sheet_id),
            row_id=int(row_id),
            column_id=int(column_id),
        )
    ref = freeze_text_ref(
        db,
        text=text,
        value_ref=value_ref,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
    )
    bind_text_ref(db, source_artifact_id=source_artifact_id, ref=ref)
    return ref


def migrate_legacy_citation_texts(db: sqlite3.Connection) -> None:
    """Move legacy ``metadata.captured_text`` into stored-once contexts.

    The caller owns the migration transaction.  Inline text is removed only
    after its context has been durably bound within that transaction.
    """

    after_id = 0
    while True:
        rows = db.execute(
            "SELECT id,source_sheet_id,source_row_id,source_column_id,metadata "
            "FROM source_artifacts WHERE id>? ORDER BY id LIMIT 500",
            (after_id,),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            after_id = int(row[0])
            try:
                metadata = json.loads(row[4])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict) or not isinstance(
                metadata.get("captured_text"), str
            ):
                continue
            text = metadata["captured_text"]
            existing = text_ref_for_artifact(db, int(row[0]))
            if existing is not None:
                if read_text_ref(db, existing) != text:
                    raise sqlite3.IntegrityError(
                        "legacy citation text does not match its stored context"
                    )
            else:
                captured_source = metadata.get("captured_source")
                value_ref = (
                    captured_source.get("value_ref")
                    if isinstance(captured_source, dict)
                    and isinstance(captured_source.get("value_ref"), dict)
                    else None
                )
                sheet_id, row_id, column_id = row[1], row[2], row[3]
                capture_text_context(
                    db,
                    source_artifact_id=int(row[0]),
                    text=text,
                    value_ref=value_ref,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    column_id=column_id,
                )
            metadata.pop("captured_text", None)
            db.execute(
                "UPDATE source_artifacts SET metadata=? WHERE id=?",
                (
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    int(row[0]),
                ),
            )
