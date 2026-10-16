from __future__ import annotations

import sqlite3
import struct
import json
from array import array

from frisket.ai.embeddings import EmbeddingStore, make_space_descriptor
from frisket.ai.embeddings.analytics import legacy_analytical_space
from frisket.ai.embeddings.backends import LOCAL_MODEL
from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact, record_source_span
from frisket.retrieval.migration import legacy_cell_vector_key
from frisket.retrieval.sources import input_hash
from frisket.retrieval.store import RetrievalStore
from frisket.retrieval.upgrade import (
    LEGACY_EMBEDDING_UPGRADE_META_KEY,
    upgrade_legacy_embedding_caches,
)


def _write_cell_vectors(
    project: Project, rows: list[tuple[str, str, list[float]]]
) -> None:
    db = sqlite3.connect(project.path / "project.search.db")
    db.execute("CREATE TABLE IF NOT EXISTS cell_vec(key TEXT PRIMARY KEY,vec BLOB)")
    db.executemany(
        "INSERT OR REPLACE INTO cell_vec(key,vec) VALUES (?,?)",
        [
            (legacy_cell_vector_key(model, text), array("f", vector).tobytes())
            for model, text, vector in rows
        ],
    )
    db.commit()
    db.close()


def _write_named_vector(
    project: Project,
    *,
    index_id: str,
    space_id: str,
    source_key: str,
    source_ref: dict,
    source_hash: str,
    vector: list[float],
) -> None:
    db = sqlite3.connect(project.path / "project.embeddings.db")
    db.executescript(
        """
        CREATE TABLE embedding_items (
          index_id TEXT NOT NULL, space_id TEXT NOT NULL, source_key TEXT NOT NULL,
          source_ref_json TEXT NOT NULL, source_hash TEXT NOT NULL,
          status TEXT NOT NULL, error_code TEXT, error_message TEXT,
          vector_table TEXT, vector_id INTEGER, embedded_at TEXT, updated_at TEXT,
          PRIMARY KEY(index_id,source_key)
        );
        CREATE TABLE embedding_vectors_f32 (
          id INTEGER PRIMARY KEY, dimension INTEGER NOT NULL, vec BLOB NOT NULL
        );
        """
    )
    vector_id = db.execute(
        "INSERT INTO embedding_vectors_f32(dimension,vec) VALUES (?,?)",
        (len(vector), b"".join(struct.pack("<f", value) for value in vector)),
    ).lastrowid
    db.execute(
        "INSERT INTO embedding_items("
        "index_id,space_id,source_key,source_ref_json,source_hash,status,"
        "vector_table,vector_id,embedded_at,updated_at"
        ") VALUES (?,?,?,?,?,'ready','embedding_vectors_f32',?,datetime('now'),datetime('now'))",
        (
            index_id,
            space_id,
            source_key,
            json.dumps(source_ref, sort_keys=True),
            source_hash,
            vector_id,
        ),
    )
    db.commit()
    db.close()


def _misses(report) -> dict[str, int]:
    return {miss.reason: miss.count for miss in report.migration.misses}


def test_upgrade_proves_current_and_preserved_inputs_then_marks_once(tmp_path) -> None:
    project = Project.create(tmp_path / "analytical.frisket")
    try:
        sheet_id = project.add_sheet("Notes")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(
            sheet_id,
            [{"body": "  current analytical text  "}],
            {"body": column_id},
        )
        preserved = "preserved transcript segment"
        artifact = record_source_artifact(
            project, artifact_kind="audio", media_type="audio/wav"
        )
        record_source_span(
            project,
            artifact_id=int(artifact["id"]),
            span_kind="transcript_segment",
            quote=preserved,
        )
        custom_model = "custom/exact-embedding-model"
        project.db.execute(
            "INSERT INTO model_calls("
            "id,fact_version,capability,engine,provider,provider_kind,model_ids"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                "call:embedding",
                "v1",
                "llm.embed",
                custom_model,
                "custom",
                "platform_api",
                '["exact-embedding-model"]',
            ),
        )
        project.db.commit()
        default_model = f"fastembed/{LOCAL_MODEL}"
        _write_cell_vectors(
            project,
            [
                (default_model, "current analytical text", [0.125] * 384),
                (custom_model, preserved, [1.0, -0.5, 0.25]),
                ("opaque/model", "lost input", [0.1, 0.2]),
            ],
        )

        report = upgrade_legacy_embedding_caches(project, batch_size=2)

        assert report.complete
        assert not report.already_complete
        assert report.migration.analytical_items == 3
        assert report.migration.analytical_imported == 2
        assert report.analytical_matches == 2
        assert report.analytical_proofs == 2
        assert _misses(report) == {"analytical_input_unproven": 1}
        assert project.db.execute(
            "SELECT 1 FROM meta WHERE key=?", (LEGACY_EMBEDDING_UPGRADE_META_KEY,)
        ).fetchone()

        default_space = legacy_analytical_space(default_model)
        custom_space = legacy_analytical_space(custom_model, dimension=3)
        with RetrievalStore(project).open() as store:
            default_hash = input_hash("current analytical text")
            custom_hash = input_hash(preserved)
            default = store.get_representations(default_space.identity, [default_hash])[
                default_hash
            ]
            custom = store.get_representations(custom_space.identity, [custom_hash])[
                custom_hash
            ]
            assert struct.unpack("<384f", default.vector) == (0.125,) * 384
            assert struct.unpack("<3f", custom.vector) == (1.0, -0.5, 0.25)

        late_text = "late legacy write"
        _write_cell_vectors(project, [(custom_model, late_text, [0.0, 1.0, 0.0])])
        rerun = upgrade_legacy_embedding_caches(project, batch_size=1)
        assert rerun.already_complete
        assert rerun.migration == report.migration
        with RetrievalStore(project).open() as store:
            assert (
                store.get_representations(
                    custom_space.identity, [input_hash(late_text)]
                )
                == {}
            )
    finally:
        project.close()


def test_upgrade_imports_named_index_before_marking_completion(tmp_path) -> None:
    project = Project.create(tmp_path / "named.frisket")
    try:
        sheet_id = project.add_sheet("Stories")
        title_id = project.add_column(sheet_id, "Title")
        row_id = project.add_rows(
            sheet_id, [{"Title": "A headline"}], {"Title": title_id}
        )[0]
        descriptor = make_space_descriptor(
            provider_id="fastembed",
            provider_kind="local",
            actual_model_id="fastembed/named-model@revision",
            modality="row",
            dimension=2,
            vector_options={"purpose": "legacy-row"},
        )
        definitions = EmbeddingStore(project)
        space_id = definitions.create_space(descriptor)
        index_id = definitions.create_index(
            name="stories",
            space_id=space_id,
            sheet_id=sheet_id,
            source_query={},
            source_columns=["Title"],
            source_policy={"kind": "row", "join": "newline"},
        )
        index = definitions.get_index(index_id)
        payload = build_source_payloads(project, index, ["Title"], [row_id])[0]
        _write_named_vector(
            project,
            index_id=index_id,
            space_id=space_id,
            source_key=str(row_id),
            source_ref=payload["source_ref"],
            source_hash=payload["source_hash"],
            vector=[0.25, 0.75],
        )

        report = upgrade_legacy_embedding_caches(project, batch_size=1)

        assert report.complete
        assert report.migration.named_imported == 1
        with RetrievalStore(project).open() as store:
            [member] = store.read_named_dataset_vectors(
                index_id, space_id=space_id, limit=10
            )
            assert member.representation.input_hash == input_hash(payload["payload"])
            assert struct.unpack("<2f", member.representation.vector) == (0.25, 0.75)

        db = sqlite3.connect(project.path / "project.embeddings.db")
        db.execute("DELETE FROM embedding_items")
        db.commit()
        db.close()
        assert upgrade_legacy_embedding_caches(project).already_complete
        with RetrievalStore(project).open() as store:
            assert (
                len(
                    store.read_named_dataset_vectors(
                        index_id, space_id=space_id, limit=10
                    )
                )
                == 1
            )
    finally:
        project.close()


def test_structural_failure_stays_unmarked_and_retries_after_repair(tmp_path) -> None:
    project = Project.create(tmp_path / "retry.frisket")
    try:
        sheet_id = project.add_sheet("Notes")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(sheet_id, [{"body": "recoverable"}], {"body": column_id})
        path = project.path / "project.search.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE cell_vec(wrong TEXT)")
        db.commit()
        db.close()

        failed = upgrade_legacy_embedding_caches(project)

        assert not failed.complete
        assert _misses(failed) == {"analytical_sidecar_schema_invalid": 1}
        assert (
            project.db.execute(
                "SELECT 1 FROM meta WHERE key=?", (LEGACY_EMBEDDING_UPGRADE_META_KEY,)
            ).fetchone()
            is None
        )

        db = sqlite3.connect(path)
        db.execute("DROP TABLE cell_vec")
        db.commit()
        db.close()
        model = f"fastembed/{LOCAL_MODEL}"
        _write_cell_vectors(project, [(model, "recoverable", [0.5] * 384)])

        repaired = upgrade_legacy_embedding_caches(project)

        assert repaired.complete
        assert repaired.migration.analytical_imported == 1
        assert project.db.execute(
            "SELECT 1 FROM meta WHERE key=?", (LEGACY_EMBEDDING_UPGRADE_META_KEY,)
        ).fetchone()
    finally:
        project.close()
