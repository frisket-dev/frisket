from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.current_cells import refresh_current_cells
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.retrieval_lifecycle import (
    read_binding_revision,
    read_binding_revisions,
    touch_binding_revisions,
)
from frisket.engine.store.retrieval_lifecycle_migration import (
    RETRIEVAL_LIFECYCLE_FROM_DIGEST,
    RETRIEVAL_LIFECYCLE_TO_DIGEST,
    migrate_retrieval_lifecycle,
)
from frisket.engine.store.execution_metadata_migration import (
    EXECUTION_METADATA_FROM_DIGEST,
)
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.engine.store.search_index_work import latest_revision
from frisket.engine.store.value_codec import encode_stored_value


def _current_cell_project(tmp_path: Path) -> tuple[Project, int, int, int]:
    project = Project.create(tmp_path / "project", name="retrieval-lifecycle")
    sheet_id = project.add_sheet("Sheet")
    column_id = project.add_column(sheet_id, "Text")
    row_id = project.add_rows(sheet_id, [{"Text": "first"}], {"Text": column_id})[0]
    return project, sheet_id, row_id, column_id


def test_refresh_preserves_revision_until_source_writer_reports_changed_value(
    tmp_path,
) -> None:
    project, _sheet_id, row_id, column_id = _current_cell_project(tmp_path)
    before = project.db.execute(
        "SELECT origin_kind,origin_op_id,origin_run_id,retrieval_revision "
        "FROM current_cells WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()
    assert before[0] == "source_cell"

    with project.transaction_scope():
        refresh_current_cells(project.db, column_ids=[column_id], row_ids=[row_id])
    unchanged = project.db.execute(
        "SELECT retrieval_revision FROM current_cells WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()
    assert int(unchanged[0]) == int(before[3])

    with project.db:
        kind, value = encode_stored_value("second")
        project.db.execute(
            "UPDATE cells SET value_kind=?,value=? WHERE row_id=? AND column_id=?",
            (kind, value, row_id, column_id),
        )
        refresh_current_cells(
            project.db,
            column_ids=[column_id],
            row_ids=[row_id],
            source_values_changed=True,
        )

    after = project.db.execute(
        "SELECT origin_kind,origin_op_id,origin_run_id,retrieval_revision "
        "FROM current_cells WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()
    assert tuple(after[:3]) == tuple(before[:3])
    assert int(after[3]) > int(before[3])
    assert int(after[3]) == latest_revision(project.db)


def test_revision_allocation_and_projection_roll_back_together(tmp_path) -> None:
    project, _sheet_id, row_id, column_id = _current_cell_project(tmp_path)
    before_revision = read_binding_revision(
        project.db, row_id=row_id, column_id=column_id
    )
    before_high_water = latest_revision(project.db)

    project.db.execute("BEGIN")
    kind, value = encode_stored_value("rolled back")
    project.db.execute(
        "UPDATE cells SET value_kind=?,value=? WHERE row_id=? AND column_id=?",
        (kind, value, row_id, column_id),
    )
    refresh_current_cells(
        project.db,
        column_ids=[column_id],
        row_ids=[row_id],
        source_values_changed=True,
    )
    assert (
        read_binding_revision(project.db, row_id=row_id, column_id=column_id)
        > before_revision
    )
    project.db.rollback()

    assert (
        read_binding_revision(project.db, row_id=row_id, column_id=column_id)
        == before_revision
    )
    assert latest_revision(project.db) == before_high_water


def test_read_and_touch_binding_revisions_are_exact_and_indexed(tmp_path) -> None:
    project, _sheet_id, row_id, column_id = _current_cell_project(tmp_path)
    assert read_binding_revisions(
        project.db,
        [(row_id, column_id), (row_id + 999, column_id)],
    ) == {
        (row_id, column_id): read_binding_revision(
            project.db, row_id=row_id, column_id=column_id
        )
    }

    with project.db:
        project.db.execute("BEGIN")
        touched = touch_binding_revisions(
            project.db, [(row_id, column_id), (row_id + 999, column_id)]
        )
    assert touched == 1

    plan = project.db.execute(
        "EXPLAIN QUERY PLAN SELECT retrieval_revision FROM current_cells "
        "WHERE column_id=? AND row_id=?",
        (column_id, row_id),
    ).fetchall()
    assert any(
        "idx_current_cells_column_row" in str(row[3])
        and "column_id=? AND row_id=?" in str(row[3])
        for row in plan
    )


def test_binding_revision_read_requires_positive_ids(tmp_path) -> None:
    project, _sheet_id, row_id, column_id = _current_cell_project(tmp_path)
    with pytest.raises(ValueError, match="row_id"):
        read_binding_revision(project.db, row_id=0, column_id=column_id)
    with pytest.raises(ValueError, match="column_id"):
        read_binding_revisions(project.db, [(row_id, 0)])
    with pytest.raises(RuntimeError, match="caller transaction"):
        touch_binding_revisions(project.db, [(row_id, column_id)])


def test_retrieval_evidence_publication_touches_its_current_binding(tmp_path) -> None:
    project, sheet_id, row_id, column_id = _current_cell_project(tmp_path)
    artifact = record_source_artifact(
        project,
        artifact_kind="av",
        media_type="video/mp4",
        blob_hash="sha256:" + "1" * 64,
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=column_id,
    )
    span = record_source_span(
        project,
        artifact_id=int(artifact["id"]),
        span_kind="temporal",
        start_ms=0,
        end_ms=1000,
    )
    before = read_binding_revision(project.db, row_id=row_id, column_id=column_id)

    record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref={"kind": "source_cell", "row_id": row_id, "column_id": column_id},
        spans=[{"span_id": int(span["id"])}],
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        link_role="media_transcribe_temporal",
    )

    assert (
        read_binding_revision(project.db, row_id=row_id, column_id=column_id) > before
    )


def test_exact_prior_bundle_migrates_without_rewriting_history(tmp_path) -> None:
    path = tmp_path / "migration.frisket"
    project, _sheet_id, row_id, column_id = _current_cell_project(path)
    # _current_cell_project nests its conventional project directory.
    bundle_path = project.path
    history_before = [
        tuple(row)
        for row in project.db.execute(
            "SELECT id,kind,spec,undo_info,status FROM ops ORDER BY id"
        )
    ]
    current_before = tuple(
        project.db.execute(
            "SELECT origin_kind,origin_op_id,origin_run_id,base_producer_id "
            "FROM current_cells WHERE row_id=? AND column_id=?",
            (row_id, column_id),
        ).fetchone()
    )
    project.db.execute("ALTER TABLE current_cells DROP COLUMN retrieval_revision")
    project.db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (
            RETRIEVAL_LIFECYCLE_FROM_DIGEST,
            SCHEMA_DIGEST_META_KEY,
        ),
    )
    project.db.commit()
    project.close()

    migrated = Project(bundle_path)
    assert migrated.get_meta(SCHEMA_DIGEST_META_KEY) == SCHEMA_DIGEST
    assert [
        tuple(row)
        for row in migrated.db.execute(
            "SELECT id,kind,spec,undo_info,status FROM ops ORDER BY id"
        )
    ] == history_before
    current_after = migrated.db.execute(
        "SELECT origin_kind,origin_op_id,origin_run_id,base_producer_id,"
        "retrieval_revision FROM current_cells WHERE row_id=? AND column_id=?",
        (row_id, column_id),
    ).fetchone()
    assert tuple(current_after[:4]) == current_before
    assert int(current_after[4]) == latest_revision(migrated.db)
    assert int(current_after[4]) > 0
    scopes = migrated.db.execute(
        "SELECT sheet_id,column_id,row_id_start,row_id_end "
        "FROM search_dirty_scopes ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in scopes] == [(None, None, None, None)]


def test_migration_rolls_back_column_revision_and_work_on_failed_stamp(
    tmp_path,
) -> None:
    project, _sheet_id, _row_id, _column_id = _current_cell_project(tmp_path)
    db = project.db
    db.execute("ALTER TABLE current_cells DROP COLUMN retrieval_revision")
    db.execute(
        "UPDATE meta SET value=? WHERE key=?",
        (RETRIEVAL_LIFECYCLE_FROM_DIGEST, SCHEMA_DIGEST_META_KEY),
    )
    scopes_before = [
        tuple(row)
        for row in db.execute(
            "SELECT id,sheet_id,column_id,row_id_start,row_id_end "
            "FROM search_dirty_scopes ORDER BY id"
        )
    ]
    db.execute(
        "CREATE TRIGGER interrupt_retrieval_lifecycle_stamp "
        "BEFORE UPDATE ON meta WHEN NEW.key='schema_digest' "
        "AND NEW.value<>OLD.value BEGIN "
        "SELECT RAISE(ABORT,'migration interrupted'); END"
    )
    db.commit()

    with pytest.raises(sqlite3.IntegrityError, match="migration interrupted"):
        migrate_retrieval_lifecycle(db, bundle_path=project.db_path)

    assert "retrieval_revision" not in {
        str(row[1]) for row in db.execute("PRAGMA table_info(current_cells)")
    }
    assert [
        tuple(row)
        for row in db.execute(
            "SELECT id,sheet_id,column_id,row_id_start,row_id_end "
            "FROM search_dirty_scopes ORDER BY id"
        )
    ] == scopes_before
    assert (
        db.execute(
            "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
        ).fetchone()[0]
        == RETRIEVAL_LIFECYCLE_FROM_DIGEST
    )


def test_fresh_schema_revision_column_is_constrained() -> None:
    db = sqlite3.connect(":memory:")
    from frisket.engine.store.schema import SCHEMA

    db.executescript(SCHEMA)
    assert RETRIEVAL_LIFECYCLE_TO_DIGEST == EXECUTION_METADATA_FROM_DIGEST
    sql = str(
        db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='current_cells'"
        ).fetchone()[0]
    )
    assert "retrieval_revision INTEGER NOT NULL" in sql
    assert "retrieval_revision>0" in sql.replace(" ", "")
