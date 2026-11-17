"""Disposable source staging for list expansion and exact joins.

Payloads and matching indexes stay on disk; only live source tokens occupy
Python memory. Both readers share the same resource cleanup boundary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from weakref import ref, finalize, WeakValueDictionary

from frisket.actions.types import ListItem, RowSource
from .joined_table import _row_key, _estimated_rows, _key_repr


def _cleanup(db, directory):
    db.close()
    directory.cleanup()


class ListItemStage(Sequence[ListItem]):
    def __init__(self, owner, sheet_id):
        directory = TemporaryDirectory(prefix="frisket-list-read-")
        self.db = sqlite3.connect(Path(directory.name) / "items.sqlite")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute(
            "CREATE TABLE items(id INTEGER PRIMARY KEY,row_id INTEGER,position INTEGER,value TEXT,parents TEXT,evidence TEXT)"
        )
        self._cleanup = finalize(self, _cleanup, self.db, directory)
        self._owner = ref(owner)
        self.sheet_id = sheet_id
        self.count = 0
        self._tokens = WeakValueDictionary()

    def add(self, row_id, value, parents, evidence, *, position):
        self.db.execute(
            "INSERT INTO items VALUES(?,?,?,?,?,?)",
            (
                self.count,
                row_id,
                position,
                json.dumps(value, allow_nan=False),
                json.dumps(parents, allow_nan=False),
                json.dumps(evidence, allow_nan=False),
            ),
        )
        self.count += 1
        if self.count % 256 == 0:
            self.db.commit()

    def seal(self):
        self.db.execute(
            "CREATE TABLE ordering AS SELECT row_number() OVER (ORDER BY position,row_id,id)-1 AS ordinal,id FROM items"
        )
        self.db.execute("CREATE UNIQUE INDEX item_ordinal ON ordering(ordinal)")
        self.db.commit()

    def close(self):
        self._cleanup()

    def __len__(self):
        return self.count

    def _item(self, row):
        index, row_id, value, parents, evidence = row
        token = self._tokens.get(index)
        if token is None:
            token = RowSource(sheet_id=self.sheet_id, row_id=row_id)
            self._tokens[index] = token
        owner = self._owner()
        if owner is None:
            raise RuntimeError("List read invocation is closed")
        owner.sources.add(token)
        owner.item_associations[token] = json.loads(evidence)
        return ListItem(
            value=json.loads(value), source=token, parent_values=json.loads(parents)
        )

    def __iter__(self):
        for row in self.db.execute(
            "SELECT i.id,i.row_id,i.value,i.parents,i.evidence FROM ordering o JOIN items i ON i.id=o.id ORDER BY o.ordinal"
        ):
            yield self._item(row)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self.count)))
        if index < 0:
            index += self.count
        row = self.db.execute(
            "SELECT i.id,i.row_id,i.value,i.parents,i.evidence FROM ordering o JOIN items i ON i.id=o.id WHERE o.ordinal=?",
            (index,),
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return self._item(row)


class JoinReadStage:
    def __init__(self):
        directory = TemporaryDirectory(prefix="frisket-join-read-")
        self.db = sqlite3.connect(Path(directory.name) / "join.sqlite")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.executescript(
            "CREATE TABLE source(side TEXT,row_id INTEGER,key TEXT,body TEXT,PRIMARY KEY(side,row_id));CREATE INDEX source_key ON source(side,key);"
        )
        self._cleanup = finalize(self, _cleanup, self.db, directory)
        self.count = 0

    def close(self):
        self._cleanup()

    def add(self, side, row_id, values, specs):
        key = _row_key(row_id, values, specs, side)
        if key is not None:
            # Python numeric equality treats negative zero as zero too.
            key = json.dumps(
                [
                    (tag, 0.0 if tag == "n" and value == 0 else value)
                    for tag, value in key
                ],
                separators=(",", ":"),
            )
        self.db.execute(
            "INSERT INTO source VALUES(?,?,?,?)",
            (
                side,
                row_id,
                key,
                json.dumps(
                    {str(cid): rows.get(row_id) for cid, rows in values.items()},
                    allow_nan=False,
                ),
            ),
        )
        self.count += 1
        if self.count % 256 == 0:
            self.db.commit()

    def seal(self):
        self.db.executescript(
            "CREATE TABLE counts AS SELECT key,sum(side='left') AS l,sum(side='right') AS r FROM source WHERE key IS NOT NULL GROUP BY key;CREATE UNIQUE INDEX counts_key ON counts(key);"
        )
        self.db.commit()

    def stats(self, how):
        both, distinct, fanout = self.db.execute(
            "SELECT coalesce(sum(l*r),0),coalesce(sum(l>0 AND r>0),0),coalesce(max(l*r),0) FROM counts"
        ).fetchone()
        left, right = self.db.execute(
            "SELECT coalesce(sum(side='left' AND (s.key IS NULL OR c.r=0)),0),coalesce(sum(side='right' AND (s.key IS NULL OR c.l=0)),0) FROM source s LEFT JOIN counts c ON c.key=s.key"
        ).fetchone()
        null_left, null_right = self.db.execute(
            "SELECT coalesce(sum(side='left'),0),coalesce(sum(side='right'),0) FROM source WHERE key IS NULL"
        ).fetchone()
        return {
            "both": both,
            "left_only": left if how in ("left", "outer") else 0,
            "right_only": right if how in ("right", "outer") else 0,
            "matched_pairs": both,
            "distinct_matched_keys": distinct,
            "max_fanout": fanout,
            "null_left_rows": null_left,
            "null_right_rows": null_right,
        }, _estimated_rows(how, both, left, right)

    def top_keys(self):
        # SQL limits before payload decode; stable printable-key order is applied
        # in SQL rather than sorting every distinct key in Python.
        self.db.create_function("display_key", 1, lambda k: _key_repr(json.loads(k)))
        return [
            {"key": display, "left": left, "right": right, "rows": left * right}
            for display, left, right in self.db.execute(
                "SELECT display_key(key),l,r FROM counts WHERE l>0 AND r>0 ORDER BY l*r DESC,display_key(key) LIMIT 10"
            )
        ]

    def valid_pair(self, left, right, how):
        if left is not None and right is not None:
            return (
                self.db.execute(
                    "SELECT 1 FROM source l JOIN source r ON r.key=l.key AND r.side='right' WHERE l.side='left' AND l.row_id=? AND r.row_id=?",
                    (left, right),
                ).fetchone()
                is not None
            )
        if left is not None and how in ("left", "outer"):
            return (
                self.db.execute(
                    "SELECT 1 FROM source s WHERE side='left' AND row_id=? AND NOT EXISTS(SELECT 1 FROM source r WHERE r.side='right' AND r.key=s.key)",
                    (left,),
                ).fetchone()
                is not None
            )
        if right is not None and how in ("right", "outer"):
            return (
                self.db.execute(
                    "SELECT 1 FROM source s WHERE side='right' AND row_id=? AND NOT EXISTS(SELECT 1 FROM source l WHERE l.side='left' AND l.key=s.key)",
                    (right,),
                ).fetchone()
                is not None
            )
        return False

    def records(self, how, columns):
        join = "LEFT JOIN" if how in ("left", "outer") else "JOIN"
        sql = f"SELECT l.row_id,r.row_id,l.body,r.body FROM source l {join} source r ON r.side='right' AND r.key=l.key WHERE l.side='left' ORDER BY l.row_id,r.row_id"
        cursors = [self.db.execute(sql)]
        if how in ("right", "outer"):
            cursors.append(
                self.db.execute(
                    "SELECT NULL,r.row_id,NULL,r.body FROM source r WHERE r.side='right' AND NOT EXISTS(SELECT 1 FROM source l WHERE l.side='left' AND l.key=r.key) ORDER BY r.row_id"
                )
            )
        for cursor in cursors:
            for left, right, left_json, right_json in cursor:
                lvalues = json.loads(left_json) if left_json else {}
                rvalues = json.loads(right_json) if right_json else {}
                merge = (
                    "both"
                    if left is not None and right is not None
                    else "left_only"
                    if left is not None
                    else "right_only"
                )
                values = {}
                for column in columns:
                    role = column["role"]
                    if role == "key":
                        value = lvalues.get(str(column["left_col_id"]))
                        if value is None:
                            value = rvalues.get(str(column["right_col_id"]))
                        if column["type"] == "text" and value is not None:
                            value = str(value)
                    elif role == "indicator":
                        value = merge
                    else:
                        value = (lvalues if role == "left" else rvalues).get(
                            str(column["source_col_id"])
                        )
                    values[column["name"]] = value
                yield left, right, values
