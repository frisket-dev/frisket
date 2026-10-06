from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.prepared_content_migration import (
    PREPARED_CONTENT_FROM_DIGEST,
    PREPARED_CONTENT_TO_DIGEST,
    migrate_prepared_content,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from tests.engine.test_scalar_storage_migration import _seed_mixed_origins


_REF_KIND = ",\n    'prepared_content_ref'"
_REF_PAYLOAD_CHECK = (
    "\n    OR (value_kind='prepared_content_ref' "
    "AND typeof(value)='integer' AND value>0)"
)

_PREDECESSOR_CURRENT_CELL_VALUES = """
CREATE VIEW current_cell_values AS
SELECT head.column_id,head.row_id,
       CASE WHEN head.inline_value_kind IS NOT NULL
       THEN head.inline_value_kind ELSE CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT source.value_kind FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE result.publication_effect
             WHEN 'publish_value' THEN result.value_kind
             WHEN 'publish_null' THEN 'null'
           END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT edit.value_kind FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END END AS value_kind,
       CASE WHEN head.inline_value_kind IS NOT NULL
       THEN head.inline_value ELSE CASE head.origin_kind
         WHEN 'source_cell' THEN (
           SELECT source.value FROM cells AS source
           WHERE source.row_id=head.row_id
             AND source.column_id=head.column_id
             AND source.producer_id IS head.base_producer_id
         )
         WHEN 'run_result' THEN (
           SELECT CASE WHEN result.publication_effect='publish_value'
             THEN result.value END
           FROM results AS result
           WHERE result.run_id=head.origin_run_id
             AND result.row_id=head.row_id
             AND result.column_id=head.column_id
         )
         WHEN 'manual_edit' THEN (
           SELECT edit.value FROM edits AS edit
           WHERE edit.op_id=head.origin_op_id
             AND edit.row_id=head.row_id
             AND edit.column_id=head.column_id
         )
       END END AS value,
       head.origin_kind,head.origin_op_id,head.origin_run_id,
       head.base_producer_id,head.validity
FROM current_cells AS head INDEXED BY idx_current_cells_column_row
"""


def _downgrade_to_exact_prepared_content_predecessor(path: Path) -> None:
    """Remove exactly the objects added by the prepared-content migration."""

    with sqlite3.connect(path / "project.db") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("PRAGMA legacy_alter_table=ON")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DROP VIEW current_cell_values")

        authority_objects = db.execute(
            "SELECT type,sql FROM sqlite_master "
            "WHERE tbl_name IN ('cells','results','edits') "
            "AND type IN ('index','trigger') AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END,name"
        ).fetchall()
        for table in ("cells", "results", "edits"):
            row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            current_ddl = str(row[0])
            assert current_ddl.count(_REF_KIND) == 1
            assert current_ddl.count(_REF_PAYLOAD_CHECK) == 1
            prior_ddl = current_ddl.replace(_REF_KIND, "").replace(
                _REF_PAYLOAD_CHECK, ""
            )
            db.execute(
                prior_ddl.replace(
                    f"CREATE TABLE {table}",
                    f"CREATE TABLE {table}_prepared_prior",
                    1,
                )
            )
            columns = [
                str(info[1]) for info in db.execute(f"PRAGMA table_info({table})")
            ]
            joined = ",".join(columns)
            db.execute(
                f"INSERT INTO {table}_prepared_prior({joined}) "
                f"SELECT {joined} FROM {table}"
            )
        for table in ("cells", "results", "edits"):
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {table}_prepared_prior RENAME TO {table}")
        for _object_type, statement in authority_objects:
            db.execute(str(statement))

        db.execute("DROP VIEW prepared_content_ref_values")
        for table in (
            "prepared_content_refs",
            "prepared_content_set_pages",
            "prepared_content_sets",
            "prepared_page_versions",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute(_PREDECESSOR_CURRENT_CELL_VALUES)
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (PREPARED_CONTENT_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.commit()


def _assert_exact_predecessor(db: sqlite3.Connection) -> None:
    assert (
        db.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()[0]
        == PREPARED_CONTENT_FROM_DIGEST
    )
    objects = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'prepared_content_%' "
            "OR name='prepared_page_versions'"
        )
    }
    assert objects == set()
    for table in ("cells", "results", "edits"):
        sql = str(
            db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
        )
        assert "prepared_content_ref" not in sql
    current_view = str(
        db.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='view' AND name='current_cell_values'"
        ).fetchone()[0]
    )
    assert "prepared_content_ref" not in current_view


def test_exact_prior_schema_preserves_values_history_undo_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "prior.frisket"
    sheet_id, source_column_id, output_column_id, row_ids = _seed_mixed_origins(path)
    _downgrade_to_exact_prepared_content_predecessor(path)
    with sqlite3.connect(path / "project.db") as db:
        _assert_exact_predecessor(db)

    project = Project(path)
    assert PREPARED_CONTENT_TO_DIGEST == SCHEMA_DIGEST
    assert project.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert project.get_values(sheet_id, source_column_id) == {
        row_ids[0]: 10,
        row_ids[1]: 20,
        row_ids[2]: 30,
    }
    assert project.get_values(sheet_id, output_column_id) == {
        row_ids[0]: "generated",
        row_ids[1]: None,
        row_ids[2]: "manual",
    }
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 3
    assert project.db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1
    history = project.history()
    assert [str(row["kind"]) for row in history] == [
        "add_rows",
        "map.regex_extract",
        "edit",
    ]
    edit_op_id = int(history[-1]["id"])
    assert project.undo() == edit_op_id
    assert project.get_values(sheet_id, output_column_id)[row_ids[2]] == "before edit"
    project.close()

    reopened = Project(path)
    assert reopened.get_values(sheet_id, output_column_id)[row_ids[2]] == "before edit"
    assert reopened.redo() == edit_op_id
    assert reopened.get_values(sheet_id, output_column_id)[row_ids[2]] == "manual"
    assert reopened.db.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


def test_exact_prior_schema_rolls_back_and_retries_after_interrupted_stamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "interrupted.frisket"
    sheet_id, _, output_column_id, row_ids = _seed_mixed_origins(path)
    _downgrade_to_exact_prepared_content_predecessor(path)
    with sqlite3.connect(path / "project.db") as db:
        db.execute(
            "CREATE TRIGGER interrupt_prepared_content_stamp BEFORE UPDATE ON meta "
            "WHEN NEW.key='schema_digest' AND NEW.value<>OLD.value BEGIN "
            "SELECT RAISE(ABORT,'migration interrupted'); END"
        )

    with sqlite3.connect(path / "project.db") as db:
        with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
            migrate_prepared_content(db, bundle_path=path / "project.db")
        _assert_exact_predecessor(db)
        assert db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT value FROM current_cell_values WHERE row_id=? AND column_id=?",
                (row_ids[2], output_column_id),
            ).fetchone()[0]
            == "manual"
        )
        assert [
            str(row[0]) for row in db.execute("SELECT kind FROM ops ORDER BY id")
        ] == [
            "add_rows",
            "map.regex_extract",
            "edit",
        ]
        db.execute("DROP TRIGGER interrupt_prepared_content_stamp")

    migrated = Project(path)
    assert migrated.get_values(sheet_id, output_column_id)[row_ids[2]] == "manual"
    assert migrated.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    migrated.close()
