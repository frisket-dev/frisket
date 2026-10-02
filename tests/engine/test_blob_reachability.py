"""Conservative ownership roots without a hashes-times-corpus scan."""

import hashlib
import json

import pytest

from frisket.engine.store import Project
from frisket.engine.store import project_blobs
from frisket.engine.store.import_intake import ImportIntakeHeader, write_import_header
from frisket.engine.store.import_inventory import ImportInventory


@pytest.fixture
def project(tmp_path):
    project = Project.create(tmp_path / "roots.frisket")
    yield project
    project.close()


def root_cell(project, value):
    sheet = project.add_sheet("root")
    column = project.add_column(sheet, "value", type="json")
    rows = project.add_rows(sheet, [{"value": value}], {"value": column})
    return sheet, column, rows[0]


def test_redoable_manual_blob_edit_remains_reachable(project):
    digest = project.add_blob(b"only in redo")
    sheet, column, row = root_cell(project, None)
    project.apply_edits(
        [{"row_id": row, "column_id": column, "value": {"blob": digest}}]
    )
    project.undo()
    assert project.gc_blobs()["hashes"] == []
    project.redo()
    assert project.get_values(sheet, column)[row] == {"blob": digest}


def test_transitive_ancestry_is_live_and_dead_edges_are_collectable(project):
    source, middle, leaf, dead_source, dead_leaf = [
        project.add_blob(value.encode())
        for value in ("source", "middle", "leaf", "dead-source", "dead-leaf")
    ]
    for child, parent in ((middle, source), (leaf, middle), (dead_leaf, dead_source)):
        project.record_blob_derivation(
            derived_hash=child, source_hash=parent, op="test"
        )
    root_cell(project, {"blob": leaf})
    assert project._referenced_blob_hashes() == {source, middle, leaf}
    assert set(project.gc_blobs()["hashes"]) == {dead_source, dead_leaf}
    assert len(project.blob_derivation_chain(leaf)) == 2
    # A corrupt cycle still terminates conservatively.
    project.record_blob_derivation(derived_hash=source, source_hash=leaf, op="cycle")
    assert project.gc_blobs()["hashes"] == []


def test_embedded_overlapping_hashes_preserve_substring_retention(project):
    digest = project.add_blob(b"inside longer hexadecimal token")
    orphan = project.add_blob(b"orphan")
    root_cell(project, "a" + digest + "b")
    assert project.gc_blobs()["hashes"] == [orphan]
    with project.blob_store.materialize(digest) as path:
        assert path.read_bytes() == b"inside longer hexadecimal token"


def _checkpoint(project, payload):
    project.db.execute(
        "INSERT INTO effect_checkpoints(id,family,group_key,unit_key,action_kind,identity,state,payload) VALUES ('checkpoint','row_effect','group','unit','test','identity','returned',?)",
        (payload,),
    )
    project.db.commit()


@pytest.mark.parametrize("payload", ["not json", "[]", '{"output":{"row_files":[{}]}}'])
def test_malformed_unresolved_effect_retains_every_blob(project, payload):
    digest = project.add_blob(b"unresolved")
    _checkpoint(project, payload)
    assert project.gc_blobs()["hashes"] == []
    assert project._referenced_blob_hashes() == {digest}
    assert not project.db.in_transaction


def test_receipt_and_paid_effect_payloads_are_conservative_roots(project):
    receipt, effect, orphan = [
        project.add_blob(value.encode()) for value in ("receipt", "effect", "orphan")
    ]
    project.db.execute(
        "INSERT INTO receipts(id,action_kind,status,body) VALUES ('receipt','test','error',?)",
        (
            json.dumps(
                {"exports": [{"kind": "export_project_file", "blob_hash": receipt}]}
            ),
        ),
    )
    project.db.execute(
        "INSERT INTO effect_checkpoints(id,family,group_key,unit_key,action_kind,identity,state,payload) VALUES ('paid','model','g','u','test','identity','returned',?)",
        (json.dumps({"response": {"blob_hash": effect}}),),
    )
    project.db.commit()
    assert project.gc_blobs()["hashes"] == [orphan]


def _inventory(project, digest):
    directory = project.path / ".imports" / ("import-" + "a" * 32)
    directory.mkdir(parents=True)
    write_import_header(
        directory,
        ImportIntakeHeader(
            project_id="roots", storage_identity=project.storage_identity, envelope={}
        ),
    )
    with ImportInventory(directory / "inventory.db") as inventory:
        inventory.append(
            [
                dict(
                    logical_path="pending.pdf",
                    mime="application/pdf",
                    sha256=digest,
                    size=1,
                    kind="pdf",
                )
            ]
        )
    return directory


def test_admitted_inventory_before_first_published_page_is_a_root(project):
    pending = project.add_blob(b"uploaded but not yet published")
    orphan = project.add_blob(b"unrelated")
    _inventory(project, pending)
    assert project.gc_blobs()["hashes"] == [orphan]
    assert project._referenced_blob_hashes() == {pending}


def test_missing_or_damaged_unresolved_inventory_fails_closed(project):
    digest = project.add_blob(b"pending ownership")
    directory = _inventory(project, digest)
    (directory / "inventory.db").unlink()
    assert project.gc_blobs()["hashes"] == []
    assert not (directory / "inventory.db").exists()  # no writable inventory opener


def test_gc_preserves_outer_transaction(project):
    orphan = project.add_blob(b"orphan")
    project.db.execute("BEGIN")
    assert project.gc_blobs()["hashes"] == [orphan]
    assert project.db.in_transaction
    project.db.rollback()
    assert project.db.execute("SELECT hash FROM blobs").fetchone()[0] == orphan


def test_returned_orphan_hashes_are_complete_not_a_truncated_page(project):
    hashes = {hashlib.sha256(str(i).encode()).hexdigest() for i in range(1100)}
    project.db.executemany(
        "INSERT INTO blobs(hash,size) VALUES (?,1)", ((value,) for value in hashes)
    )
    project.db.commit()
    result = project.gc_blobs()
    assert result["blobs_removed"] == 1100
    assert set(result["hashes"]) == hashes


def test_no_blobs_does_not_scan_root_text(project, monkeypatch):
    root_cell(project, "plain text only")

    def refuse(*args):
        raise AssertionError("no blobs means no reachability corpus scan")

    monkeypatch.setattr(project_blobs, "_root_texts", refuse)
    assert project.gc_blobs()["hashes"] == []


def test_delete_sheet_streams_surviving_roots_in_bounded_batches(project, monkeypatch):
    count = 3000
    hashes = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(count)]
    project.db.executemany(
        "INSERT INTO blobs(hash,size) VALUES (?,1)", ((value,) for value in hashes)
    )
    project.db.commit()
    sheet = project.add_sheet("survivors")
    column = project.add_column(sheet, "value")
    project.add_rows(sheet, [{"value": value} for value in hashes], {"value": column})
    doomed = project.add_sheet("delete me")
    original = project_blobs._insert_live_hashes
    observed = []

    def bounded_insert(db, batch):
        observed.append(len(batch))
        return original(db, batch)

    def forbid_set():
        raise AssertionError("GC must not materialize its compatibility hash set")

    monkeypatch.setattr(project_blobs, "_insert_live_hashes", bounded_insert)
    monkeypatch.setattr(project, "_referenced_blob_hashes", forbid_set)
    project.delete_sheet(doomed)
    assert max(observed) <= 512
    assert sum(observed) == count
    assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == count
