"""Authoritative document text selected through an ordinary Text column.

The column relationship is project metadata.  Prepared text keeps its existing
immutable artifact provenance; only flat manual edits need an extra pin to the
prepared reference whose source they correct.
"""

from __future__ import annotations

import sqlite3
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


class DocumentTextRoleError(ValueError):
    """A requested File-to-Text role is structurally invalid or conflicts."""


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

    row = _db(db_or_project).execute(
        _ROLE_SELECT + "WHERE role.source_column_id=?",
        (_positive(source_column_id, "source_column_id"),),
    ).fetchone()
    return None if row is None else _role_from_row(row)


def document_text_role_for_text_column(
    db_or_project: Any, *, text_column_id: int
) -> DocumentTextRole | None:
    """Return configured role metadata by the designated Text column."""

    row = _db(db_or_project).execute(
        _ROLE_SELECT + "WHERE role.text_column_id=?",
        (_positive(text_column_id, "text_column_id"),),
    ).fetchone()
    return None if row is None else _role_from_row(row)


def document_text_related_column_ids(
    db_or_project: Any, *, column_id: int
) -> tuple[int, ...]:
    """Return the File/Text pair containing ``column_id``, or only itself."""

    column_id = _positive(column_id, "column_id")
    row = _db(db_or_project).execute(
        "SELECT source_column_id,text_column_id FROM document_text_column_roles "
        "WHERE source_column_id=? OR text_column_id=?",
        (column_id, column_id),
    ).fetchone()
    if row is None:
        return (column_id,)
    return tuple(sorted((int(row[0]), int(row[1]))))


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
    if str(source[2]) != "file" or str(text[2]) != "text":
        raise DocumentTextRoleError("document text role requires File and Text columns")
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
        raise DocumentTextRoleError("document text role conflicts with an existing pair")
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

    del db_or_project, source_cell, allow_hidden
    raise NotImplementedError("document text selection resolver is not implemented")


__all__ = [
    "DOCUMENT_TEXT_SCHEMA_SQL",
    "DocumentTextRole",
    "DocumentTextRoleError",
    "DocumentTextSelection",
    "DocumentTextUnavailable",
    "document_text_related_column_ids",
    "document_text_role",
    "document_text_role_for_text_column",
    "ensure_document_text_role",
    "resolve_document_text_selection",
]
