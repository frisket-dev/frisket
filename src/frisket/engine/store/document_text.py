"""Authoritative document text selected through an ordinary Text column.

The column relationship is project metadata.  Prepared text keeps its existing
immutable artifact provenance; only flat manual edits need an extra pin to the
prepared reference whose source they correct.
"""

from __future__ import annotations

import json
import sqlite3
import re
from dataclasses import dataclass
from typing import Any


DOCUMENT_TEXT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS document_text_column_roles (
  source_column_id INTEGER PRIMARY KEY
    REFERENCES columns(id) ON DELETE CASCADE,
  text_column_id INTEGER NOT NULL UNIQUE
    REFERENCES columns(id) ON DELETE CASCADE,
  CHECK (source_column_id <> text_column_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS document_text_manual_pins (
  edit_op_id INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  text_column_id INTEGER NOT NULL,
  prepared_ref_id INTEGER NOT NULL
    REFERENCES prepared_content_refs(id) ON DELETE RESTRICT,
  PRIMARY KEY (edit_op_id,row_id,text_column_id),
  FOREIGN KEY (edit_op_id,row_id,text_column_id)
    REFERENCES edits(op_id,row_id,column_id) ON DELETE CASCADE
) WITHOUT ROWID;
"""


# Keep source selector eligibility identical at read and publication boundaries.
_SOURCE_PAGE_VALID_SQL = """(
    json_type(source_live.value,'$.page') IS NULL
    OR json_type(source_live.value,'$.page')='null'
    OR (
      source_column.type='file'
      AND json_type(source_live.value,'$.page')='integer'
      AND json_extract(source_live.value,'$.page')>0
    )
)"""


DOCUMENT_TEXT_SELECTION_VIEW_SQL = f"""
CREATE VIEW IF NOT EXISTS current_document_text_selections AS
SELECT source_column.sheet_id AS source_sheet_id,
       source_head.row_id AS row_id,
       role.source_column_id AS source_column_id,
       role.text_column_id AS text_column_id,
       source_head.retrieval_revision AS source_retrieval_revision,
       text_head.retrieval_revision AS text_retrieval_revision,
       json_extract(source_live.value,'$.blob') AS source_blob_hash,
       CASE WHEN json_type(source_live.value,'$.page')='integer'
            THEN CAST(json_extract(source_live.value,'$.page') AS INTEGER) END
         AS source_page_number,
       text_live.prepared_ref_id AS prepared_ref_id,
       COALESCE(text_live.prepared_ref_id,pin.prepared_ref_id)
         AS provenance_ref_id,
       ref.set_id AS set_id,
       content.source_artifact_id AS source_artifact_id,
       ref.page_number AS ref_page_number,
       artifact.page_count AS page_count,
       text_column.hidden AS text_hidden
FROM document_text_column_roles role
JOIN columns source_column ON source_column.id=role.source_column_id
JOIN columns text_column ON text_column.id=role.text_column_id
JOIN current_cells source_head ON source_head.column_id=role.source_column_id
JOIN current_cell_values source_live
  ON source_live.column_id=source_head.column_id
 AND source_live.row_id=source_head.row_id
JOIN current_cells text_head
  ON text_head.column_id=role.text_column_id
 AND text_head.row_id=source_head.row_id
JOIN current_cell_values text_live
  ON text_live.column_id=text_head.column_id
 AND text_live.row_id=text_head.row_id
LEFT JOIN document_text_manual_pins pin
  ON text_head.origin_kind='manual_edit'
 AND pin.edit_op_id=text_head.origin_op_id
 AND pin.row_id=text_head.row_id
 AND pin.text_column_id=text_head.column_id
JOIN prepared_content_refs ref
  ON ref.id=COALESCE(text_live.prepared_ref_id,pin.prepared_ref_id)
JOIN prepared_content_sets content ON content.id=ref.set_id
JOIN source_artifacts artifact ON artifact.id=content.source_artifact_id
WHERE source_column.sheet_id=text_column.sheet_id
  AND source_column.type IN ('file','image')
  AND text_column.type='text'
  AND source_column.active=1
  AND source_column.hidden=0
  AND text_column.active=1
  AND source_live.validity='valid'
  AND source_live.value_kind='json'
  AND text_live.validity='valid'
  AND text_live.value_kind='text'
  AND typeof(json_extract(source_live.value,'$.blob'))='text'
  AND length(json_extract(source_live.value,'$.blob'))>0
  AND {_SOURCE_PAGE_VALID_SQL}
  AND artifact.blob_hash=json_extract(source_live.value,'$.blob')
  AND ref.page_number IS CASE
    WHEN json_type(source_live.value,'$.page')='integer'
      THEN CAST(json_extract(source_live.value,'$.page') AS INTEGER)
    ELSE NULL END
  AND (
    (
      artifact.source_sheet_id=source_column.sheet_id
      AND artifact.source_row_id=source_head.row_id
      AND artifact.source_column_id=role.source_column_id
    )
    OR (
      ref.page_number IS NOT NULL
      AND EXISTS (
        SELECT 1
        FROM source_spans source_span
        JOIN evidence_link_spans linked ON linked.span_id=source_span.id
        JOIN evidence_links source_link ON source_link.id=linked.link_id
        WHERE source_span.artifact_id=artifact.id
          AND source_span.span_kind='page'
          AND source_span.page_start=ref.page_number
          AND source_span.page_end=ref.page_number
          AND source_link.status='active'
          AND source_link.link_role='primary_support'
          AND source_link.subject_kind='cell_value'
          AND source_link.sheet_id=source_column.sheet_id
          AND source_link.row_id=source_head.row_id
          AND source_link.column_id=role.source_column_id
      )
    )
  );
"""


class DocumentTextRoleError(ValueError):
    """A requested File-to-Text role is structurally invalid or conflicts."""


class DocumentTextPublicationRefused(DocumentTextRoleError):
    """A known source mismatch prevents this result from becoming current."""


class DocumentTextUnavailable(RuntimeError):
    """A configured role has no fresh, usable current document text."""

    def __init__(
        self,
        reason: str,
        *,
        source_cell: tuple[int, int, int],
        text_column_id: int,
    ) -> None:
        super().__init__(reason.replace("_", " "))
        self.reason = reason
        self.source_cell = source_cell
        self.text_column_id = text_column_id


@dataclass(frozen=True, slots=True)
class DocumentTextRole:
    source_column_id: int
    text_column_id: int
    sheet_id: int
    source_active: bool
    source_hidden: bool
    text_active: bool
    text_hidden: bool


@dataclass(frozen=True, slots=True)
class DocumentTextSelection:
    role: DocumentTextRole
    source_cell: tuple[int, int, int]
    text_cell: tuple[int, int, int]
    source_blob_hash: str
    source_page_number: int | None
    source_retrieval_revision: int
    text_retrieval_revision: int
    prepared_ref_id: int | None
    provenance_ref_id: int
    set_id: int
    source_artifact_id: int


def _db(value: Any) -> sqlite3.Connection:
    return value if isinstance(value, sqlite3.Connection) else value.db


def _role_table_exists(db: sqlite3.Connection) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='document_text_column_roles'"
        ).fetchone()
        is not None
    )


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _role_from_row(row: sqlite3.Row | tuple[Any, ...]) -> DocumentTextRole:
    return DocumentTextRole(
        source_column_id=int(row[0]),
        text_column_id=int(row[1]),
        sheet_id=int(row[2]),
        source_active=bool(row[3]),
        source_hidden=bool(row[4]),
        text_active=bool(row[5]),
        text_hidden=bool(row[6]),
    )


_ROLE_SELECT = (
    "SELECT role.source_column_id,role.text_column_id,source.sheet_id,"
    "source.active,source.hidden,text.active,text.hidden "
    "FROM document_text_column_roles role "
    "JOIN columns source ON source.id=role.source_column_id "
    "JOIN columns text ON text.id=role.text_column_id "
)


def document_text_role(
    db_or_project: Any, *, source_column_id: int
) -> DocumentTextRole | None:
    """Return configured role metadata, including hidden or inactive roles."""

    db = _db(db_or_project)
    if not _role_table_exists(db):
        return None
    row = db.execute(
        _ROLE_SELECT + "WHERE role.source_column_id=?",
        (_positive(source_column_id, "source_column_id"),),
    ).fetchone()
    return None if row is None else _role_from_row(row)


def document_text_role_for_text_column(
    db_or_project: Any, *, text_column_id: int
) -> DocumentTextRole | None:
    """Return configured role metadata by the designated Text column."""

    db = _db(db_or_project)
    if not _role_table_exists(db):
        return None
    row = db.execute(
        _ROLE_SELECT + "WHERE role.text_column_id=?",
        (_positive(text_column_id, "text_column_id"),),
    ).fetchone()
    return None if row is None else _role_from_row(row)


def document_text_related_column_ids(
    db_or_project: Any, *, column_id: int
) -> tuple[int, ...]:
    """Return the File/Text pair containing ``column_id``, or only itself."""

    column_id = _positive(column_id, "column_id")
    db = _db(db_or_project)
    if not _role_table_exists(db):
        return (column_id,)
    row = db.execute(
        "SELECT source_column_id,text_column_id FROM document_text_column_roles "
        "WHERE source_column_id=? OR text_column_id=?",
        (column_id, column_id),
    ).fetchone()
    if row is None:
        return (column_id,)
    return tuple(sorted((int(row[0]), int(row[1]))))


def is_generation_free_document_text_target(
    db_or_project: Any,
    *,
    sheet_id: int,
    source_column_id: int,
    text_column_id: int,
) -> bool:
    """Return whether an exact bound target is safe for first-run admission.

    Existing populated cells are allowed only when they already resolve through
    the designated role to fresh prepared provenance.  This admits seeded
    imports without adopting arbitrary or stale Text values.
    """

    db = _db(db_or_project)
    sheet_id = _positive(sheet_id, "sheet_id")
    source_column_id = _positive(source_column_id, "source_column_id")
    text_column_id = _positive(text_column_id, "text_column_id")
    if not _role_table_exists(db):
        return False
    columns = db.execute(
        "SELECT source.type,source.active,source.hidden,text.type,text.active,"
        "text.hidden,text.current_run_id "
        "FROM document_text_column_roles role "
        "JOIN columns source ON source.id=role.source_column_id "
        "JOIN columns text ON text.id=role.text_column_id "
        "WHERE role.source_column_id=? AND role.text_column_id=? "
        "AND source.sheet_id=? AND text.sheet_id=?",
        (source_column_id, text_column_id, sheet_id, sheet_id),
    ).fetchone()
    if (
        columns is None
        or str(columns[0]) not in {"file", "image"}
        or not bool(columns[1])
        or bool(columns[2])
        or str(columns[3]) != "text"
        or not bool(columns[4])
        or bool(columns[5])
        or columns[6] is not None
    ):
        return False
    if (
        db.execute(
            "SELECT 1 FROM results WHERE column_id=? "
            "UNION ALL SELECT 1 FROM run_output_generations WHERE column_id=? LIMIT 1",
            (text_column_id, text_column_id),
        ).fetchone()
        is not None
    ):
        return False
    stale_or_unpinned = db.execute(
        "SELECT 1 FROM current_cells head "
        "JOIN current_cell_values live "
        "ON live.row_id=head.row_id AND live.column_id=head.column_id "
        "WHERE head.column_id=? AND live.value_kind IS NOT NULL "
        "AND live.value_kind<>'null' AND NOT EXISTS ("
        "SELECT 1 FROM current_document_text_selections selected "
        "WHERE selected.source_sheet_id=? AND selected.row_id=head.row_id "
        "AND selected.source_column_id=? AND selected.text_column_id=?"
        ") LIMIT 1",
        (text_column_id, sheet_id, source_column_id, text_column_id),
    ).fetchone()
    return stale_or_unpinned is None


def ensure_document_text_role(
    project: Any,
    *,
    source_column_id: int,
    text_column_id: int,
    commit: bool = False,
) -> DocumentTextRole:
    """Bind one exact active File/Text pair; replay is idempotent."""

    db = _db(project)
    source_column_id = _positive(source_column_id, "source_column_id")
    text_column_id = _positive(text_column_id, "text_column_id")
    if not commit and not db.in_transaction:
        raise RuntimeError("document text role writes require a caller transaction")
    if not _role_table_exists(db):
        raise DocumentTextRoleError("document text role schema is unavailable")
    rows = db.execute(
        "SELECT id,sheet_id,type,active,hidden FROM columns WHERE id IN (?,?)",
        (source_column_id, text_column_id),
    ).fetchall()
    columns = {int(row[0]): row for row in rows}
    if set(columns) != {source_column_id, text_column_id}:
        raise DocumentTextRoleError("document text role columns must exist")
    source = columns[source_column_id]
    text = columns[text_column_id]
    if int(source[1]) != int(text[1]):
        raise DocumentTextRoleError("document text role columns must share a sheet")
    if str(source[2]) not in {"file", "image"} or str(text[2]) != "text":
        raise DocumentTextRoleError(
            "document text role requires a File/Image source and Text target"
        )
    if not bool(source[3]) or bool(source[4]) or not bool(text[3]):
        raise DocumentTextRoleError("document text role columns are unavailable")
    existing = db.execute(
        "SELECT source_column_id,text_column_id FROM document_text_column_roles "
        "WHERE source_column_id=? OR text_column_id=?",
        (source_column_id, text_column_id),
    ).fetchall()
    if existing and any(
        (int(row[0]), int(row[1])) != (source_column_id, text_column_id)
        for row in existing
    ):
        raise DocumentTextRoleError(
            "document text role conflicts with an existing pair"
        )
    db.execute(
        "INSERT OR IGNORE INTO document_text_column_roles "
        "(source_column_id,text_column_id) VALUES (?,?)",
        (source_column_id, text_column_id),
    )
    if commit:
        db.commit()
    role = document_text_role(db, source_column_id=source_column_id)
    assert role is not None
    return role


def resolve_document_text_selection(
    db_or_project: Any,
    source_cell: tuple[int, int, int],
    *,
    allow_hidden: bool = False,
) -> DocumentTextSelection | None:
    """Resolve the fresh selected Text value for one File cell.

    ``None`` means the File column has no configured role.  A configured role
    that is hidden, stale, empty, or lacks prepared provenance raises
    :class:`DocumentTextUnavailable` so callers cannot silently choose another
    text candidate.
    """

    if (
        not isinstance(source_cell, tuple)
        or len(source_cell) != 3
        or any(type(value) is not int or value <= 0 for value in source_cell)
    ):
        raise ValueError("source_cell must be positive (sheet,row,column) ids")
    db = _db(db_or_project)
    sheet_id, row_id, source_column_id = source_cell
    role = document_text_role(db, source_column_id=source_column_id)
    if role is None:
        return None
    if role.sheet_id != sheet_id:
        raise DocumentTextUnavailable(
            "source_cell_mismatch",
            source_cell=source_cell,
            text_column_id=role.text_column_id,
        )
    if not role.source_active or role.source_hidden or not role.text_active:
        raise DocumentTextUnavailable(
            "role_unavailable",
            source_cell=source_cell,
            text_column_id=role.text_column_id,
        )
    if role.text_hidden and not allow_hidden:
        raise DocumentTextUnavailable(
            "text_column_hidden",
            source_cell=source_cell,
            text_column_id=role.text_column_id,
        )
    row = db.execute(
        "SELECT source_retrieval_revision,text_retrieval_revision,"
        "source_blob_hash,source_page_number,prepared_ref_id,provenance_ref_id,"
        "set_id,source_artifact_id FROM current_document_text_selections "
        "WHERE source_sheet_id=? AND row_id=? AND source_column_id=?",
        source_cell,
    ).fetchone()
    if row is None:
        raise DocumentTextUnavailable(
            "selection_stale_or_missing",
            source_cell=source_cell,
            text_column_id=role.text_column_id,
        )
    return DocumentTextSelection(
        role=role,
        source_cell=source_cell,
        text_cell=(sheet_id, row_id, role.text_column_id),
        source_blob_hash=str(row[2]),
        source_page_number=None if row[3] is None else int(row[3]),
        source_retrieval_revision=int(row[0]),
        text_retrieval_revision=int(row[1]),
        prepared_ref_id=None if row[4] is None else int(row[4]),
        provenance_ref_id=int(row[5]),
        set_id=int(row[6]),
        source_artifact_id=int(row[7]),
    )


def inherit_document_text_manual_pins(
    db: sqlite3.Connection,
    encoded_edits: list[tuple[int, int, int, str, object]],
) -> None:
    """Pin flat Text edits to their predecessor's prepared provenance.

    This runs after the immutable edit rows are inserted but before current-cell
    projection refresh, so ``current_cells`` still identifies each predecessor.
    """

    if not encoded_edits or not _role_table_exists(db):
        return
    columns = {column for _, _, column, kind, _ in encoded_edits if kind == "text"}
    if not columns:
        return
    designated = {
        int(row[0])
        for row in db.execute(
            "SELECT text_column_id FROM document_text_column_roles "
            "WHERE text_column_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(columns)),),
        )
    }
    if not designated:
        return
    targets = [
        (op_id, row_id, column_id)
        for op_id, row_id, column_id, kind, _ in encoded_edits
        if kind == "text" and column_id in designated
    ]
    # Read only predecessor metadata, in bounded batches; never hydrate bodies.
    for offset in range(0, len(targets), 300):
        batch = targets[offset : offset + 300]
        placeholders = ",".join("(?,?,?)" for _ in batch)
        db.execute(
            f"""WITH targets(edit_op_id,row_id,text_column_id) AS (
                VALUES {placeholders}
            )
            INSERT INTO document_text_manual_pins
              (edit_op_id,row_id,text_column_id,prepared_ref_id)
            SELECT target.edit_op_id,target.row_id,target.text_column_id,
                   COALESCE(live.prepared_ref_id,pin.prepared_ref_id)
            FROM targets target
            JOIN current_cell_values live
              ON live.row_id=target.row_id AND live.column_id=target.text_column_id
            LEFT JOIN document_text_manual_pins pin
              ON live.origin_kind='manual_edit' AND pin.edit_op_id=live.origin_op_id
             AND pin.row_id=live.row_id AND pin.text_column_id=live.column_id
            WHERE COALESCE(live.prepared_ref_id,pin.prepared_ref_id) IS NOT NULL""",
            tuple(value for target in batch for value in target),
        )


def _prepared_result_matches_source_sql(result_alias: str) -> str:
    """Return metadata-only SQL matching a prepared result to its role File."""

    return f"""EXISTS (
      SELECT 1
      FROM document_text_column_roles role
      JOIN columns source_column ON source_column.id=role.source_column_id
      JOIN columns text_column ON text_column.id=role.text_column_id
      JOIN current_cell_values source_live
        ON source_live.column_id=role.source_column_id
       AND source_live.row_id={result_alias}.row_id
      JOIN prepared_content_refs ref
        ON ref.id=CAST({result_alias}.value AS INTEGER)
      JOIN prepared_content_sets content ON content.id=ref.set_id
      JOIN source_artifacts artifact ON artifact.id=content.source_artifact_id
      WHERE role.text_column_id={result_alias}.column_id
        AND source_column.sheet_id=text_column.sheet_id
        AND source_column.type IN ('file','image') AND text_column.type='text'
        AND source_column.active=1 AND source_column.hidden=0
        AND text_column.active=1
        AND source_live.validity='valid' AND source_live.value_kind='json'
        AND typeof(json_extract(source_live.value,'$.blob'))='text'
        AND length(json_extract(source_live.value,'$.blob'))>0
        AND {_SOURCE_PAGE_VALID_SQL}
        AND artifact.blob_hash=json_extract(source_live.value,'$.blob')
        AND ref.page_number IS CASE
          WHEN json_type(source_live.value,'$.page')='integer'
            AND json_extract(source_live.value,'$.page')>0
          THEN CAST(json_extract(source_live.value,'$.page') AS INTEGER)
          ELSE NULL END
        AND (
          (artifact.source_sheet_id=source_column.sheet_id
           AND artifact.source_row_id={result_alias}.row_id
           AND artifact.source_column_id=role.source_column_id)
          OR (
            ref.page_number IS NOT NULL
            AND EXISTS (
              SELECT 1 FROM source_spans source_span
              JOIN evidence_link_spans linked ON linked.span_id=source_span.id
              JOIN evidence_links source_link ON source_link.id=linked.link_id
              WHERE source_span.artifact_id=artifact.id
                AND source_span.span_kind='page'
                AND source_span.page_start=ref.page_number
                AND source_span.page_end=ref.page_number
                AND source_link.status='active'
                AND source_link.link_role='primary_support'
                AND source_link.subject_kind='cell_value'
                AND source_link.sheet_id=source_column.sheet_id
                AND source_link.row_id={result_alias}.row_id
                AND source_link.column_id=role.source_column_id
            )
          )
        )
    )"""


def document_text_result_eligibility_sql(
    result_alias: str = "result", generation_alias: str = "generation"
) -> str:
    """Return immutable head eligibility from the generation's role snapshot."""

    if any(
        not alias.replace("_", "").isalnum()
        for alias in (result_alias, generation_alias)
    ):
        raise ValueError("SQL aliases must be identifiers")
    return (
        f"({generation_alias}.document_text_authority=0 OR ("
        f"{result_alias}.publication_effect='publish_value' AND "
        f"{result_alias}.value_kind='prepared_content_ref'" + "))"
    )


def document_text_generation_authority(
    db: sqlite3.Connection, *, column_id: int
) -> bool:
    """Snapshot whether a new output generation targets a designated Text.

    Designation is durable policy.  Source and target availability is checked
    by admission and publication fences, but must not retroactively weaken how
    failed values from an already planned generation affect canonical heads.
    """

    if not _role_table_exists(db):
        return False
    return (
        db.execute(
            "SELECT 1 FROM document_text_column_roles WHERE text_column_id=?",
            (_positive(column_id, "column_id"),),
        ).fetchone()
        is not None
    )


def require_document_text_publication_sources(
    db: sqlite3.Connection,
    *,
    run_id: int,
    keys: list[tuple[int, int]],
) -> None:
    """Fence new authoritative prepared values to the current source snapshot."""

    if (
        not keys
        or db.execute(
            "SELECT 1 FROM run_output_generations WHERE run_id=? "
            "AND document_text_authority=1 LIMIT 1",
            (int(run_id),),
        ).fetchone()
        is None
    ):
        return
    ordered = sorted({(int(row_id), int(column_id)) for row_id, column_id in keys})
    # Two ids per requested coordinate plus the run id stay below SQLite's
    # common 999-variable limit.
    for start in range(0, len(ordered), 400):
        chunk = ordered[start : start + 400]
        requested = ",".join("(?,?)" for _ in chunk)
        params = [value for pair in chunk for value in pair]
        bad = db.execute(
            f"WITH requested(row_id,column_id) AS (VALUES {requested}) "
            "SELECT result.row_id,result.column_id,result.value_kind "
            "FROM requested target JOIN results result "
            "ON result.row_id=target.row_id AND result.column_id=target.column_id "
            "JOIN run_output_generations generation "
            "ON generation.run_id=result.run_id "
            "AND generation.column_id=result.column_id "
            "WHERE result.run_id=? AND generation.document_text_authority=1 "
            "AND result.publication_effect='publish_value' AND ("
            "result.value_kind<>'prepared_content_ref' OR NOT "
            + _prepared_result_matches_source_sql("result")
            + ") LIMIT 1",
            (*params, int(run_id)),
        ).fetchone()
        if bad is not None:
            raise DocumentTextPublicationRefused(
                "authoritative document text must be prepared content matching "
                f"its current source (row {int(bad[0])}, column {int(bad[1])})"
            )


def require_document_text_publication_key_table(
    db: sqlite3.Connection, *, run_id: int, key_table: str
) -> None:
    """Fence staged authoritative publication using an existing key table."""

    if re.fullmatch(r"temp_result_keys_[0-9a-f]+", key_table) is None:
        raise ValueError("invalid document text publication key table")
    bad = db.execute(
        "SELECT result.row_id,result.column_id,result.value_kind "
        f"FROM {key_table} target JOIN results result "
        "ON result.row_id=target.row_id AND result.column_id=target.column_id "
        "JOIN run_output_generations generation "
        "ON generation.run_id=result.run_id "
        "AND generation.column_id=result.column_id "
        "WHERE result.run_id=? AND generation.document_text_authority=1 "
        "AND result.publication_effect='publish_value' AND ("
        "result.value_kind<>'prepared_content_ref' OR NOT "
        + _prepared_result_matches_source_sql("result")
        + ") LIMIT 1",
        (int(run_id),),
    ).fetchone()
    if bad is not None:
        raise DocumentTextPublicationRefused(
            "authoritative document text must be prepared content matching "
            f"its current source (row {int(bad[0])}, column {int(bad[1])})"
        )


__all__ = [
    "DOCUMENT_TEXT_SCHEMA_SQL",
    "DOCUMENT_TEXT_SELECTION_VIEW_SQL",
    "DocumentTextRole",
    "DocumentTextRoleError",
    "DocumentTextPublicationRefused",
    "DocumentTextSelection",
    "DocumentTextUnavailable",
    "document_text_related_column_ids",
    "document_text_generation_authority",
    "document_text_result_eligibility_sql",
    "document_text_role",
    "document_text_role_for_text_column",
    "ensure_document_text_role",
    "inherit_document_text_manual_pins",
    "is_generation_free_document_text_target",
    "require_document_text_publication_sources",
    "require_document_text_publication_key_table",
    "resolve_document_text_selection",
]
