"""A project read snapshot must ATTACH ``file:`` URIs on every SQLite build.

SQLite builds without SQLITE_USE_URI (uv-managed CPython among them) treat a
``file:...?mode=ro`` ATTACH target as a literal filename unless the connection
was opened with ``uri=True``. The retrieval reader attaches its index that way
through the snapshot's connection.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing


def test_read_snapshot_attaches_a_read_only_file_uri(tmp_path, make_project):
    index = tmp_path / "index.db"
    with closing(sqlite3.connect(index)) as db:
        db.execute("CREATE TABLE meta (value TEXT)")
        db.execute("INSERT INTO meta VALUES ('attached')")
        db.commit()
    project = make_project("uri")
    with project.read_snapshot() as snapshot:
        snapshot.db.execute(
            "ATTACH DATABASE ? AS idx", (f"{index.resolve().as_uri()}?mode=ro",)
        )
        row = snapshot.db.execute("SELECT value FROM idx.meta").fetchone()
        assert tuple(row) == ("attached",)
