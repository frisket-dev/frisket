from __future__ import annotations

import sqlite3

import pytest

from frisket.engine.store.project import Project
from frisket.engine.store.search_index_work import (
    SearchDirtyScope,
    ack_dirty_scope,
    advance_dirty_scope,
    enqueue_dirty_scope,
    latest_revision,
    read_dirty_scopes,
)

_PRE_SEARCH_DIGEST = "frisket.schema.v1:cb9a46c224c6d5e95af3c7e91df76e26"


def test_worklist_revision_progress_and_ack_survive_deletion(tmp_path) -> None:
    project = Project.create(tmp_path / "project", name="search-work")
    db = project.db
    db.execute("DELETE FROM search_dirty_scopes")
    db.commit()
    with db:
        db.execute("BEGIN")
        first = enqueue_dirty_scope(db)
        second = enqueue_dirty_scope(
            db, sheet_id=7, column_id=9, row_id_start=11, row_id_end=20
        )
    assert latest_revision(db) == second
    scopes = read_dirty_scopes(db, limit=2)
    assert [scope.id for scope in scopes[-2:]] == [first, second]
    assert scopes[-1].scan_cursor == "[0,0]"

    with db:
        db.execute("BEGIN")
        assert advance_dirty_scope(
            db,
            scope_id=second,
            expected_cursor="[0,0]",
            scan_cursor="[9,15]",
        )
        assert not ack_dirty_scope(db, scope_id=second, expected_cursor="[0,0]")
        assert ack_dirty_scope(db, scope_id=second, expected_cursor="[9,15]")
    assert latest_revision(db) == second


def test_worklist_mutations_require_authoritative_transaction(tmp_path) -> None:
    project = Project.create(tmp_path / "project", name="search-work")
    with pytest.raises(RuntimeError, match="caller transaction"):
        enqueue_dirty_scope(project.db)


@pytest.mark.parametrize("column_id", [None, 5])
def test_broader_scope_supersedes_narrow_work_with_fresh_revision(
    tmp_path, column_id
) -> None:
    project = Project.create(tmp_path / "project", name="search-work")
    db = project.db
    db.execute("DELETE FROM search_dirty_scopes")
    db.commit()
    db.execute("BEGIN")
    narrow = enqueue_dirty_scope(
        db, sheet_id=4, column_id=5, row_id_start=10, row_id_end=20
    )
    broad = enqueue_dirty_scope(db, sheet_id=4, column_id=column_id)
    db.commit()

    assert broad > narrow
    assert read_dirty_scopes(db, limit=10) == [
        SearchDirtyScope(broad, 4, column_id, None, None, "[0,0]")
    ]
    with db:
        db.execute("BEGIN")
        assert not ack_dirty_scope(db, scope_id=narrow, expected_cursor="[0,0]")
    project.close()


def test_current_cell_refresh_enqueues_bounded_region(tmp_path) -> None:
    project = Project.create(tmp_path / "project", name="search-work")
    sheet_id = project.add_sheet("Sheet")
    column_id = project.add_column(sheet_id, "Text")
    row_id = project.add_rows(sheet_id, [{}], {})[0]
    project.db.execute("DELETE FROM search_dirty_scopes")
    project.db.commit()

    with project.db:
        project.db.execute(
            "INSERT INTO cells(row_id,column_id,value) VALUES (?,?,?)",
            (row_id, column_id, '"hello"'),
        )
        from frisket.engine.store.current_cells import refresh_current_cells

        refresh_current_cells(project.db, column_ids=[column_id], row_ids=[row_id])
    scope = read_dirty_scopes(project.db, limit=1)[0]
    assert (scope.sheet_id, scope.column_id) == (sheet_id, column_id)
    assert (scope.row_id_start, scope.row_id_end) == (row_id, row_id)


def test_visibility_triggers_cover_rows_columns_and_sheets(tmp_path) -> None:
    project = Project.create(tmp_path / "project", name="search-work")
    sheet_id = project.add_sheet("Sheet")
    column_id = project.add_column(sheet_id, "Text")
    row_id = project.add_rows(sheet_id, [{}], {})[0]
    project.db.execute("DELETE FROM search_dirty_scopes")
    project.db.commit()

    with project.db:
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_id,))
        project.db.execute("UPDATE columns SET type='number' WHERE id=?", (column_id,))
        project.db.execute("UPDATE sheets SET name='Renamed' WHERE id=?", (sheet_id,))
    scopes = read_dirty_scopes(project.db, limit=10)
    assert any(
        scope.sheet_id == sheet_id
        and scope.column_id is None
        and scope.row_id_start is None
        for scope in scopes
    )
    assert any(scope.column_id == column_id for scope in scopes)
    assert any(
        scope.sheet_id == sheet_id and scope.column_id is None for scope in scopes
    )


def test_plain_connection_reads_latest_revision(tmp_path) -> None:
    project = Project.create(tmp_path / "project", name="search-work")
    path = project.db_path
    project.close()
    db = sqlite3.connect(path)
    assert latest_revision(db) >= 1
    db.close()


def test_known_bundle_migration_preserves_data_and_seeds_repair(tmp_path) -> None:
    path = tmp_path / "project"
    project = Project.create(path, name="search-work")
    sheet_id = project.add_sheet("Preserved")
    trigger_names = [
        str(row[0])
        for row in project.db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'trg_search_%'"
        )
    ]
    with project.db:
        for name in trigger_names:
            project.db.execute(f'DROP TRIGGER "{name}"')
        project.db.execute("DROP INDEX idx_current_cells_column_row")
        project.db.execute("DROP TABLE search_dirty_scopes")
        project.db.execute(
            "UPDATE meta SET value=? WHERE key='schema_digest'",
            (_PRE_SEARCH_DIGEST,),
        )
    project.close()

    reopened = Project(path)
    assert (
        reopened.db.execute(
            "SELECT name FROM sheets WHERE id=?", (sheet_id,)
        ).fetchone()[0]
        == "Preserved"
    )
    scopes = read_dirty_scopes(reopened.db, limit=2)
    assert len(scopes) == 1
    assert scopes[0].sheet_id is None
    indexes = {
        str(row[1]) for row in reopened.db.execute("PRAGMA index_list(current_cells)")
    }
    assert "idx_current_cells_column_row" in indexes
