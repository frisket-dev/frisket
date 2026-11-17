"""The shared historical-step transaction: stamp on success, roll back on anything."""

from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store import Project
from frisket.engine.store.schema import BundleSchemaMismatch, SCHEMA_DIGEST_META_KEY
from frisket.upgrades.project._step import run_rebuild_step, run_step, schema_step

_FROM = "frisket.schema.v1:from"
_TO = "frisket.schema.v1:to"


def _stamped(tmp_path, digest: str = _FROM) -> sqlite3.Connection:
    db = sqlite3.connect(tmp_path / "project.db")
    db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES (?,?)", (SCHEMA_DIGEST_META_KEY, digest))
    db.commit()
    return db


def _digest(db: sqlite3.Connection) -> str:
    return db.execute(
        "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
    ).fetchone()[0]


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master")}


def test_step_applies_statements_and_stamps_its_endpoint(tmp_path):
    db = _stamped(tmp_path)
    run_step(db, _FROM, _TO, ("CREATE TABLE added (id INTEGER)",))
    assert _digest(db) == _TO
    assert "added" in _tables(db)
    assert not db.in_transaction


def test_interrupt_inside_a_step_rolls_back_ddl_and_stamp(tmp_path):
    db = _stamped(tmp_path)

    def interrupted(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE added (id INTEGER)")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_step(db, _FROM, _TO, interrupted)
    assert not db.in_transaction
    assert _digest(db) == _FROM
    assert "added" not in _tables(db)


def test_interrupt_inside_a_rebuild_step_restores_connection_pragmas(tmp_path):
    db = _stamped(tmp_path)
    db.execute("PRAGMA foreign_keys=ON")

    def interrupted(conn: sqlite3.Connection) -> None:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        conn.execute("CREATE TABLE added (id INTEGER)")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_rebuild_step(db, _FROM, _TO, interrupted)
    assert _digest(db) == _FROM
    assert "added" not in _tables(db)
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0


def test_step_already_completed_elsewhere_neither_applies_nor_restamps(tmp_path):
    db = _stamped(tmp_path, digest=_TO)
    with schema_step(db, _FROM, "frisket.schema.v1:other") as pending:
        assert pending is False
    assert _digest(db) == _TO
    assert not db.in_transaction


def test_unreadable_metadata_reaches_the_schema_fence_refusal(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    with sqlite3.connect(path / "project.db") as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(BundleSchemaMismatch, match="unstamped"):
        Project(path, maintenance=True)
