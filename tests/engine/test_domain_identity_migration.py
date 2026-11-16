"""The breaking cutover must never reassign a surviving historical coordinate."""

from __future__ import annotations

import json
import gzip
from pathlib import Path
import sqlite3

import pytest

from frisket.upgrades.project.domain_identity_migration import (
    migrate_identity_high_water,
)


def _prior(path=":memory:"):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    # Real predecessor DDL stays fixed when the production schema changes.
    fixture = Path(__file__).parents[1] / "fixtures/domain-v0-schema.sql.gz"
    db.executescript(gzip.decompress(fixture.read_bytes()).decode())
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("INSERT INTO sheets(id,name,hidden) VALUES(4,'Existing',0)")
    db.executemany(
        "INSERT INTO columns(id,sheet_id,name,active,hidden,default_hidden) VALUES(?,4,?,?,?,?)",
        [
            (10, "visible", 1, 0, 0),
            (11, "user hidden", 1, 0, 1),
            (12, "inactive", 0, 1, 0),
            (13, "internal", 1, 1, 1),
        ],
    )
    db.executemany(
        "INSERT INTO rows(id,sheet_id,hidden) VALUES(?,4,?)", [(20, 0), (21, 1)]
    )
    db.execute(
        "INSERT INTO ops(id,kind,spec,undo_info) VALUES(1,'import',?,?)",
        (
            json.dumps(
                {
                    "scope": {"sheet_id": 90, "row_ids": [800, 900]},
                    "params": {"row_id": 999999},
                }
            ),
            json.dumps({"created_columns": [600], "created_sheets": [91]}),
        ),
    )
    db.commit()
    return db


def _begin(db):
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")
    db.execute("BEGIN IMMEDIATE")


def _commit(db):
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    db.commit()
    db.execute("PRAGMA legacy_alter_table=OFF")
    db.execute("PRAGMA foreign_keys=ON")


def test_preserves_identity_visibility_and_typed_historical_floors():
    db = _prior()
    before = [tuple(r) for r in db.execute("SELECT * FROM columns ORDER BY id")]
    _begin(db)
    floors = migrate_identity_high_water(db)
    _commit(db)
    assert floors["rows"] == 900  # arbitrary params.row_id was deliberately ignored
    assert floors["columns"] == 600
    assert floors["sheets"] == 91
    assert [tuple(r) for r in db.execute("SELECT * FROM columns ORDER BY id")] == before
    assert [tuple(r) for r in db.execute("SELECT id,hidden FROM rows ORDER BY id")] == [
        (20, 0),
        (21, 1),
    ]
    assert db.execute("SELECT spec FROM ops WHERE id=1").fetchone()[0] == json.dumps(
        {"scope": {"sheet_id": 90, "row_ids": [800, 900]}, "params": {"row_id": 999999}}
    )
    assert db.execute("INSERT INTO sheets(name) VALUES('new')").lastrowid == 92
    assert db.execute("INSERT INTO rows(sheet_id) VALUES(4)").lastrowid == 901
    assert (
        db.execute("INSERT INTO columns(sheet_id,name) VALUES(4,'new')").lastrowid
        == 601
    )
    # Restored active-name partial index still allows inactive-name reuse only.
    db.execute("INSERT INTO columns(sheet_id,name) VALUES(4,'inactive')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO columns(sheet_id,name) VALUES(4,'user hidden')")
    # A trigger recreated with the original table name still runs.
    assert db.execute(
        "SELECT 1 FROM search_dirty_scopes WHERE column_id=601"
    ).fetchone()


def test_empty_identity_tables_seed_from_retained_typed_refs():
    db = _prior()
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM rows")
    db.execute("DELETE FROM columns")
    db.execute("DELETE FROM sheets")
    db.execute(
        "INSERT INTO ops(kind,spec) VALUES('kept',?)",
        (
            json.dumps(
                {
                    "outputs": [
                        {
                            "ref": {
                                "prepared_ref_id": 1200,
                                "source_cell": [92, 1000, 700],
                            }
                        }
                    ]
                }
            ),
        ),
    )
    db.commit()
    _begin(db)
    floors = migrate_identity_high_water(db)
    _commit(db)
    assert floors["prepared_content_refs"] == 1200
    assert (
        db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='prepared_content_refs'"
        ).fetchone()[0]
        == 1200
    )
    assert db.execute("INSERT INTO sheets(name) VALUES('new')").lastrowid == 93
    assert (
        db.execute("INSERT INTO columns(sheet_id,name) VALUES(93,'new')").lastrowid
        == 701
    )
    assert db.execute("INSERT INTO rows(sheet_id) VALUES(93)").lastrowid == 1001


def test_allocation_does_not_reuse_after_deletion_and_repeated_helper():
    db = _prior()
    _begin(db)
    migrate_identity_high_water(db)
    _commit(db)
    row_id = db.execute("INSERT INTO rows(sheet_id) VALUES(4)").lastrowid
    db.execute("DELETE FROM rows WHERE id=?", (row_id,))
    db.commit()
    _begin(db)
    migrate_identity_high_water(db)
    _commit(db)
    assert db.execute("INSERT INTO rows(sheet_id) VALUES(4)").lastrowid > row_id


def test_interrupted_rebuild_rolls_back_without_partial_schema_or_data(tmp_path):
    path = tmp_path / "old.db"
    db = _prior(path)
    before = [tuple(r) for r in db.execute("SELECT * FROM columns ORDER BY id")]
    _begin(db)
    migrate_identity_high_water(db)
    # Closing without COMMIT models interrupted migration before its schema marker.
    db.close()
    db = sqlite3.connect(path)
    assert (
        "AUTOINCREMENT"
        not in db.execute("SELECT sql FROM sqlite_master WHERE name='rows'").fetchone()[
            0
        ]
    )
    assert db.execute("SELECT * FROM columns ORDER BY id").fetchall() == before
    _begin(db)
    migrate_identity_high_water(db)
    _commit(db)
    assert db.execute("INSERT INTO rows(sheet_id) VALUES(4)").lastrowid == 901


def test_requires_enclosing_migration_exclusion_and_transaction():
    db = _prior()
    with pytest.raises(RuntimeError, match="caller transaction"):
        migrate_identity_high_water(db)
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(RuntimeError, match="foreign keys disabled"):
        migrate_identity_high_water(db)
    db.rollback()


def test_consumed_operation_and_producer_identities_are_never_reused():
    db = _prior()
    db.execute(
        "UPDATE ops SET spec=? WHERE id=1",
        (json.dumps({"source": {"source_op_id": 900, "base_producer_id": 700}}),),
    )
    db.commit()
    _begin(db)
    migrate_identity_high_water(db)
    _commit(db)
    op = db.execute("INSERT INTO ops(kind) VALUES('edit')").lastrowid
    producer = db.execute(
        "INSERT INTO base_cell_producers(stage_id) VALUES('new')"
    ).lastrowid
    assert op == 901
    assert producer == 701
    db.execute("DELETE FROM ops WHERE id=?", (op,))
    db.execute("DELETE FROM base_cell_producers WHERE id=?", (producer,))
    assert db.execute("INSERT INTO ops(kind) VALUES('edit')").lastrowid == 902
    assert (
        db.execute("INSERT INTO base_cell_producers(stage_id) VALUES('next')").lastrowid
        == 702
    )


def test_view_and_watch_high_water_survives_shape_conversion_and_deletion():
    from frisket.upgrades.project.domain_view_migration import migrate_unified_views

    db = _prior()
    db.execute(
        "UPDATE ops SET spec=? WHERE id=1",
        (json.dumps({"source": {"view_id": 100, "watch_id": 200}}),),
    )
    db.execute("INSERT INTO views(id,sheet_id,name,spec) VALUES(7,4,'existing','{}')")
    db.execute("INSERT INTO lenses(id,sheet_id,name,spec) VALUES(1,4,'converted','{}')")
    db.commit()
    _begin(db)
    floors = migrate_identity_high_water(db)
    migrate_unified_views(db)
    _commit(db)
    assert floors["views"] == 100
    assert floors["watches"] == 200
    assert (
        db.execute("SELECT id FROM views WHERE name='converted'").fetchone()[0] == 101
    )
    assert db.execute("SELECT id FROM views WHERE name='existing'").fetchone()[0] == 7
    # Restoring an old explicit ID does not reset future allocation.
    db.execute("DELETE FROM views WHERE id=101")
    new_view = db.execute(
        "INSERT INTO views(sheet_id,name,spec) VALUES(4,'next','{}')"
    ).lastrowid
    assert new_view == 102
    watch = db.execute(
        "INSERT INTO watches(name,view_id,detection_policy) VALUES('next',?,'{}')",
        (new_view,),
    ).lastrowid
    assert watch == 201
    db.execute("DELETE FROM watches WHERE id=?", (watch,))
    assert (
        db.execute(
            "INSERT INTO watches(name,view_id,detection_policy) VALUES('next again',?,'{}')",
            (new_view,),
        ).lastrowid
        == 202
    )
