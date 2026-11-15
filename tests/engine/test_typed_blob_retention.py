"""Ownership survives independent materialization, while provenance isn't a lease."""

import gzip
import sqlite3
from contextlib import closing

import pytest

from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.blob_retention_migration import (
    migrate_blob_derivation_ownership,
)
from frisket.engine.store.evidence import record_source_artifact
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.project_blobs import reclaim_local_blobs
from frisket.engine.store.runs import RunResultStore


@pytest.fixture
def project(tmp_path):
    with closing(Project.create(tmp_path / "retention.frisket")) as project:
        with project.db:
            project.db.execute("BEGIN")
            migrate_blob_derivation_ownership(project.db)
        yield project


def add_value(project, name, value, *, type="json"):
    sheet = project.add_sheet(name)
    column = project.add_column(sheet, "value", type=type)
    row = project.add_rows(sheet, [{"value": value}], {"value": column})[0]
    return sheet, column, row


def test_independent_file_survives_reclaimed_source_with_provenance(project):
    source = project.add_blob(b"original recording")
    child = project.add_blob(b"materialized clip")
    project.record_blob_derivation(derived_hash=child, source_hash=source, op="clip")
    add_value(project, "saved clip", {"blob": child})
    result = reclaim_local_blobs(project, dry_run=False)
    assert result["blobs_removed"] == 1
    with project.materialize_blob(child) as path:
        assert path.read_bytes() == b"materialized clip"
    with pytest.raises(BlobNotFoundError):
        with project.materialize_blob(source):
            pass
    assert project.blob_derivation_chain(child)[0]["source_hash"] == source
    assert project.db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("selector", [{"page": 2}, {"start_ms": 100, "end_ms": 900}])
def test_virtual_page_or_clip_keeps_actual_source(project, selector):
    source = project.add_blob(b"source bytes still required")
    add_value(project, "virtual", {"blob": source, **selector})
    assert reclaim_local_blobs(project, dry_run=False)["blobs_removed"] == 0
    with project.materialize_blob(source) as path:
        assert path.read_bytes() == b"source bytes still required"


def test_only_real_source_artifact_container_owns_original(project):
    owned = project.add_blob(b"imported original with no rendered page")
    cited = project.add_blob(b"historical citation only")
    old_cell = project.add_blob(b"old citation on a still-existing cell")
    sheet = project.add_sheet("documents")
    text_sheet, text_column, text_row = add_value(
        project, "text", "answer", type="text"
    )
    record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=owned,
        source_sheet_id=sheet,
    )
    record_source_artifact(
        project, artifact_kind="file", media_type="application/pdf", blob_hash=cited
    )
    record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=old_cell,
        source_sheet_id=text_sheet,
        source_row_id=text_row,
        source_column_id=text_column,
    )
    assert set(project.gc_blobs()["hashes"]) == {cited, old_cell}
    assert project.db.execute(
        "SELECT blob_hash FROM source_artifacts WHERE blob_hash=?", (cited,)
    ).fetchone()


def prepared(project, text):
    artifact = record_source_artifact(
        project, artifact_kind="document", media_type="application/pdf", page_count=1
    )
    with project.db:
        project.db.execute("BEGIN")
        op = project.append_op("prepare.test", commit=False)
        return PreparedContentStore(project).stage_reference(
            source_artifact_id=artifact["id"],
            producing_op_id=op,
            pages=[PreparedPageDraft(1, text)],
        )


def test_prepared_content_is_kept_by_independent_copy_and_removed_after_last_owner(
    project,
):
    shared = prepared(project, "retained independent text")
    orphan = prepared(project, "abandoned staging")
    first, _, _ = add_value(project, "first", shared, type="text")
    second, column, row = add_value(project, "copy", shared, type="text")
    project.delete_sheet(first)
    preview = reclaim_local_blobs(project)
    assert preview["prepared_refs_reclaimable"] == 1
    assert project.db.execute(
        "SELECT 1 FROM prepared_content_refs WHERE id=?", (orphan.ref_id,)
    ).fetchone()
    reclaim_local_blobs(project, dry_run=False)
    assert project.get_values(second, column)[row] == "retained independent text"
    assert (
        project.db.execute("SELECT id FROM prepared_content_refs").fetchall()[0][0]
        == shared.ref_id
    )
    project.delete_sheet(second)
    result = reclaim_local_blobs(project, dry_run=False)
    assert result["prepared_pages_reclaimable"] == 1
    assert (
        project.db.execute("SELECT count(*) FROM prepared_page_versions").fetchone()[0]
        == 0
    )


def test_orphan_trace_cleanup_keeps_existing_run_and_ignores_symlinks(
    project, tmp_path
):
    sheet = project.add_sheet("run")
    op = project.append_op("test.run")
    run = RunResultStore(project).start_run(op, sheet, "test.run")
    root = project.path / "traces"
    root.mkdir()
    for run_id in (run, run + 10):
        (root / f"run-{run_id}.jsonl.gz").write_bytes(gzip.compress(b"private content"))
    external = tmp_path / "unrelated"
    external.write_bytes(b"outside bundle")
    (root / f"run-{run + 20}.jsonl.gz").symlink_to(external)
    preview = reclaim_local_blobs(project)
    assert preview["traces_reclaimable"] == 1
    assert (root / f"run-{run + 10}.jsonl.gz").exists()
    actual = reclaim_local_blobs(project, dry_run=False)
    assert actual["traces_removed"] == 1
    assert (root / f"run-{run}.jsonl.gz").exists()
    assert external.read_bytes() == b"outside bundle"
    assert reclaim_local_blobs(project, dry_run=False)["traces_removed"] == 0


@pytest.mark.parametrize("empty", [False, True])
def test_derivation_migration_is_transactional_and_preserves_ids(empty):
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE blobs(hash TEXT PRIMARY KEY);
            CREATE TABLE blob_derivations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              derived_hash TEXT NOT NULL REFERENCES blobs(hash) ON DELETE CASCADE,
              source_hash TEXT NOT NULL REFERENCES blobs(hash) ON DELETE RESTRICT,
              op TEXT NOT NULL,params_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL DEFAULT (datetime('now')));
            INSERT INTO blobs VALUES ('source'),('child');
            INSERT INTO blob_derivations(id,derived_hash,source_hash,op)
              VALUES (900,'child','source','clip');
        """)
        if empty:
            db.execute("DELETE FROM blob_derivations")
            db.commit()
        db.execute("BEGIN")
        migrate_blob_derivation_ownership(db)
        db.rollback()
        assert any(
            row[3] == "source_hash"
            for row in db.execute("PRAGMA foreign_key_list(blob_derivations)")
        )
        with db:
            db.execute("BEGIN")
            migrate_blob_derivation_ownership(db)
        assert db.execute("SELECT id FROM blob_derivations").fetchall() == (
            [] if empty else [(900,)]
        )
        with db:
            db.execute("BEGIN")
            migrate_blob_derivation_ownership(db)
            db.execute("DELETE FROM blobs WHERE hash='source'")
            cursor = db.execute(
                "INSERT INTO blob_derivations(derived_hash,source_hash,op) VALUES ('child','source','clip')"
            )
        assert cursor.lastrowid == 901
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
