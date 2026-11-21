"""Rename the Ask research pause column without rewriting saved pause state."""

from __future__ import annotations

import sqlite3

from frisket.engine.store.current_cells import register_current_cell_functions


RESEARCH_PAUSE_TO_DIGEST = "frisket.schema.v1:6c7377b07512aa641693bd0486c1ca0d"


def apply_research_pause(db: sqlite3.Connection) -> None:
    # SQLite validates views referencing custom functions during RENAME COLUMN.
    register_current_cell_functions(db)
    db.execute(
        "ALTER TABLE project_qa_research_runs "
        "RENAME COLUMN pending_approval_json TO pending_pause_json"
    )
