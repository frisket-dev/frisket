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
    # The cutover must install the same authority relation as a fresh project,
    # including presentation visibility facts rather than hiding source columns.
    from frisket.engine.store.document_text import DOCUMENT_TEXT_SELECTION_VIEW_SQL

    with sqlite3.connect(":memory:") as fresh:
        fresh.execute(DOCUMENT_TEXT_SELECTION_VIEW_SQL)
        expected = fresh.execute(
            "SELECT sql FROM sqlite_schema WHERE name='current_document_text_selections'"
        ).fetchone()[0]
    assert (
        db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='current_document_text_selections'"
        ).fetchone()[0]
        == expected
    )
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


@pytest.mark.parametrize(
    "details,params",
    [
        ("not JSON", "not JSON"),
        ("null", "null"),
        ("[]", "[]"),
        ("true", "42"),
        ('{"output_fields":null}', '{"output_names":null}'),
        ('{"output_fields":42}', '{"output_names":[]}'),
        ('{"output_fields":[null,42,"wrong"]}', '{"output_names":"wrong"}'),
        ('{"output_fields":{"name":"Text","column_type":"text"}}', "{}"),
        ("{}", '{"output_names":{"a":"Text","b":"Text"}}'),
    ],
)
def test_cutover_retains_data_when_optional_output_metadata_is_malformed(
    old_db, details, params
):
    from frisket.engine.store.domain_migration import migrate_domain, DOMAIN_TO_DIGEST
    from frisket.engine.store.schema import SCHEMA_DIGEST_META_KEY

    db = old_db
    db.execute(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind,status,params) "
        "VALUES (1,1,1,'map.ask','completed',?)",
        (params,),
    )
    db.execute(
        "INSERT INTO run_output_generations(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token) "
        "VALUES (1,1,'Text','text-v1','create','active','old-claim')"
    )
    db.execute(
        "INSERT INTO output_column_claims(id,sheet_id,column_id,output_name,run_id,action_kind,claim_token,status,details) "
        "VALUES ('old',1,1,'Text',1,'map.ask','old-claim','completed',?)",
        (details,),
    )
    _stamp_old(db)
    migrate_domain(db)
    assert tuple(
        db.execute(
            "SELECT output_role,output_descriptor_json FROM run_output_generations"
        ).fetchone()
    ) == ("Text", "{}")
    assert (
        db.execute("SELECT value FROM current_cell_values").fetchone()[0] == "corrected"
    )
    assert db.execute("SELECT params FROM runs WHERE id=1").fetchone()[0] == params
    assert (
        db.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()[0]
        == DOMAIN_TO_DIGEST
    )
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_released_runs_migrate_as_singleton_execution_identities(old_db):
    from types import SimpleNamespace
    from frisket.engine.store.domain_migration import migrate_domain
    from frisket.engine.store.execution_groups import execution_runs

    db = old_db
    db.executemany(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind,params,status) "
        "VALUES (?,1,1,'map.ask',?,'completed')",
        [(1, "{}"), (2, '{"execution_root_run_id":1,"question":"historical data"}')],
    )
    _stamp_old(db)
    migrate_domain(db)
    project = SimpleNamespace(db=db)
    assert execution_runs(project, 1) == (1, [1])
    assert execution_runs(project, 2) == (2, [2])
    assert [row[0] for row in db.execute("SELECT execution_root_run_id FROM runs")] == [
        None,
        None,
    ]
    assert (
        json.loads(db.execute("SELECT params FROM runs WHERE id=2").fetchone()[0])[
            "question"
        ]
        == "historical data"
    )
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def _check_constraints(sql):
    """Normalize CHECK expressions without confusing quoted strings/comments."""
    import re

    tokens = re.findall(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/|[A-Za-z_]\w*|[^\s]",
        sql,
        re.S,
    )
    tokens = [
        token if token.startswith(("'", '"')) else token.lower()
        for token in tokens
        if not token.startswith(("--", "/*"))
    ]
    checks = []
    for index, token in enumerate(tokens):
        if token != "check" or tokens[index + 1] != "(":
            continue
        depth = 0
        for end in range(index + 1, len(tokens)):
            depth += (tokens[end] == "(") - (tokens[end] == ")")
            if depth == 0:
                checks.append("".join(tokens[index + 2 : end]))
                break
        else:
            raise AssertionError("Unterminated CHECK")
    return sorted(checks)


def test_migrated_schema_matches_fresh_columns_and_constraints(old_db):
    from frisket.engine.store.domain_migration import migrate_domain
    from frisket.engine.store.schema import SCHEMA

    _stamp_old(old_db)
    migrate_domain(old_db)
    fresh = sqlite3.connect(":memory:")
    try:
        fresh.executescript(SCHEMA)
        tables = dict(
            fresh.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        migrated = dict(
            old_db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        assert migrated.keys() == tables.keys()
        for table, sql in tables.items():
            assert _check_constraints(migrated[table]) == _check_constraints(sql), table
            before = {
                row[1]: tuple(row[2:])
                for row in old_db.execute(f'PRAGMA table_info("{table}")')
            }
            after = {
                row[1]: tuple(row[2:])
                for row in fresh.execute(f'PRAGMA table_info("{table}")')
            }
            # ADD COLUMN retains these historical representation differences.
            # Neither changes existing rows or the typed writer's required data.
            if table == "watches":
                name = "view_id"
                assert before[name][1] == 0 and after[name][1] == 1
                before[name] = (before[name][0], 1, *before[name][2:])
            assert before == after, table
            assert sorted(
                tuple(row[2:])
                for row in old_db.execute(f'PRAGMA foreign_key_list("{table}")')
            ) == sorted(
                tuple(row[2:])
                for row in fresh.execute(f'PRAGMA foreign_key_list("{table}")')
            ), table
    finally:
        fresh.close()


def test_results_rebuild_retains_data_indexes_triggers_and_accepts_retained_outcomes(
    old_db,
):
    from frisket.engine.store.domain_migration import migrate_domain

    db = old_db
    db.execute(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind,status) VALUES (1,1,1,'map.ask','completed')"
    )
    db.execute(
        "INSERT INTO results(run_id,row_id,column_id,value_kind,value,publication_effect) VALUES (1,1,1,'text','saved',NULL)"
    )
    db.execute("CREATE INDEX result_probe_index ON results(review_note)")
    db.execute(
        "CREATE TRIGGER result_probe_trigger AFTER UPDATE OF review_note ON results BEGIN UPDATE meta SET value=NEW.review_note WHERE key='probe'; END"
    )
    db.execute("INSERT INTO meta(key,value) VALUES('probe','old')")
    objects = dict(
        db.execute(
            "SELECT name,sql FROM sqlite_master WHERE tbl_name='results' AND type IN ('index','trigger') AND sql IS NOT NULL"
        )
    )
    _stamp_old(db)
    migrate_domain(db)
    assert db.execute("SELECT value FROM results").fetchone()[0] == "saved"
    for name, sql in objects.items():
        statement = db.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()[0]
        if name == "trg_results_published_no_delete":
            assert "frisket_purge_result_delete_authorized" in statement
        else:
            assert statement == sql
    db.execute("UPDATE results SET review_note='still works' WHERE run_id=1")
    assert (
        db.execute("SELECT value FROM meta WHERE key='probe'").fetchone()[0]
        == "still works"
    )
    db.execute("INSERT INTO ops(id,kind) VALUES(3,'map.ask')")
    db.execute(
        "INSERT INTO runs(id,op_id,sheet_id,action_kind) VALUES(2,3,1,'map.ask')"
    )
    db.execute(
        "INSERT INTO run_output_generations(run_id,column_id,output_role,output_descriptor_json,compatibility_key,write_mode,state,claim_token) VALUES(2,1,'answer','{}','v1','replace_scope','active','test')"
    )
    db.execute(
        "INSERT INTO results(run_id,row_id,column_id,value_kind,value,publication_effect) VALUES(2,1,1,'text','retained','retain')"
    )
    for kind, value, error in [
        ("text", "retained answer", None),
        ("null", None, None),
        (None, None, "retained failure"),
    ]:
        db.execute(
            "UPDATE results SET value_kind=?,value=?,error=?,publication_effect='retain' WHERE run_id=2",
            (kind, value, error),
        )
        assert tuple(
            db.execute(
                "SELECT value_kind,value,error,publication_effect FROM results WHERE run_id=2"
            ).fetchone()
        ) == (kind, value, error, "retain")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db.execute(
            "UPDATE results SET value_kind='text',value='illegal',error='failure' WHERE run_id=2"
        )
