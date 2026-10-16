from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from array import array

import pytest

from frisket.ai.embeddings import EmbeddingStore, make_space_descriptor
from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobStoreError
from frisket.engine.store.retrieval_lifecycle import read_binding_revisions
from frisket.retrieval.migration import (
    LegacyCellVectorProof,
    migrate_legacy_embedding_caches,
)
from frisket.retrieval.sources import input_hash
from frisket.retrieval.store import RetrievalStore


def _write_legacy_item(
    project,
    *,
    index_id,
    space_id,
    source_key,
    source_ref,
    source_hash,
    vector,
):
    db = sqlite3.connect(project.path / "project.embeddings.db")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS embedding_vectors_f32 (
          id INTEGER PRIMARY KEY, dimension INTEGER NOT NULL, vec BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS embedding_items (
          index_id TEXT NOT NULL, space_id TEXT NOT NULL, source_key TEXT NOT NULL,
          source_ref_json TEXT NOT NULL, source_hash TEXT NOT NULL,
          status TEXT NOT NULL, vector_table TEXT, vector_id INTEGER,
          PRIMARY KEY(index_id, source_key)
        );
        """
    )
    cursor = db.execute(
        "INSERT INTO embedding_vectors_f32(dimension,vec) VALUES (?,?)",
        (len(vector), struct.pack(f"<{len(vector)}f", *vector)),
    )
    db.execute(
        "INSERT INTO embedding_items VALUES (?,?,?,?,?,'ready',"
        "'embedding_vectors_f32',?)",
        (
            index_id,
            space_id,
            str(source_key),
            json.dumps(source_ref, sort_keys=True, separators=(",", ":")),
            source_hash,
            cursor.lastrowid,
        ),
    )
    db.commit()
    db.close()


def _row_space(model: str, *, dimension: int = 2) -> dict:
    return make_space_descriptor(
        provider_id="fastembed",
        provider_kind="local",
        actual_model_id=model,
        modality="row",
        dimension=dimension,
        vector_options={"purpose": "legacy-row"},
    )


def _legacy_project(tmp_path, *, sparse=False):
    project = Project.create(tmp_path / "legacy.frisket", name="legacy")
    sheet_id = project.add_sheet("Stories")
    title_id = project.add_column(sheet_id, "Title")
    body_id = project.add_column(sheet_id, "Body")
    row_id = project.add_rows(
        sheet_id,
        [
            {"Title": "A headline"}
            if sparse
            else {"Title": "A headline", "Body": "The complete body"}
        ],
        {"Title": title_id, "Body": body_id},
    )[0]

    model = "fastembed/test-model@revision"
    metadata = EmbeddingStore(project)
    space_id = metadata.create_space(_row_space(model))
    index_id = metadata.create_index(
        name="story rows",
        space_id=space_id,
        sheet_id=sheet_id,
        source_query={},
        source_columns=["Title", "Body"],
        source_policy={"kind": "row", "join": "newline"},
    )
    index = metadata.get_index(index_id)
    payload = build_source_payloads(project, index, ["Title", "Body"], [row_id])[0]
    _write_legacy_item(
        project,
        index_id=index_id,
        space_id=space_id,
        source_key=payload["source_key"],
        source_ref=payload["source_ref"],
        source_hash=payload["source_hash"],
        vector=[0.25, 0.75],
    )
    return project, index_id, space_id, model, row_id, title_id, body_id, payload


def _write_cell_vec(
    project: Project, model: str, text: str, vector: list[float]
) -> str:
    key = hashlib.sha1((model + "\0" + text).encode("utf-8", "replace")).hexdigest()
    db = sqlite3.connect(project.path / "project.search.db")
    db.execute("CREATE TABLE cell_vec(key TEXT PRIMARY KEY, vec BLOB NOT NULL)")
    db.execute(
        "INSERT INTO cell_vec(key,vec) VALUES (?,?)",
        (key, array("f", vector).tobytes()),
    )
    db.commit()
    db.close()
    return key


def _misses(report) -> dict[str, int]:
    return {miss.reason: miss.count for miss in report.misses}


def test_real_legacy_project_imports_exact_named_and_analytical_vectors_idempotently(
    tmp_path,
):
    (
        project,
        index_id,
        space_id,
        model,
        row_id,
        title_id,
        body_id,
        payload,
    ) = _legacy_project(tmp_path)
    analytical_text = "an exact paid analytical input"
    _write_cell_vec(project, model, analytical_text, [1.0, -0.5])
    definitions_before = [
        dict(row)
        for row in project.db.execute("SELECT * FROM embedding_indexes ORDER BY id")
    ]
    project_changes_before = project.db.total_changes

    with RetrievalStore(project).open() as target:
        proof = LegacyCellVectorProof(space_id, model, analytical_text)
        report = migrate_legacy_embedding_caches(
            project, target, cell_vector_proofs=(proof,), batch_size=1
        )

        assert report.complete
        assert (report.named_indexes, report.named_items, report.named_imported) == (
            1,
            1,
            1,
        )
        assert (report.analytical_items, report.analytical_imported) == (1, 1)
        assert report.misses == ()
        named = target.read_named_dataset_vectors(index_id, space_id=space_id, limit=10)
        assert len(named) == 1
        assert struct.unpack("<2f", named[0].representation.vector) == (
            0.25,
            0.75,
        )
        assert named[0].representation.input_hash == input_hash(payload["payload"])

        revisions = read_binding_revisions(
            project.db, [(row_id, title_id), (row_id, body_id)]
        )
        stored_dependencies = target.db.execute(
            "SELECT row_id,column_id,retrieval_revision "
            "FROM binding_dependencies ORDER BY column_id"
        ).fetchall()
        assert [tuple(row) for row in stored_dependencies] == [
            (row_id, column_id, revisions[(row_id, column_id)])
            for column_id in sorted((title_id, body_id))
        ]
        analytical_hash = input_hash(analytical_text)
        analytical = target.get_representations(space_id, [analytical_hash])
        assert struct.unpack("<2f", analytical[analytical_hash].vector) == (1.0, -0.5)

        counts_before_rerun = {
            table: target.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "sources",
                "parts",
                "bindings",
                "representations",
                "named_dataset_membership",
            )
        }
        rerun = migrate_legacy_embedding_caches(
            project, target, cell_vector_proofs=(proof,), batch_size=1
        )
        assert rerun == report
        assert counts_before_rerun == {
            table: target.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in counts_before_rerun
        }
        assert (project.path / "project.embeddings.db").is_file()
        assert (project.path / "project.search.db").is_file()

        # Cleanup is an external controller step after this complete report and
        # worker quiescence. A post-cleanup rerun is a no-op, not a destructive
        # empty replacement of already migrated membership.
        (project.path / "project.embeddings.db").unlink()
        (project.path / "project.search.db").unlink()
        after_cleanup = migrate_legacy_embedding_caches(
            project, target, cell_vector_proofs=(proof,)
        )
        assert after_cleanup.complete
        assert not after_cleanup.named_sidecar_found
        assert not after_cleanup.analytical_sidecar_found
        assert (
            len(
                target.read_named_dataset_vectors(index_id, space_id=space_id, limit=10)
            )
            == 1
        )
        assert analytical_hash in target.get_representations(
            space_id, [analytical_hash]
        )

    assert project.db.total_changes == project_changes_before
    assert [
        dict(row)
        for row in project.db.execute("SELECT * FROM embedding_indexes ORDER BY id")
    ] == definitions_before
    project.close()


def test_stale_named_source_and_opaque_cell_cache_are_explicit_misses(tmp_path):
    project, index_id, space_id, model, row_id, title_id, _, _ = _legacy_project(
        tmp_path
    )
    _write_cell_vec(project, model, "no supplied proof", [0.1, 0.2])
    project.apply_edits([{"row_id": row_id, "column_id": title_id, "value": "Changed"}])

    with RetrievalStore(project).open() as target:
        report = migrate_legacy_embedding_caches(project, target)
        assert report.complete
        assert report.named_imported == 0
        assert report.analytical_imported == 0
        assert _misses(report) == {
            "analytical_input_unproven": 1,
            "named_source_stale": 1,
        }
        assert (
            target.read_named_dataset_vectors(index_id, space_id=space_id, limit=10)
            == ()
        )
    project.close()


def test_blob_backed_legacy_image_uses_verified_bytes_as_cache_input(tmp_path):
    project = Project.create(tmp_path / "images.frisket", name="images")
    sheet_id = project.add_sheet("Images")
    image_id = project.add_column(sheet_id, "Image", type="image")
    image_bytes = b"\x89PNG\r\n\x1a\nexact-immutable-image-input"
    digest = project.add_blob(image_bytes, filename="source.png", mime="image/png")
    row_id = project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "source.png", "mime": "image/png"}}],
        {"Image": image_id},
    )[0]
    descriptor = make_space_descriptor(
        provider_id="fastembed",
        provider_kind="local",
        actual_model_id="legacy/image-model@revision",
        modality="image",
        dimension=2,
        vector_options={"resize": 224, "crop": "center"},
    )
    metadata = EmbeddingStore(project)
    space_id = metadata.create_space(descriptor)
    index_id = metadata.create_index(
        name="images",
        space_id=space_id,
        sheet_id=sheet_id,
        source_query={},
        source_columns=["Image"],
        source_policy={"kind": "image_blob"},
    )
    index = metadata.get_index(index_id)
    payload = build_source_payloads(project, index, ["Image"], [row_id])[0]
    _write_legacy_item(
        project,
        index_id=index_id,
        space_id=space_id,
        source_key=str(row_id),
        source_ref=payload["source_ref"],
        source_hash=payload["source_hash"],
        vector=[0.3, 0.4],
    )

    with RetrievalStore(project).open() as target:
        report = migrate_legacy_embedding_caches(project, target)
        assert report.named_imported == 1
        member = target.read_named_dataset_vectors(
            index_id, space_id=space_id, limit=1
        )[0]
        assert member.representation.input_hash == input_hash(image_bytes)
        assert struct.unpack("<2f", member.representation.vector) == pytest.approx(
            (0.3, 0.4)
        )
    project.close()


def test_malformed_legacy_sidecars_fail_closed_without_removal(tmp_path):
    project = Project.create(tmp_path / "broken.frisket", name="broken")
    sqlite3.connect(project.path / "project.embeddings.db").execute(
        "CREATE TABLE embedding_items(wrong TEXT)"
    ).connection.close()
    sqlite3.connect(project.path / "project.search.db").execute(
        "CREATE TABLE other_cache(key TEXT)"
    ).connection.close()

    with RetrievalStore(project).open() as target:
        report = migrate_legacy_embedding_caches(project, target)
        assert not report.complete
        assert _misses(report) == {
            "analytical_sidecar_schema_invalid": 1,
            "named_sidecar_schema_invalid": 1,
        }
    assert (project.path / "project.embeddings.db").is_file()
    assert (project.path / "project.search.db").is_file()
    project.close()


def test_unreadable_legacy_sidecars_are_reported_without_raising(tmp_path):
    project = Project.create(tmp_path / "corrupt.frisket", name="corrupt")
    (project.path / "project.embeddings.db").write_bytes(b"not sqlite")
    (project.path / "project.search.db").write_bytes(b"also not sqlite")

    with RetrievalStore(project).open() as target:
        report = migrate_legacy_embedding_caches(project, target)
        assert not report.complete
        assert _misses(report) == {
            "analytical_sidecar_unreadable": 1,
            "named_sidecar_unreadable": 1,
        }
    project.close()


def test_blob_materialization_failure_is_an_explicit_named_miss(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    import frisket.retrieval.migration as migration_module

    project, index_id, space_id, *_rest = _legacy_project(tmp_path)

    def fail_input(*_args, **_kwargs):
        raise BlobStoreError("blob backend unavailable")

    monkeypatch.setattr(migration_module, "named_encoder_input", fail_input)
    with RetrievalStore(project).open() as target:
        report = migrate_legacy_embedding_caches(project, target)
        assert report.complete
        assert report.named_imported == 0
        assert _misses(report) == {"named_input_unprovable": 1}
        assert not target.read_named_dataset_vectors(
            index_id, space_id=space_id, limit=10
        )
    project.close()


def test_sparse_named_row_preserves_vector_and_absent_member_revision(tmp_path):
    from frisket.ai.embeddings.unified_index import plan_named_index_items

    project, index_id, space_id, _model, row, title, body, payload = _legacy_project(
        tmp_path, sparse=True
    )
    try:
        # Sparse imports have no current cell for a missing member.
        project.db.execute(
            "DELETE FROM current_cells WHERE row_id=? AND column_id=?", (row, body)
        )
        project.db.commit()
        metadata = EmbeddingStore(project)
        _recipe, items = plan_named_index_items(
            project,
            index=metadata.get_index(index_id),
            space=metadata.get_space(space_id),
            payloads=[payload],
        )
        assert [
            (d.column_id, d.retrieval_revision) for d in items[0].binding.dependencies
        ][-1] == (body, 0)
        with RetrievalStore(project).open() as target:
            report = migrate_legacy_embedding_caches(project, target)
            assert report.named_imported == 1
            assert (
                target.db.execute(
                    "SELECT retrieval_revision FROM binding_dependencies WHERE row_id=? AND column_id=?",
                    (row, body),
                ).fetchone()[0]
                == 0
            )
        project.apply_edits([{"row_id": row, "column_id": body, "value": "new body"}])
        updated = build_source_payloads(
            project, metadata.get_index(index_id), ["Title", "Body"], [row]
        )
        _recipe, fresh = plan_named_index_items(
            project,
            index=metadata.get_index(index_id),
            space=metadata.get_space(space_id),
            payloads=updated,
        )
        assert fresh[0].binding.dependencies[-1].retrieval_revision > 0
        assert fresh[0].source != items[0].source
    finally:
        project.close()
