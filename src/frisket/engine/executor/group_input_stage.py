"""Disposable grouped source rows; retain payloads on disk until dispatch."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from weakref import finalize

from frisket.ops.group_summary import group_summary_identity
from .table_read_stage import _cleanup


class _GroupRows:
    def __init__(self, stage, group_id):
        self.stage, self.group_id = stage, group_id

    def __iter__(self):
        cursor = self.stage.db.execute(
            "SELECT payload FROM inputs WHERE group_id=? ORDER BY position",
            (self.group_id,),
        )
        try:
            for row in cursor:
                yield json.loads(row[0])
        finally:
            if self.stage._cleanup.alive:
                cursor.close()


class GroupInputStage(Sequence):
    """Reiterable groups ordered by first contributor, without resident values."""

    def __init__(self, row_ids, *, source, group_by, group_type):
        directory = TemporaryDirectory(prefix="frisket-group-read-")
        self.path = Path(directory.name) / "groups.sqlite"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.executescript("""
            CREATE TABLE roster(row_id INTEGER PRIMARY KEY, position INTEGER NOT NULL);
            CREATE TABLE groups(id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE,
                                name TEXT NOT NULL, first_position INTEGER NOT NULL);
            CREATE TABLE inputs(row_id INTEGER PRIMARY KEY, position INTEGER NOT NULL,
                                group_id INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX group_inputs ON inputs(group_id,position);
        """)
        self.db.executemany(
            "INSERT OR IGNORE INTO roster VALUES(?,?)",
            ((row, number) for number, row in enumerate(row_ids)),
        )
        self.db.commit()
        self._cleanup = finalize(self, _cleanup, self.db, directory)
        self.source, self.group_by, self.group_type = source, group_by, group_type
        self.count = 0

    def add(self, row_id, values):
        ordinal = self.db.execute(
            "SELECT position FROM roster WHERE row_id=?", (row_id,)
        ).fetchone()[0]
        name, identity = group_summary_identity(
            values.get(self.group_by) if self.group_by is not None else "all",
            self.group_type,
        )
        self.db.execute(
            """INSERT INTO groups(identity,name,first_position) VALUES(?,?,?)
            ON CONFLICT(identity) DO UPDATE SET
              name=CASE WHEN excluded.first_position<groups.first_position THEN excluded.name ELSE groups.name END,
              first_position=min(groups.first_position,excluded.first_position)""",
            (identity, name, ordinal),
        )
        group_id = self.db.execute(
            "SELECT id FROM groups WHERE identity=?", (identity,)
        ).fetchone()[0]
        payload = {
            "row_id": row_id,
            "values": {name: values.get(name) for name in self.source},
        }
        self.db.execute(
            "INSERT INTO inputs VALUES(?,?,?,?)",
            (
                row_id,
                ordinal,
                group_id,
                json.dumps(payload, ensure_ascii=False, allow_nan=False),
            ),
        )
        self.count += 1
        if self.count % 256 == 0:
            self.db.commit()

    def seal(self):
        self.db.commit()

    def close(self):
        self._cleanup()

    def __len__(self):
        return self.db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]

    def _group(self, row):
        group_id, identity, name = row
        ids = [
            item[0]
            for item in self.db.execute(
                "SELECT row_id FROM inputs WHERE group_id=? ORDER BY position",
                (group_id,),
            )
        ]
        return {
            "name": name,
            "identity_key": identity,
            "source_row_ids": ids,
            "rows": _GroupRows(self, group_id),
        }

    def __iter__(self):
        cursor = self.db.execute(
            "SELECT id,identity,name FROM groups ORDER BY first_position"
        )
        try:
            for row in cursor:
                yield self._group(row)
        finally:
            if self._cleanup.alive:
                cursor.close()

    def by_identity(self, identity):
        row = self.db.execute(
            "SELECT id,identity,name FROM groups WHERE identity=?", (identity,)
        ).fetchone()
        if row is None:
            raise KeyError(identity)
        return self._group(row)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[number] for number in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        row = self.db.execute(
            "SELECT id,identity,name FROM groups ORDER BY first_position LIMIT 1 OFFSET ?",
            (index,),
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return self._group(row)
