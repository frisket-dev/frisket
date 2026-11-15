"""Observed source baselines and validated keyed input, independent of Undo.

Writers publish through normal cell/membership owners. This module decides which
fields changed and keeps observations/checkpoints in the caller's transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

UpdatePolicy = Literal["keep_changes", "incoming_wins"]

KEYED_UPDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS keyed_relationships (
  id TEXT PRIMARY KEY,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  key_definition TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keyed_observations (
  relationship_id TEXT NOT NULL REFERENCES keyed_relationships(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  record TEXT NOT NULL,
  record_hash TEXT NOT NULL,
  PRIMARY KEY(relationship_id,row_id)
);
CREATE INDEX IF NOT EXISTS idx_keyed_observation_key
 ON keyed_observations(relationship_id,key);
CREATE TABLE IF NOT EXISTS keyed_publications (
  op_id INTEGER PRIMARY KEY REFERENCES ops(id) ON DELETE CASCADE,
  relationship_id TEXT NOT NULL REFERENCES keyed_relationships(id) ON DELETE CASCADE,
  input_hash TEXT NOT NULL,
  cursor INTEGER NOT NULL DEFAULT 0 CHECK(cursor>=0),
  completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1))
);
"""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def typed_equal(left: Any, right: Any) -> bool:
    return canonical(left) == canonical(right)


def typed_key(values: Sequence[tuple[str, Any]]) -> str | None:
    if any(
        value is None or isinstance(value, str) and not value.strip()
        for _, value in values
    ):
        return None
    return canonical(values)


@dataclass(frozen=True)
class KeyedRecord:
    ordinal: int
    key: str | None
    record: dict[str, Any]
    conflicting: bool
    copies: int


class ValidatedKeyedInput:
    """Bounded-memory index; nothing can publish before complete enumeration.

    The temporary index is rebuilt from immutable intake on process restart;
    publication resumes at the durable cursor only if its complete digest matches.
    It is not another retained source corpus.
    """

    def __init__(self):
        self._directory = tempfile.TemporaryDirectory(prefix="frisket-keyed-")
        self.db = sqlite3.connect(Path(self._directory.name) / "keys.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE records(ordinal INTEGER PRIMARY KEY,key TEXT UNIQUE,
          record TEXT NOT NULL,conflicting INTEGER NOT NULL DEFAULT 0,copies INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE targets(key TEXT NOT NULL,row_id INTEGER NOT NULL);
          CREATE INDEX target_key ON targets(key);
        """)
        self.sealed = False
        self.failed = False
        self.count = 0
        self.blank_keys = 0
        self._hash = hashlib.sha256()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.db.close()
        self._directory.cleanup()

    def stage(self, records: Iterable[tuple[str | None, Mapping[str, Any]]]) -> None:
        if self.sealed:
            raise ValueError("key domain already sealed")
        try:
            with self.db:
                for key, record in records:
                    self.count += 1
                    body = canonical(record)
                    self._hash.update(canonical([key, record]).encode() + b"\n")
                    if key is None:
                        self.blank_keys += 1
                    self.db.execute(
                        "INSERT INTO records(ordinal,key,record) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET copies=records.copies+1,"
                        "conflicting=records.conflicting OR records.record<>excluded.record",
                        (self.count, key, body),
                    )
        except BaseException:
            self.failed = True
            raise

    def seal(self) -> str:
        if self.failed:
            raise ValueError("key domain enumeration failed")
        self.sealed = True
        return self.input_hash

    @property
    def input_hash(self) -> str:
        if not self.sealed:
            raise ValueError("key domain enumeration is incomplete")
        return self._hash.hexdigest()

    def add_targets(self, records: Iterable[tuple[str | None, int]]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO targets(key,row_id) VALUES(?,?)",
                ((key, row) for key, row in records if key is not None),
            )

    def targets(self, key: str) -> list[int]:
        # Two is sufficient to report ambiguity; never load an unbounded duplicate set.
        return [
            int(row[0])
            for row in self.db.execute(
                "SELECT row_id FROM targets WHERE key=? LIMIT 2", (key,)
            )
        ]

    def records(
        self, *, after: int = 0, limit: int | None = None
    ) -> Iterable[KeyedRecord]:
        if not self.sealed:
            raise ValueError("key domain enumeration is incomplete")
        query = "SELECT * FROM records WHERE ordinal>? ORDER BY ordinal"
        params = [after]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        for row in self.db.execute(query, params):
            yield KeyedRecord(
                int(row["ordinal"]),
                row["key"],
                json.loads(row["record"]),
                bool(row["conflicting"]),
                int(row["copies"]),
            )


def require_relationship(
    db: sqlite3.Connection, *, relationship_id: str, sheet_id: int, key_definition: Any
) -> None:
    if not db.in_transaction:
        raise RuntimeError("keyed update requires a caller transaction")
    definition = canonical(key_definition)
    db.execute(
        "INSERT INTO keyed_relationships(id,sheet_id,key_definition) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING",
        (relationship_id, sheet_id, definition),
    )
    row = db.execute(
        "SELECT sheet_id,key_definition FROM keyed_relationships WHERE id=?",
        (relationship_id,),
    ).fetchone()
    if row is None or tuple(row) != (sheet_id, definition):
        raise ValueError("keyed relationship cannot change target or key definition")


def observation(
    db: sqlite3.Connection, relationship_id: str | None, row_id: int
) -> dict[str, Any] | None:
    if relationship_id is None:
        return None
    row = db.execute(
        "SELECT record FROM keyed_observations WHERE relationship_id=? AND row_id=?",
        (relationship_id, row_id),
    ).fetchone()
    return None if row is None else json.loads(row[0])


def retained_hidden_targets(
    db: sqlite3.Connection, relationship_id: str, key: str
) -> list[int]:
    rows = db.execute(
        "SELECT observation.row_id FROM keyed_observations observation JOIN rows r ON r.id=observation.row_id "
        "WHERE observation.relationship_id=? AND observation.key=? AND r.hidden=1 LIMIT 2",
        (relationship_id, key),
    ).fetchall()
    return [int(row[0]) for row in rows]


def changed_fields(
    current: Mapping[str, Any],
    incoming: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    policy: UpdatePolicy,
) -> dict[str, Any]:
    if policy not in ("keep_changes", "incoming_wins"):
        raise ValueError("unknown keyed update policy")
    prior = {} if baseline is None else baseline
    changes = {}
    for name, value in incoming.items():
        if name in prior and typed_equal(prior[name], value):
            continue
        if name in current and typed_equal(current[name], value):
            continue
        unchanged_locally = (
            name in prior
            and name in current
            and typed_equal(current[name], prior[name])
        )
        if policy == "incoming_wins" or name not in current or unchanged_locally:
            changes[name] = value
    return changes


def observe(
    db: sqlite3.Connection,
    *,
    relationship_id: str,
    row_id: int,
    key: str,
    incoming: Mapping[str, Any],
) -> None:
    if not db.in_transaction:
        raise RuntimeError("observation must commit with its publication")
    prior = observation(db, relationship_id, row_id) or {}
    prior.update(incoming)
    body = canonical(prior)
    db.execute(
        "INSERT INTO keyed_observations(relationship_id,row_id,key,record,record_hash) VALUES(?,?,?,?,?) "
        "ON CONFLICT(relationship_id,row_id) DO UPDATE SET key=excluded.key,record=excluded.record,record_hash=excluded.record_hash",
        (relationship_id, row_id, key, body, hashlib.sha256(body.encode()).hexdigest()),
    )


def publication_cursor(
    db: sqlite3.Connection,
    *,
    op_id: int,
    relationship_id: str,
    staged: ValidatedKeyedInput,
) -> int:
    if not db.in_transaction:
        raise RuntimeError("publication requires a caller transaction")
    db.execute(
        "INSERT INTO keyed_publications(op_id,relationship_id,input_hash) VALUES(?,?,?) ON CONFLICT(op_id) DO NOTHING",
        (op_id, relationship_id, staged.input_hash),
    )
    row = db.execute(
        "SELECT relationship_id,input_hash,cursor FROM keyed_publications WHERE op_id=?",
        (op_id,),
    ).fetchone()
    if tuple(row[:2]) != (relationship_id, staged.input_hash):
        raise ValueError("resumed keyed publication input changed")
    return int(row[2])


def checkpoint(
    db: sqlite3.Connection,
    *,
    op_id: int,
    previous: int,
    through: int,
    completed: bool = False,
) -> None:
    if not db.in_transaction or through < previous:
        raise ValueError("invalid keyed publication checkpoint")
    changed = db.execute(
        "UPDATE keyed_publications SET cursor=?,completed=? WHERE op_id=? AND cursor=? AND completed=0",
        (through, int(completed), op_id, previous),
    ).rowcount
    if changed != 1:
        raise ValueError("keyed publication checkpoint changed")
