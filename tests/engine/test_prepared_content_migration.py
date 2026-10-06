from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.extraction_layouts import (
    get_layout,
    remember_success,
    save_layout,
    selected_layout_id,
)
from frisket.engine.store.prepared_content_migration import (
    PREPARED_CONTENT_FROM_DIGEST,
    PREPARED_CONTENT_TO_DIGEST,
    migrate_prepared_content,
)
from frisket.engine.store.schema import (
    SCALAR_CURRENT_CELL_VALUES_SQL,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_META_KEY,
)
from tests.engine.test_scalar_storage_migration import _seed_mixed_origins


_REF_KIND = ",\n    'prepared_content_ref'"
_REF_PAYLOAD_CHECK = (
    "\n    OR (value_kind='prepared_content_ref' "
    "AND typeof(value)='integer' AND value>0)"
)


def _seed_extraction_layout(
    path: Path, *, sheet_id: int, row_id: int
) -> tuple[int, int]:
    project = Project(path)
    source_column_id = project.add_column(sheet_id, "PDF", type="file")
    layout = save_layout(
        project,
        sheet_id=sheet_id,
        source="PDF",
        draft={"fields": [{"name": "Total", "type": "currency"}]},
        reference_row_id=row_id,
        repeat_group_id="line_items",
    )
    layout_id = int(layout["id"])
    remember_success(project, layout_id, [row_id], commit=True)
    project.close()
    return source_column_id, layout_id


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
        db.execute(SCALAR_CURRENT_CELL_VALUES_SQL)
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
    layout_source_id, layout_id = _seed_extraction_layout(
        path, sheet_id=sheet_id, row_id=row_ids[0]
    )
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
    layout = get_layout(project, layout_id)
    assert layout is not None
    assert layout["draft"] == {"fields": [{"name": "Total", "type": "currency"}]}
    assert layout["has_applied"] is True
    assert selected_layout_id(project, sheet_id, layout_source_id) == layout_id
    assert (
        project.db.execute(
            "SELECT layout_id FROM extraction_layout_documents "
            "WHERE source_column_id=? AND row_id=?",
            (layout_source_id, row_ids[0]),
        ).fetchone()[0]
        == layout_id
    )
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
