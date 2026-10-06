"""Add project-local extraction drafts without changing source or result data."""

from __future__ import annotations

import sqlite3

from .extraction_layouts import EXTRACTION_LAYOUTS_SCHEMA_SQL
from .scalar_storage_migration import SCALAR_CURRENT_VALUES_TO_DIGEST
from .schema import SCHEMA_DIGEST_META_KEY


EXTRACTION_LAYOUTS_FROM_DIGEST = SCALAR_CURRENT_VALUES_TO_DIGEST
EXTRACTION_LAYOUTS_TO_DIGEST = "frisket.schema.v1:82e99299b4fe25eb46a92c8b0b56e265"


def migrate_extraction_layouts(db: sqlite3.Connection) -> None:
    query = "SELECT value FROM meta WHERE key=?"
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
    except sqlite3.DatabaseError:
        return
    if row is None or row[0] != EXTRACTION_LAYOUTS_FROM_DIGEST:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(query, (SCHEMA_DIGEST_META_KEY,)).fetchone()
        if row is not None and row[0] == EXTRACTION_LAYOUTS_FROM_DIGEST:
            statement = ""
            for line in EXTRACTION_LAYOUTS_SCHEMA_SQL.splitlines(keepends=True):
                if line.lstrip().startswith("--"):
                    continue
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement.replace(" IF NOT EXISTS", ""))
                    statement = ""
            if statement.strip():
                raise RuntimeError("incomplete extraction layouts migration DDL")
            db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (EXTRACTION_LAYOUTS_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
            )
        db.commit()
    except BaseException:
        db.rollback()
        raise
