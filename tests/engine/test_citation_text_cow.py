from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from frisket.engine.store.citation_text import (
    bind_text_ref,
    capture_text_context,
    freeze_text_ref,
    install_citation_text_schema,
    migrate_legacy_citation_texts,
    read_text_ref,
    text_ref_for_artifact,
)
from frisket.engine.store.evidence import (
    _artifact_text_context,
    record_source_artifact,
)


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE sheets(id INTEGER PRIMARY KEY);
        CREATE TABLE rows(
          id INTEGER PRIMARY KEY,
          sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE
        );
        CREATE TABLE columns(id INTEGER PRIMARY KEY);
        CREATE TABLE base_cell_producers(id INTEGER PRIMARY KEY);
        CREATE TABLE ops(id INTEGER PRIMARY KEY);
        CREATE TABLE runs(
          id INTEGER PRIMARY KEY,
          sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE
        );
        CREATE TABLE cells(
          row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
          column_id INTEGER NOT NULL,
          value_kind TEXT NOT NULL,
          value,
          producer_id INTEGER REFERENCES base_cell_producers(id),
          UNIQUE(row_id,column_id)
        );
        CREATE TABLE results(
          run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
          row_id INTEGER NOT NULL,
          column_id INTEGER NOT NULL,
          value_kind TEXT NOT NULL,
          value,
          PRIMARY KEY(run_id,row_id,column_id)
        ) WITHOUT ROWID;
        CREATE TABLE edits(
          op_id INTEGER NOT NULL REFERENCES ops(id) ON DELETE CASCADE,
          row_id INTEGER NOT NULL,
          column_id INTEGER NOT NULL,
          value_kind TEXT NOT NULL,
          value,
          PRIMARY KEY(op_id,row_id,column_id)
        ) WITHOUT ROWID;
        CREATE TABLE source_artifacts(
          id INTEGER PRIMARY KEY,
          stable_id TEXT NOT NULL UNIQUE,
          artifact_kind TEXT NOT NULL,
          media_type TEXT NOT NULL,
          blob_hash TEXT,
          source_url TEXT,
          canonical_url TEXT,
          title TEXT,
          filename TEXT,
          page_count INTEGER,
          duration_ms INTEGER,
          source_sheet_id INTEGER,
          source_row_id INTEGER,
          source_column_id INTEGER,
          external_ref_json TEXT NOT NULL DEFAULT '{}',
          metadata TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE VIEW current_cell_values AS
        SELECT cell.column_id,cell.row_id,cell.value_kind,cell.value,
               'source_cell' AS origin_kind,NULL AS origin_op_id,
               NULL AS origin_run_id,cell.producer_id AS base_producer_id,
               'valid' AS validity
        FROM cells cell;
        INSERT INTO sheets VALUES (1);
        INSERT INTO rows VALUES (10,1);
        INSERT INTO rows VALUES (11,1);
        INSERT INTO columns VALUES (20);
        INSERT INTO base_cell_producers VALUES (30);
        INSERT INTO base_cell_producers VALUES (31);
        INSERT INTO ops VALUES (40);
        INSERT INTO runs VALUES (50,1);
        """
    )
    install_citation_text_schema(db)
    db.commit()
    return db


def _artifact(db: sqlite3.Connection, artifact_id: int, metadata: str = "{}") -> None:
    db.execute(
        "INSERT INTO source_artifacts "
        "(id,stable_id,artifact_kind,media_type,source_sheet_id,source_row_id,"
        "source_column_id,metadata) VALUES (?,?, 'text','text/plain',1,10,20,?)",
        (artifact_id, f"artifact:{artifact_id}", metadata),
    )


def test_live_source_text_is_not_copied_until_update_and_rollback_restores_ref() -> (
    None
):
    db = _database()
    db.execute("INSERT INTO cells VALUES (10,20,'text','before',30)")
    _artifact(db, 1)
    ref = freeze_text_ref(
        db,
        text="before",
        value_ref={"kind": "source_cell", "row_id": 10, "column_id": 20},
        sheet_id=1,
        row_id=10,
        column_id=20,
    )
    bind_text_ref(db, source_artifact_id=1, ref=ref)
    db.commit()

    assert db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 0
    assert read_text_ref(db, text_ref_for_artifact(db, 1)) == "before"

    db.execute("BEGIN")
    db.execute("UPDATE cells SET value='after' WHERE row_id=10 AND column_id=20")
    assert db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 1
    assert read_text_ref(db, text_ref_for_artifact(db, 1)) == "before"
    db.rollback()

    rolled_back = text_ref_for_artifact(db, 1)
    assert rolled_back is not None and rolled_back.frozen_hash is None
    assert db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 0

    db.execute("UPDATE cells SET value='after' WHERE row_id=10 AND column_id=20")
    db.commit()
    frozen = text_ref_for_artifact(db, 1)
    assert frozen is not None and frozen.frozen_hash == _hash("before")
    assert read_text_ref(db, frozen) == "before"


@pytest.mark.parametrize("authority", ["source_cell", "run_result", "manual_edit"])
def test_every_native_authority_freezes_before_delete(authority: str) -> None:
    db = _database()
    _artifact(db, 1)
    if authority == "source_cell":
        db.execute("INSERT INTO cells VALUES (10,20,'text','saved',30)")
        value_ref = {"kind": authority}
        delete = "DELETE FROM cells WHERE row_id=10 AND column_id=20"
    elif authority == "run_result":
        db.execute("INSERT INTO results VALUES (50,10,20,'text','saved')")
        value_ref = {"kind": authority, "run_id": 50}
        delete = "DELETE FROM results WHERE run_id=50 AND row_id=10 AND column_id=20"
    else:
        db.execute("INSERT INTO edits VALUES (40,10,20,'text','saved')")
        value_ref = {"kind": authority, "op_id": 40}
        delete = "DELETE FROM edits WHERE op_id=40 AND row_id=10 AND column_id=20"
    capture_text_context(
        db,
        source_artifact_id=1,
        text="saved",
        value_ref=value_ref,
        sheet_id=1,
        row_id=10,
        column_id=20,
    )

    db.execute(delete)

    assert read_text_ref(db, text_ref_for_artifact(db, 1)) == "saved"
    assert db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 1


def test_parent_cascade_and_replace_both_freeze_the_incumbent_value() -> None:
    db = _database()
    db.execute("INSERT INTO cells VALUES (10,20,'text','cascade',30)")
    _artifact(db, 1)
    capture_text_context(
        db,
        source_artifact_id=1,
        text="cascade",
        value_ref={"kind": "source_cell"},
        sheet_id=1,
        row_id=10,
        column_id=20,
    )
    db.execute("DELETE FROM rows WHERE id=10")
    assert read_text_ref(db, text_ref_for_artifact(db, 1)) == "cascade"

    db.execute("INSERT INTO rows VALUES (10,1)")
    db.execute("INSERT INTO cells VALUES (10,20,'text','replace',30)")
    _artifact(db, 2)
    capture_text_context(
        db,
        source_artifact_id=2,
        text="replace",
        value_ref={"kind": "source_cell"},
        sheet_id=1,
        row_id=10,
        column_id=20,
    )
    db.execute("INSERT OR REPLACE INTO cells VALUES (10,20,'text','new',31)")
    assert read_text_ref(db, text_ref_for_artifact(db, 2)) == "replace"


def test_same_text_freezes_once_and_mismatch_freezes_immediately() -> None:
    db = _database()
    db.executemany("INSERT INTO cells VALUES (?,20,'text','same',30)", [(10,), (11,)])
    for artifact_id, row_id in ((1, 10), (2, 11)):
        _artifact(db, artifact_id)
        capture_text_context(
            db,
            source_artifact_id=artifact_id,
            text="same",
            value_ref={"kind": "source_cell"},
            sheet_id=1,
            row_id=row_id,
            column_id=20,
        )
    db.execute("UPDATE cells SET value='changed'")
    assert db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 1

    _artifact(db, 3)
    mismatched = capture_text_context(
        db,
        source_artifact_id=3,
        text="transformed",
        value_ref={"kind": "source_cell"},
        sheet_id=1,
        row_id=10,
        column_id=20,
    )
    assert mismatched.frozen_hash == _hash("transformed")


def test_source_producer_identity_prevents_rebinding_equal_replacement_text() -> None:
    db = _database()
    db.execute("INSERT INTO cells VALUES (10,20,'text','same',31)")
    _artifact(db, 1)

    ref = capture_text_context(
        db,
        source_artifact_id=1,
        text="same",
        value_ref={"kind": "source_cell", "producer_id": 30},
        sheet_id=1,
        row_id=10,
        column_id=20,
    )

    assert ref.frozen_hash == _hash("same")


def test_hash_collision_aborts_without_binding_context() -> None:
    db = _database()
    _artifact(db, 1)
    db.execute(
        "INSERT INTO citation_texts(content_hash,text) VALUES (?,?)",
        (_hash("wanted"), "different"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="hash collision"):
        capture_text_context(
            db,
            source_artifact_id=1,
            text="wanted",
            value_ref=None,
            sheet_id=None,
            row_id=None,
            column_id=None,
        )
    assert text_ref_for_artifact(db, 1) is None


class _Project:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.path = Path("project.frisket")


def test_artifact_writer_strips_inline_text_and_viewer_keeps_utf16_ranges() -> None:
    db = _database()
    db.execute("INSERT INTO cells VALUES (10,20,'text','A😀B',30)")
    project = _Project(db)
    artifact = record_source_artifact(
        project,
        artifact_kind="text",
        media_type="text/plain",
        source_sheet_id=1,
        source_row_id=10,
        source_column_id=20,
        metadata={"captured_text": "A😀B", "label": "input"},
    )

    assert "captured_text" not in artifact["metadata"]
    assert db.execute("SELECT COUNT(*) FROM citation_texts").fetchone()[0] == 0
    context = _artifact_text_context(
        project,
        {
            "id": artifact["id"],
            "metadata": artifact["metadata"],
            "spans": [
                {
                    "stable_id": "span:1",
                    "selector": {"char_start": 1, "char_end": 2},
                    "text_layer_hash": _hash("A😀B"),
                }
            ],
        },
    )
    assert context == {
        "text": "A😀B",
        "offset_unit": "utf16_code_unit",
        "ranges": [{"span_id": "span:1", "start": 1, "end": 3}],
    }


def test_legacy_migration_binds_verified_live_text_and_freezes_old_text() -> None:
    db = _database()
    db.execute("INSERT INTO cells VALUES (10,20,'text','live',30)")
    _artifact(db, 1, json.dumps({"captured_text": "live", "kept": True}))
    _artifact(db, 2, json.dumps({"captured_text": "old", "kept": True}))

    migrate_legacy_citation_texts(db)

    metadata = [
        json.loads(row[0])
        for row in db.execute("SELECT metadata FROM source_artifacts ORDER BY id")
    ]
    assert metadata == [{"kept": True}, {"kept": True}]
    live = text_ref_for_artifact(db, 1)
    old = text_ref_for_artifact(db, 2)
    assert live is not None and live.frozen_hash is None
    assert old is not None and old.frozen_hash == _hash("old")
    assert read_text_ref(db, live) == "live"
    assert read_text_ref(db, old) == "old"
