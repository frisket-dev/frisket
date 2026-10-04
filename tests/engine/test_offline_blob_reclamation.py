import hashlib
import json
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.import_intake import ImportIntakeHeader, write_import_header
from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.project_blobs import reclaim_local_blobs


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "cleanup.frisket")
    try:
        yield project
    finally:
        project.close()


def path_for(project, digest):
    return project.path / "blobs" / digest[:2] / digest


def root(project, digest):
    sheet = project.add_sheet("root")
    column = project.add_column(sheet, "file", type="json")
    row = project.add_rows(sheet, [{"file": {"blob": digest}}], {"file": column})[0]
    return column, row


def test_reclaims_old_metadata_less_files_but_preserves_metadata_less_roots(project):
    orphan = project.add_blob(b"orphan")
    live = project.add_blob(b"live")
    project.gc_blobs()  # Historic metadata-only collection leaves both files.
    root(project, live)
    preview = reclaim_local_blobs(project)
    assert preview["blobs_reclaimable"] == 1
    assert preview["bytes_reclaimable"] == 6
    assert preview["bytes_freed"] == 0
    assert path_for(project, orphan).exists()
    result = reclaim_local_blobs(project, dry_run=False)
    assert result["blobs_removed"] == 1
    assert result["bytes_freed"] == 6
    assert path_for(project, live).exists()
    assert not path_for(project, orphan).exists()
    assert reclaim_local_blobs(project, dry_run=False)["blobs_removed"] == 0


def test_normal_gc_marks_only_registered_hashes_but_offline_marks_all_roots(project):
    from frisket.engine.store.project_blobs import _live_blob_hashes

    known = project.add_blob(b"registered")
    unknown = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(1200)]
    root(project, " ".join([known, *unknown]))
    with _live_blob_hashes(project) as db:
        assert [
            row[0] for row in db.execute("SELECT hash FROM temp.gc_live_blob_hashes")
        ] == [known]
    with _live_blob_hashes(project, strict=True) as db:
        assert (
            db.execute("SELECT count(*) FROM temp.gc_live_blob_hashes").fetchone()[0]
            == 1201
        )


def test_malformed_ownership_aborts_before_deleting_even_without_metadata(project):
    digest = project.add_blob(b"uncertain")
    project.gc_blobs()
    project.db.execute(
        "INSERT INTO receipts(id,action_kind,status,body) VALUES ('bad','test','error','{')"
    )
    project.db.commit()
    with pytest.raises(ValueError, match="ownership"):
        reclaim_local_blobs(project, dry_run=False)
    assert path_for(project, digest).exists()


def test_unlink_failure_rolls_back_metadata_and_can_retry(project, monkeypatch):
    digests = [project.add_blob(value) for value in (b"one", b"two")]
    original = Path.unlink
    calls = []

    def interrupt(path, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk error")
        return original(path, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt)
    with pytest.raises(OSError, match="disk error"):
        reclaim_local_blobs(project, dry_run=False)
    assert project.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2
    monkeypatch.setattr(Path, "unlink", original)
    reclaim_local_blobs(project, dry_run=False)
    assert project.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
    assert all(not path_for(project, digest).exists() for digest in digests)


def test_symlinks_unknown_layout_and_retention_are_respected(project, tmp_path):
    digest = project.add_blob(b"symlink target")
    canonical = path_for(project, digest)
    outside = tmp_path / "outside"
    canonical.replace(outside)
    canonical.symlink_to(outside)
    unknown = canonical.parent / "staging.tmp"
    unknown.write_bytes(b"staging")
    reclaim_local_blobs(project, dry_run=False)
    assert canonical.is_symlink() and outside.read_bytes() == b"symlink target"
    assert unknown.exists()
    assert project.db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()
    project.set_retention_policy(no_compact=True)
    with pytest.raises(ValueError, match="retention"):
        reclaim_local_blobs(project, dry_run=False)


def test_redo_and_transitive_ancestry_remain_physical_roots(project):
    parent = project.add_blob(b"source")
    child = project.add_blob(b"derived")
    project.record_blob_derivation(derived_hash=child, source_hash=parent, op="test")
    column, row = root(project, None)
    project.apply_edits(
        [{"row_id": row, "column_id": column, "value": {"blob": child}}]
    )
    project.undo()
    reclaim_local_blobs(project, dry_run=False)
    assert path_for(project, parent).exists()
    assert path_for(project, child).exists()
    project.redo()


def test_discarded_edit_remains_a_physical_blob_root(project):
    digest = project.add_blob(b"discarded edit history")
    column, row = root(project, None)
    edit_op = project.apply_edits(
        [{"row_id": row, "column_id": column, "value": {"blob": digest}}]
    )
    assert project.undo() == edit_op
    project.append_op("branch.after.undo")
    assert (
        project.db.execute("SELECT status FROM ops WHERE id=?", (edit_op,)).fetchone()[
            0
        ]
        == "discarded"
    )

    reclaim_local_blobs(project, dry_run=False)

    assert path_for(project, digest).exists()
    assert project.db.execute("SELECT 1 FROM blobs WHERE hash=?", (digest,)).fetchone()


def test_admitted_inventory_and_published_receipt_survive_physical_cleanup(project):
    admitted = project.add_blob(b"not published yet")
    published = project.add_blob(b"exported")
    orphan = project.add_blob(b"unused")
    directory = project.path / ".imports" / ("import-" + "a" * 32)
    directory.mkdir(parents=True)
    write_import_header(
        directory,
        ImportIntakeHeader(
            project_id="cleanup", storage_identity=project.storage_identity, envelope={}
        ),
    )
    with ImportInventory(directory / "inventory.db") as inventory:
        inventory.append(
            [
                dict(
                    logical_path="pending.pdf",
                    mime="application/pdf",
                    sha256=admitted,
                    size=17,
                    kind="pdf",
                )
            ]
        )
    project.db.execute(
        "INSERT INTO receipts(id,action_kind,status,body) VALUES ('export','test','completed',?)",
        (
            json.dumps(
                {"exports": [{"kind": "export_project_file", "blob_hash": published}]}
            ),
        ),
    )
    project.db.commit()
    reclaim_local_blobs(project, dry_run=False)
    assert path_for(project, admitted).exists()
    assert path_for(project, published).exists()
    assert not path_for(project, orphan).exists()


def test_directory_sync_precedes_metadata_removal(project, monkeypatch):
    from frisket.engine.store import project_blobs

    digest = project.add_blob(b"needs directory sync")
    observed = []

    def sync(directory):
        assert not path_for(project, digest).exists()
        assert project.db.execute(
            "SELECT 1 FROM blobs WHERE hash=?", (digest,)
        ).fetchone()
        observed.append(directory)

    monkeypatch.setattr(project_blobs, "_fsync_directory", sync)
    reclaim_local_blobs(project, dry_run=False)
    assert observed == [path_for(project, digest).parent]
