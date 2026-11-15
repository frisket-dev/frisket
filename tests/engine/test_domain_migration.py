"""Upgrade assertions use the frozen bd402937 schema, not current DDL."""

import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from frisket.engine.store.domain_migration import (
    archive_history,
    capture_current_baseline,
    install_current_baseline,
    retire_unfinished_work,
)
from frisket.engine.store.schema import BundleSchemaMismatch


@pytest.fixture
def old_db(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.row_factory = sqlite3.Row
    fixture = Path(__file__).parents[1] / "fixtures/domain-v0-schema.sql.gz"
    db.executescript(gzip.decompress(fixture.read_bytes()).decode())
    db.execute("INSERT INTO sheets(id,name) VALUES (1,'Source')")
    db.execute("INSERT INTO columns(id,sheet_id,name) VALUES (1,1,'Text')")
    db.execute("INSERT INTO rows(id,sheet_id) VALUES (1,1)")
    db.execute("INSERT INTO ops(id,kind) VALUES (1,'import')")
    db.execute("INSERT INTO ops(id,kind) VALUES (2,'edit')")
    db.execute("INSERT INTO base_cell_producers(id,stage_id,op_id) VALUES (1,'old',1)")
    db.execute(
        "INSERT INTO cells(row_id,column_id,value_kind,value,producer_id) "
        "VALUES (1,1,'text','source',1)"
    )
    db.execute(
        "INSERT INTO edits(op_id,row_id,column_id,value_kind,value) "
        "VALUES (2,1,1,'text','corrected')"
    )
    db.execute(
        "INSERT INTO current_cells(column_id,row_id,retrieval_revision,"
        "origin_kind,origin_op_id,validity) VALUES (1,1,1,'manual_edit',2,'valid')"
    )
    db.commit()
    yield db
    db.close()


def test_baseline_retains_winning_reference_not_a_payload_copy(old_db):
    db = old_db
    db.execute("BEGIN IMMEDIATE")
    capture_current_baseline(db)
    db.execute(
        "CREATE TABLE cell_baseline(row_id,column_id,origin_kind,origin_op_id,"
        "origin_run_id,base_producer_id,PRIMARY KEY(column_id,row_id,origin_kind))"
    )
    install_current_baseline(db)
    db.commit()
    assert tuple(
        db.execute(
            "SELECT * FROM cell_baseline WHERE origin_kind='manual_edit'"
        ).fetchone()
    ) == (
        1,
        1,
        "manual_edit",
        2,
        None,
        None,
    )
    assert db.execute("SELECT value FROM edits").fetchone()[0] == "corrected"
    assert db.execute("SELECT value FROM cells").fetchone()[0] == "source"
    assert not db.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE name='domain_baseline_capture'"
    ).fetchone()


def test_retirement_keeps_returned_work_and_completed_history(old_db):
    db = old_db
    db.execute(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind) VALUES (1,1,1,'map.ask')"
    )
    for identity, state, payload in (
        ("pending", "reserved", None),
        ("returned", "returned", '{"answer":"saved"}'),
        ("consumed", "consumed", '{"usage":42}'),
    ):
        db.execute(
            "INSERT INTO effect_checkpoints"
            "(id,family,group_key,unit_key,action_kind,identity,state,payload) "
            "VALUES (?,'model','run-1',?,'map.ask',?,?,?)",
            (identity, identity, identity, state, payload),
        )
    for identity, status in (("active", "running"), ("done", "completed")):
        db.execute(
            "INSERT INTO receipts(id,run_id,action_kind,status,body) VALUES (?,1,'map.ask',?,?)",
            (identity, status, json.dumps({"receipt_id": identity, "status": status})),
        )
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    retire_unfinished_work(db)
    db.commit()
    assert db.execute("SELECT status FROM runs").fetchone()[0] == "cancelled"
    assert dict(db.execute("SELECT id,status FROM receipts")) == {
        "active": "cancelled",
        "done": "completed",
    }
    assert dict(db.execute("SELECT id,payload FROM effect_checkpoints")) == {
        "returned": '{"answer":"saved"}',
        "consumed": '{"usage":42}',
    }
    body = json.loads(
        db.execute("SELECT body FROM receipts WHERE id='active'").fetchone()[0]
    )
    assert body["status"] == "cancelled"
    assert body["errors"][0]["code"] == "execution_cutover"


def test_bad_receipt_rolls_back_the_entire_upgrade(old_db):
    db = old_db
    db.execute(
        "INSERT INTO receipts(id,action_kind,status,body) VALUES ('bad','map.ask','running','{}')"
    )
    db.commit()
    with pytest.raises(BundleSchemaMismatch), db:
        db.execute("BEGIN IMMEDIATE")
        capture_current_baseline(db)
        db.execute("UPDATE cells SET value='must roll back'")
        retire_unfinished_work(db)
    assert db.execute("SELECT value FROM cells").fetchone()[0] == "source"
    assert db.execute("SELECT status FROM receipts").fetchone()[0] == "running"


def test_old_history_is_inspectable_but_not_navigable(old_db):
    db = old_db
    db.execute("ALTER TABLE ops ADD COLUMN change_order INTEGER")
    db.execute("ALTER TABLE ops ADD COLUMN publication_closed INTEGER DEFAULT 1")
    db.execute("ALTER TABLE ops ADD COLUMN redo_eligible INTEGER DEFAULT 1")
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    archive_history(db)
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1
    assert (
        db.execute(
            "SELECT value FROM meta WHERE key='domain_history_floor'"
        ).fetchone()[0]
        == "2"
    )
    assert all(
        row[0] is None and row[1:] == (1, 0)
        for row in (
            tuple(row)
            for row in db.execute(
                "SELECT change_order,publication_closed,redo_eligible FROM ops"
            )
        )
    )


def _stamp_old(db):
    from frisket.engine.store.domain_migration import DOMAIN_FROM_DIGEST

    db.execute(
        "INSERT INTO meta(key,value) VALUES ('schema_digest',?)", (DOMAIN_FROM_DIGEST,)
    )
    db.commit()


def test_full_cutover_preserves_values_and_starts_new_history(old_db):
    from frisket.engine.store.domain_migration import migrate_domain
    from frisket.engine.store.schema import SCHEMA_DIGEST

    db = old_db
    _stamp_old(db)
    db.execute("PRAGMA foreign_keys=ON")
    migrate_domain(db)
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
    assert (
        db.execute("SELECT value FROM meta WHERE key='schema_digest'").fetchone()[0]
        == SCHEMA_DIGEST
    )
    assert (
        db.execute(
            "SELECT value FROM current_cell_values WHERE column_id=1 AND row_id=1"
        ).fetchone()[0]
        == "corrected"
    )
    assert (
        db.execute(
            "SELECT origin_op_id FROM canonical_cell_heads WHERE column_id=1 AND row_id=1"
        ).fetchone()[0]
        == 2
    )
    assert db.execute("SELECT value FROM cells").fetchone()[0] == "source"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM ops WHERE change_order IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
    migrate_domain(db)  # Reopening does not recapture or reset new history.


def test_cutover_preserves_anonymous_source_identity(old_db):
    from frisket.engine.store.domain_migration import migrate_domain

    db = old_db
    db.execute("DELETE FROM edits")
    db.execute("UPDATE cells SET producer_id=NULL")
    db.execute(
        "UPDATE current_cells SET origin_kind='source_cell',origin_op_id=NULL,base_producer_id=NULL"
    )
    _stamp_old(db)
    migrate_domain(db)
    head = db.execute(
        "SELECT base_producer_id,value FROM current_cell_values"
    ).fetchone()
    assert head[0] is not None and head[1] == "source"
    assert (
        db.execute(
            "SELECT op_id FROM base_cell_producers WHERE id=?", (head[0],)
        ).fetchone()[0]
        is None
    )
    assert (
        db.execute("SELECT base_producer_id FROM cell_baseline").fetchone()[0]
        == head[0]
    )


def test_full_cutover_failure_restores_schema_and_pragmas(old_db):
    from frisket.engine.store.domain_migration import migrate_domain, DOMAIN_FROM_DIGEST

    db = old_db
    db.execute(
        "INSERT INTO receipts(id,action_kind,status,body) VALUES('bad','map.ask','running','{}')"
    )
    _stamp_old(db)
    db.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(BundleSchemaMismatch, match="malformed receipt"):
        migrate_domain(db)
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not db.in_transaction
    assert (
        db.execute("SELECT value FROM meta WHERE key='schema_digest'").fetchone()[0]
        == DOMAIN_FROM_DIGEST
    )
    assert "change_order" not in {r[1] for r in db.execute("PRAGMA table_info(ops)")}
    assert db.execute("SELECT value FROM edits").fetchone()[0] == "corrected"
    assert not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='cell_baseline'"
    ).fetchone()


def test_cutover_preserves_saved_answer_beneath_manual_correction(old_db):
    from frisket.engine.store.domain_migration import migrate_domain
    from frisket.engine.store.changes import start_change, ChangePublicationClosed

    db = old_db
    db.execute(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind,status) VALUES (1,1,1,'map.ask','completed')"
    )
    db.execute(
        "INSERT INTO run_output_generations(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) VALUES (1,1,'Text','text-v1','create','active','old-claim')"
    )
    db.execute(
        "INSERT INTO results(run_id,row_id,column_id,value_kind,value,outcome,publication_effect,review_state,review_decision) VALUES (1,1,1,'text','original answer','ok','publish_value','reviewed','accept')"
    )
    db.execute("INSERT INTO cell_result_heads(column_id,row_id,run_id) VALUES (1,1,1)")
    db.execute(
        "UPDATE runs SET params=? WHERE id=1",
        (json.dumps({"output_names": {"answer": "Text"}}),),
    )
    db.execute(
        "INSERT INTO output_column_claims(id,sheet_id,column_id,output_name,run_id,action_kind,claim_token,status,details) VALUES ('old',1,1,'Text',1,'map.ask','old-claim','completed',?)",
        (json.dumps({"output_fields": [{"name": "Text", "column_type": "text"}]}),),
    )
    _stamp_old(db)
    migrate_domain(db)
    assert (
        db.execute(
            "SELECT value FROM current_cell_values WHERE row_id=1 AND column_id=1"
        ).fetchone()[0]
        == "corrected"
    )
    assert (
        db.execute(
            "SELECT value FROM unmodified_cell_values WHERE row_id=1 AND column_id=1"
        ).fetchone()[0]
        == "original answer"
    )
    assert (
        db.execute(
            "SELECT run_id FROM current_result_heads WHERE row_id=1 AND column_id=1"
        ).fetchone()[0]
        == 1
    )
    assert tuple(
        db.execute("SELECT published,review_decision FROM results").fetchone()
    ) == (1, "accept")
    descriptor = db.execute(
        "SELECT output_role,output_descriptor_json FROM run_output_generations"
    ).fetchone()
    assert descriptor[0] == "answer"
    assert json.loads(descriptor[1]) == {"column_type": "text"}
    with db, pytest.raises(ChangePublicationClosed, match="Pre-upgrade history"):
        db.execute("BEGIN IMMEDIATE")
        start_change(db, 1, writing=True)


def test_cutover_normalizes_swapped_output_roles_atomically(old_db):
    from frisket.engine.store.domain_migration import migrate_domain

    db = old_db
    db.execute("INSERT INTO columns(id,sheet_id,name) VALUES (2,1,'Other')")
    db.execute(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind,status,params) VALUES (1,1,1,'map.extract','completed',?)",
        (json.dumps({"output_names": {"Text": "Other", "Other": "Text"}}),),
    )
    for column, physical in [(1, "Text"), (2, "Other")]:
        db.execute(
            "INSERT INTO run_output_generations(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) VALUES (1,?,?,'text-v1','create','active','old-claim')",
            (column, physical),
        )
    _stamp_old(db)
    migrate_domain(db)
    assert dict(
        db.execute("SELECT column_id,output_role FROM run_output_generations")
    ) == {1: "Other", 2: "Text"}
    # Missing frozen descriptors are explicitly unavailable, not guessed from
    # the surviving mutable column definitions.
    assert {
        row[0]
        for row in db.execute(
            "SELECT output_descriptor_json FROM run_output_generations"
        )
    } == {"{}"}


def _legacy_reapplied_result(db, copied_value, target_column_id=1):
    for run_id, value in ((5, copied_value), (6, "latest answer")):
        db.execute("INSERT INTO ops(id,kind) VALUES (?,'map.ask')", (run_id,))
        db.execute(
            "INSERT INTO runs(id,op_id,sheet_id,action_kind,status) VALUES (?,?,1,'map.ask','completed')",
            (run_id, run_id),
        )
        db.execute(
            "INSERT INTO run_output_generations(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) VALUES (?,1,'Text','text-v1','create','active',?)",
            (run_id, f"old:{run_id}"),
        )
        db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value,outcome,publication_effect,review_state,review_decision,review_note) "
            "VALUES (?,1,1,?,?, 'ok',?,'reviewed','accept','checked old answer')",
            (
                run_id,
                "null" if value is None else "text",
                value,
                "publish_null" if value is None else "publish_value",
            ),
        )
    db.execute("INSERT INTO cell_result_heads(column_id,row_id,run_id) VALUES (1,1,6)")
    db.execute(
        "INSERT INTO ops(id,kind,spec) VALUES (7,'edit',?)",
        (
            json.dumps(
                {
                    "action_kind": "run.reapply",
                    "source_run_id": 5,
                    "result_refs": [
                        {
                            "run_id": 5,
                            "row_id": 1,
                            "column_id": 1,
                            "target_column_id": target_column_id,
                        }
                    ],
                }
            ),
        ),
    )
    if target_column_id != 1:
        db.execute(
            "INSERT INTO columns(id,sheet_id,name) VALUES (?,1,'Copy')",
            (target_column_id,),
        )
    db.execute("DELETE FROM edits")
    db.execute(
        "INSERT INTO edits(op_id,row_id,column_id,value_kind,value) VALUES (7,1,?,?,?)",
        (target_column_id, "null" if copied_value is None else "text", copied_value),
    )
    db.execute(
        "UPDATE current_cells SET column_id=?,origin_op_id=7,validity=?",
        (target_column_id, "missing" if copied_value is None else "valid"),
    )


@pytest.mark.parametrize("copied_value", ["earlier answer", None])
@pytest.mark.parametrize("target_column_id", [1, 2])
def test_cutover_preserves_reapplied_older_result_identity(
    old_db, copied_value, target_column_id
):
    from frisket.engine.store.domain_migration import migrate_domain

    db = old_db
    _legacy_reapplied_result(db, copied_value, target_column_id)
    _stamp_old(db)
    migrate_domain(db)
    for view in (
        "current_cell_values",
        "unmodified_cell_values",
        "canonical_cell_heads",
    ):
        row = db.execute(
            f"SELECT origin_kind,origin_op_id,origin_run_id FROM {view} WHERE row_id=1 AND column_id=?",
            (target_column_id,),
        ).fetchone()
        assert tuple(row) == ("copied_result", 7, 5)
    assert (
        db.execute("SELECT value FROM current_cell_values").fetchone()[0]
        == copied_value
    )
    assert (
        db.execute("SELECT value FROM unmodified_cell_values").fetchone()[0]
        == copied_value
    )
    copy = db.execute(
        "SELECT source_run_id,source_row_id,source_column_id,review_state,review_decision,review_note FROM copied_result_outcomes"
    ).fetchone()
    assert tuple(copy) == (5, 1, 1, "reviewed", "accept", "checked old answer")
    assert (
        db.execute("SELECT value FROM results WHERE run_id=6").fetchone()[0]
        == "latest answer"
    )
    from types import SimpleNamespace
    from frisket.engine.store.cells import get_values_with_refs

    for apply_edits in (True, False):
        values, refs = get_values_with_refs(
            SimpleNamespace(db=db), 1, target_column_id, [1], apply_edits=apply_edits
        )
        assert values == {1: copied_value}
        assert refs[1]["retained_result"] == {"run_id": 5, "row_id": 1, "column_id": 1}
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize(
    "problem",
    [
        "missing_source",
        "wrong_target",
        "different_value",
        "duplicate",
        "malformed_entry",
        "malformed_spec",
    ],
)
def test_cutover_does_not_guess_an_old_reapply_reference(old_db, problem):
    from frisket.engine.store.domain_migration import migrate_domain

    db = old_db
    _legacy_reapplied_result(db, "earlier answer")
    spec = json.loads(db.execute("SELECT spec FROM ops WHERE id=7").fetchone()[0])
    if problem == "missing_source":
        spec["result_refs"][0]["run_id"] = 999
    elif problem == "wrong_target":
        spec["result_refs"][0]["target_column_id"] = 999
    elif problem == "different_value":
        db.execute("UPDATE edits SET value='manual adjustment' WHERE op_id=7")
    elif problem == "duplicate":
        spec["result_refs"] *= 2
    elif problem == "malformed_entry":
        spec["result_refs"] = [None, "invalid"]
    db.execute(
        "UPDATE ops SET spec=? WHERE id=7",
        ("{" if problem == "malformed_spec" else json.dumps(spec),),
    )
    _stamp_old(db)
    migrate_domain(db)
    assert db.execute("SELECT COUNT(*) FROM copied_result_outcomes").fetchone()[0] == 0
    assert tuple(
        db.execute("SELECT origin_kind,value FROM current_cell_values").fetchone()
    ) == (
        "manual_edit",
        "manual adjustment" if problem == "different_value" else "earlier answer",
    )


def test_cutover_reapply_preserves_a_prepared_text_reference(old_db):
    from frisket.engine.store.domain_migration import migrate_domain

    db = old_db
    _legacy_reapplied_result(db, "earlier answer")
    db.execute(
        "INSERT INTO prepared_page_versions(id,page_number,prepared_text,content_hash,producing_op_id) VALUES (1,1,'earlier answer',?,5)",
        ("sha256:" + "0" * 64,),
    )
    db.execute("INSERT INTO prepared_content_sets(id,producing_op_id) VALUES (1,5)")
    db.execute(
        "INSERT INTO prepared_content_set_pages(set_id,page_number,version_id) VALUES (1,1,1)"
    )
    db.execute(
        "INSERT INTO prepared_content_refs(id,set_id,page_number) VALUES (1,1,1)"
    )
    db.execute(
        "UPDATE results SET value_kind='prepared_content_ref',value=1 WHERE run_id=5"
    )
    _stamp_old(db)
    migrate_domain(db)
    assert tuple(
        db.execute(
            "SELECT origin_kind,value,prepared_ref_id FROM current_cell_values"
        ).fetchone()
    ) == (
        "copied_result",
        "earlier answer",
        1,
    )
    assert tuple(
        db.execute(
            "SELECT source_run_id,source_column_id FROM copied_result_outcomes"
        ).fetchone()
    ) == (5, 1)
    assert not db.execute("PRAGMA foreign_key_check").fetchall()
