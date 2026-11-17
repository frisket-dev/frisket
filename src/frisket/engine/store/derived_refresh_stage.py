"""Disposable disk staging for a complete derived refresh enumeration."""

from __future__ import annotations

import json
import sqlite3
from tempfile import TemporaryDirectory
from pathlib import Path

from .keyed_updates import canonical

REFRESH_BATCH_SIZE = 256


class DerivedRefreshStage:
    """Validate identities before effects, then iterate bounded publication batches.

    This is scratch space, not project history or a resumable execution store.
    All seen keys survive conflicting duplicates so they cannot imply absence.
    """

    def __init__(self, *, keyed: bool):
        self.keyed = keyed
        self._directory = TemporaryDirectory(prefix="frisket-derived-refresh-")
        self.db = sqlite3.connect(Path(self._directory.name) / "rows.sqlite")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.executescript(
            "CREATE TABLE staged(seq INTEGER PRIMARY KEY,key TEXT NOT NULL,"
            "values_json TEXT NOT NULL,parent_row_id INTEGER,sources_json TEXT NOT NULL,metadata_json TEXT);"
            "CREATE INDEX staged_key ON staged(key);"
            "CREATE TABLE published(row_id INTEGER PRIMARY KEY);"
        )
        self._count = 0
        self._sealed = False
        self.row_count = 0

    def add(self, row, *, metadata=None):
        if self._sealed:
            raise ValueError("derived enumeration is already complete")
        key = row.key if self.keyed else f"occurrence:{self._count}"
        if key is None:
            raise ValueError("A keyed derived output omitted its declared identity")
        self.db.execute(
            "INSERT INTO staged VALUES(?,?,?,?,?,?)",
            (
                self._count,
                key,
                canonical(row.values),
                row.parent_row_id,
                canonical(row.sources),
                canonical(metadata) if metadata is not None else None,
            ),
        )
        self._count += 1
        if self._count % REFRESH_BATCH_SIZE == 0:
            self.db.commit()

    def seal(self):
        if self._sealed:
            return
        self.db.execute(
            "CREATE TABLE identities AS SELECT key,MIN(seq) AS seq,"
            "COUNT(DISTINCT values_json)>1 AS conflict FROM staged GROUP BY key"
        )
        self.db.execute("CREATE UNIQUE INDEX identity_key ON identities(key)")
        self.row_count = int(
            self.db.execute(
                "SELECT COUNT(*) FROM identities WHERE conflict=0"
            ).fetchone()[0]
        )
        self.db.commit()
        self._sealed = True

    def batches(self, size=REFRESH_BATCH_SIZE):
        from .derived_updates import DerivedRefreshRow

        if not self._sealed:
            raise ValueError("derived enumeration is not complete")
        cursor = self.db.execute(
            "SELECT s.seq,s.key,s.values_json,s.parent_row_id,s.sources_json,s.metadata_json "
            "FROM staged s JOIN identities i ON i.seq=s.seq WHERE i.conflict=0 ORDER BY s.seq"
        )
        while rows := cursor.fetchmany(size):
            yield [
                (
                    seq,
                    DerivedRefreshRow(
                        json.loads(values),
                        key if self.keyed else None,
                        parent,
                        tuple(map(tuple, json.loads(sources))),
                    ),
                    json.loads(metadata) if metadata is not None else None,
                )
                for seq, key, values, parent, sources, metadata in rows
            ]

    def record_published(self, row_ids):
        self.db.executemany(
            "INSERT OR IGNORE INTO published VALUES(?)",
            ((row,) for row in row_ids if row is not None),
        )
        self.db.commit()

    def contains(self, *, keys=(), row_ids=()):
        if not self._sealed:
            raise ValueError("absence cannot be inferred before complete enumeration")
        if self.keyed:
            return {
                row[0]
                for row in self.db.execute(
                    "SELECT key FROM identities WHERE key IN (SELECT value FROM json_each(?))",
                    (json.dumps(list(keys)),),
                )
            }
        return {
            row[0]
            for row in self.db.execute(
                "SELECT row_id FROM published WHERE row_id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(row_ids)),),
            )
        }

    def close(self):
        self.db.close()
        self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
