from __future__ import annotations

import sqlite3
import struct
import json
import time
from array import array
from contextlib import closing

import pytest
from filelock import FileLock

from frisket.ai.embeddings import EmbeddingStore, make_space_descriptor
from frisket.ai.embeddings.analytics import legacy_analytical_space
from frisket.ai.embeddings.backends import LOCAL_MODEL
from frisket.ai.embeddings.source_payload import build_source_payloads
from frisket.engine.store import Project
from frisket.engine.store.evidence import record_source_artifact, record_source_span
from frisket.retrieval.migration import legacy_cell_vector_key
from frisket.retrieval.sources import input_hash
from frisket.retrieval.store import (
    PROJECT_SIDECAR_META_KEY,
    SCHEMA_VERSION,
    RetrievalStore,
    UnsupportedRetrievalSchema,
    read_sidecar_id,
)
from frisket.retrieval.upgrade import (
    LEGACY_EMBEDDING_UPGRADE_META_KEY,
    prepare_retrieval_on_open,
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
        assert not report.reused
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
        assert not rerun.reused
        assert rerun.migration.analytical_items == 4
        assert _misses(rerun) == {"analytical_input_unproven": 2}
        with RetrievalStore(project).open() as store:
            assert (
                store.get_representations(
                    custom_space.identity, [input_hash(late_text)]
                )
                == {}
            )
    finally:
        project.close()


def test_upgrade_reopens_when_a_legacy_wal_gains_a_committed_vector(tmp_path) -> None:
    project = Project.create(tmp_path / "wal-append.frisket")
    writer: sqlite3.Connection | None = None
    try:
        sheet_id = project.add_sheet("Notes")
        column_id = project.add_column(sheet_id, "body")
        project.add_rows(
            sheet_id,
            [{"body": "first cached text"}, {"body": "second cached text"}],
            {"body": column_id},
        )
        model = f"fastembed/{LOCAL_MODEL}"
        path = project.path / "project.search.db"
        writer = sqlite3.connect(path)
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE cell_vec(key TEXT PRIMARY KEY,vec BLOB)")
        writer.execute(
            "INSERT INTO cell_vec(key,vec) VALUES (?,?)",
            (
                legacy_cell_vector_key(model, "first cached text"),
                array("f", [0.25] * 384).tobytes(),
            ),
        )
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        first = upgrade_legacy_embedding_caches(project)

        assert not first.reused
        assert first.migration.analytical_imported == 1
        main_fingerprint = (path.stat().st_size, path.stat().st_mtime_ns)

        writer.execute(
            "INSERT INTO cell_vec(key,vec) VALUES (?,?)",
            (
                legacy_cell_vector_key(model, "second cached text"),
                array("f", [0.5] * 384).tobytes(),
            ),
        )
        writer.commit()
        assert (path.stat().st_size, path.stat().st_mtime_ns) == main_fingerprint
        wal_path = path.with_name(f"{path.name}-wal")
        assert wal_path.stat().st_size > 0

        second = upgrade_legacy_embedding_caches(project)

        assert not second.reused
        assert second.migration.analytical_items == 2
        assert second.migration.analytical_imported == 2
        space = legacy_analytical_space(model)
        with RetrievalStore(project).open() as store:
            found = store.get_representations(
                space.identity,
                [input_hash("first cached text"), input_hash("second cached text")],
            )
        assert set(found) == {
            input_hash("first cached text"),
            input_hash("second cached text"),
        }

        stable = upgrade_legacy_embedding_caches(project)
        assert stable.reused
        assert stable.migration == second.migration
    finally:
        if writer is not None:
            writer.close()
        project.close()


def test_upgrade_reuses_marker_when_reading_creates_an_empty_legacy_wal(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "empty-wal.frisket")
    try:
        path = project.path / "project.search.db"
        legacy = sqlite3.connect(path)
        try:
            assert legacy.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            legacy.execute("CREATE TABLE cell_vec(key TEXT PRIMARY KEY,vec BLOB)")
            legacy.commit()
            legacy.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            legacy.close()
        wal_path = path.with_name(f"{path.name}-wal")
        if wal_path.exists():
            assert wal_path.stat().st_size == 0
            wal_path.unlink()
        assert not wal_path.exists()

        first = upgrade_legacy_embedding_caches(project)

        assert not first.reused
        assert wal_path.is_file()
        assert wal_path.stat().st_size == 0
        second = upgrade_legacy_embedding_caches(project)
        assert second.reused
        assert second.migration == first.migration
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
        rerun = upgrade_legacy_embedding_caches(project)
        assert not rerun.reused
        with RetrievalStore(project).open() as store:
            assert not store.read_named_dataset_vectors(
                index_id, space_id=space_id, limit=10
            )
    finally:
        project.close()


def test_structural_failure_is_recorded_once_and_retries_after_repair(tmp_path) -> None:
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
        assert project.db.execute(
            "SELECT 1 FROM meta WHERE key=?", (LEGACY_EMBEDDING_UPGRADE_META_KEY,)
        ).fetchone()
        project.db.execute(
            "UPDATE meta SET value='not-json' WHERE key=?",
            (LEGACY_EMBEDDING_UPGRADE_META_KEY,),
        )
        project.db.commit()
        redone = upgrade_legacy_embedding_caches(project)
        assert not redone.reused
        assert redone.migration == failed.migration
        unchanged = upgrade_legacy_embedding_caches(project)
        assert unchanged.reused
        assert unchanged.migration == failed.migration

        db = sqlite3.connect(path)
        db.execute("DROP TABLE cell_vec")
        db.commit()
        db.close()
        model = f"fastembed/{LOCAL_MODEL}"
        _write_cell_vectors(project, [(model, "recoverable", [0.5] * 384)])

        repaired = upgrade_legacy_embedding_caches(project)

        assert repaired.complete
        assert not repaired.reused
        assert repaired.migration.analytical_imported == 1
        assert project.db.execute(
            "SELECT 1 FROM meta WHERE key=?", (LEGACY_EMBEDDING_UPGRADE_META_KEY,)
        ).fetchone()
    finally:
        project.close()


def test_project_open_imports_legacy_cache_before_consumers(tmp_path):
    path = tmp_path / "reopen.frisket"
    with_project = Project.create(path)
    sheet = with_project.add_sheet("Notes")
    column = with_project.add_column(sheet, "body")
    with_project.add_rows(sheet, [{"body": "current text"}], {"body": column})
    _write_cell_vectors(
        with_project,
        [(f"fastembed/{LOCAL_MODEL}", "current text", [1.0] + [0.0] * 383)],
    )
    with_project.close()
    project = Project(path)
    try:
        report = upgrade_legacy_embedding_caches(project)
        assert report.reused
        assert report.analytical_matches == 1
    finally:
        project.close()


def test_project_open_requeues_rebuild_before_any_cache_recreates_sidecar(tmp_path):
    from frisket.search import drain_index

    path = tmp_path / "repair.frisket"
    project = Project.create(path)
    sheet = project.add_sheet("Notes")
    column = project.add_column(sheet, "body")
    project.add_rows(sheet, [{"body": "rebuild needle"}], {"body": column})
    drain_index(project)
    assert not project.db.execute("SELECT 1 FROM search_dirty_scopes").fetchone()
    original_sidecar_id = read_sidecar_id(path)
    project.close()
    (path / "project.retrieval.db").unlink()
    with RetrievalStore(path).open() as recreated:
        recreated_sidecar_id = recreated.sidecar_id()
    assert recreated_sidecar_id != original_sidecar_id
    reopened = Project(path)
    try:
        assert reopened.db.execute(
            "SELECT 1 FROM search_dirty_scopes WHERE sheet_id IS NULL AND scan_cursor='[0,0]'"
        ).fetchone()
        with RetrievalStore(reopened):
            pass
        from frisket.search_index import index_needs_work

        assert index_needs_work(reopened)
    finally:
        reopened.close()


def test_sidecar_replacement_invalidates_a_matching_legacy_attempt(tmp_path) -> None:
    project = Project.create(tmp_path / "sidecar-replaced.frisket")
    try:
        sheet = project.add_sheet("Notes")
        column = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": "cached"}], {"body": column})
        _write_cell_vectors(
            project,
            [(f"fastembed/{LOCAL_MODEL}", "cached", [0.25] * 384)],
        )
        first = upgrade_legacy_embedding_caches(project)
        assert not first.reused
        assert first.migration.analytical_imported == 1
        old_sidecar_id = read_sidecar_id(project)

        (project.path / "project.retrieval.db").unlink()
        second = upgrade_legacy_embedding_caches(project)

        assert not second.reused
        assert second.migration.analytical_imported == 1
        assert read_sidecar_id(project) != old_sidecar_id
    finally:
        project.close()


def test_corrupt_legacy_cache_records_attempt_without_blocking_open(tmp_path) -> None:
    path = tmp_path / "corrupt-legacy.frisket"
    project = Project.create(path)
    project.close()
    (path / "project.search.db").write_bytes(b"not a sqlite database")

    reopened = Project(path)
    try:
        report = upgrade_legacy_embedding_caches(reopened)
        assert report.reused
        assert not report.complete
        assert _misses(report) == {"analytical_sidecar_unreadable": 1}
    finally:
        reopened.close()


def test_empty_project_open_does_not_queue_a_global_search_rebuild(tmp_path):
    from frisket.search import drain_index

    path = tmp_path / "empty.frisket"
    with closing(Project.create(path)) as project:
        drain_index(project)
    (path / "project.retrieval.db").unlink()
    with closing(Project(path)) as project:
        assert not project.db.execute("SELECT 1 FROM search_dirty_scopes").fetchone()


@pytest.mark.parametrize("kind", ["garbage", "foreign", "old_version"])
def test_unusable_retrieval_cache_is_preserved_and_rebuilt_on_open(tmp_path, kind):
    from frisket.search import drain_index, search_project

    path = tmp_path / f"recover-{kind}.frisket"
    with closing(Project.create(path)) as project:
        sheet = project.add_sheet("Notes")
        column = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": "recoverable needle"}], {"body": column})
    sidecar = path / "project.retrieval.db"
    if kind == "garbage":
        sidecar.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(sidecar) as db:
            db.execute("CREATE TABLE preserved_work(value TEXT)")
            db.execute("INSERT INTO preserved_work VALUES ('paid vector remains')")
            if kind == "old_version":
                db.execute(
                    "CREATE TABLE retrieval_meta(key TEXT PRIMARY KEY,value TEXT)"
                )
                db.execute("INSERT INTO retrieval_meta VALUES ('schema_version','1')")
    with closing(Project(path)) as project:
        archives = list(path.glob("retrieval-unusable-*/project.retrieval.db"))
        assert len(archives) == 1
        if kind == "garbage":
            assert archives[0].read_bytes() == b"not a sqlite database"
        else:
            with sqlite3.connect(archives[0]) as db:
                assert db.execute("SELECT value FROM preserved_work").fetchone() == (
                    "paid vector remains",
                )
        assert project.db.execute("SELECT 1 FROM search_dirty_scopes").fetchone()
        drain_index(project)
        assert search_project(project, "needle")
    with closing(Project(path)):
        assert len(list(path.glob("retrieval-unusable-*"))) == 1


def test_open_skips_upgrade_while_another_process_lock_is_held(tmp_path) -> None:
    project = Project.create(tmp_path / "locked-upgrade.frisket")
    try:
        (project.path / "project.search.db").write_bytes(b"not sqlite")
        lock = FileLock(str(project.path / ".retrieval-upgrade.lock"))
        with lock:
            prepare_retrieval_on_open(project)
        assert (
            project.db.execute(
                "SELECT 1 FROM meta WHERE key=?", (LEGACY_EMBEDDING_UPGRADE_META_KEY,)
            ).fetchone()
            is None
        )

        prepare_retrieval_on_open(project)
        assert project.db.execute(
            "SELECT 1 FROM meta WHERE key=?", (LEGACY_EMBEDDING_UPGRADE_META_KEY,)
        ).fetchone()
    finally:
        project.close()


def test_open_defers_repair_while_initial_sidecar_creation_is_locked(tmp_path) -> None:
    project = Project.create(tmp_path / "sidecar-creation-race.frisket")
    writer: sqlite3.Connection | None = None
    try:
        sheet = project.add_sheet("Notes")
        column = project.add_column(sheet, "body")
        project.add_rows(sheet, [{"body": "repair later"}], {"body": column})
        project.db.execute("DELETE FROM search_dirty_scopes")
        project.db.commit()

        path = project.path / "project.retrieval.db"
        writer = sqlite3.connect(path, isolation_level=None)
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "CREATE TABLE retrieval_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        writer.execute(
            "INSERT INTO retrieval_meta(key,value) VALUES ('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )

        started = time.monotonic()  # realtime: bound actual SQLite lock waiting
        prepare_retrieval_on_open(project)
        elapsed = time.monotonic() - started  # realtime: assert nonblocking open

        assert elapsed < 1
        assert project.db.execute(
            "SELECT 1 FROM search_dirty_scopes WHERE sheet_id IS NULL "
            "AND scan_cursor='[0,0]'"
        ).fetchone()

        writer.rollback()
        writer.close()
        writer = None
        prepare_retrieval_on_open(project)
        assert read_sidecar_id(project) is not None
    finally:
        if writer is not None:
            writer.rollback()
            writer.close()
        project.close()


def test_project_open_does_not_wait_for_current_sidecar_writer(tmp_path) -> None:
    from frisket.search_index import bind_retrieval_sidecar

    path = tmp_path / "sidecar-writer.frisket"
    with closing(Project.create(path)) as project:
        sidecar_id = bind_retrieval_sidecar(project)

    writer = sqlite3.connect(path / "project.retrieval.db", isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()  # realtime: bound actual SQLite lock waiting
        with closing(Project(path)) as reopened:
            assert read_sidecar_id(reopened) == sidecar_id
        elapsed = time.monotonic() - started  # realtime: assert nonblocking open
        assert elapsed < 1
    finally:
        writer.rollback()
        writer.close()


def test_project_database_failure_does_not_quarantine_healthy_sidecar(
    tmp_path, monkeypatch
) -> None:
    from frisket import search_index

    project = Project.create(tmp_path / "project-db-failure.frisket")
    try:
        with RetrievalStore(project).open():
            pass
        project.set_meta(PROJECT_SIDECAR_META_KEY, "stale-sidecar")
        failure = sqlite3.DatabaseError("project database write failed")
        failure.sqlite_errorcode = sqlite3.SQLITE_CORRUPT

        def fail_project_binding(_project) -> str:
            raise failure

        monkeypatch.setattr(
            search_index, "bind_retrieval_sidecar", fail_project_binding
        )
        with pytest.raises(
            sqlite3.DatabaseError, match="project database write failed"
        ):
            prepare_retrieval_on_open(project)

        assert (project.path / "project.retrieval.db").is_file()
        assert not list(project.path.glob("retrieval-unusable-*"))
    finally:
        project.close()


@pytest.mark.parametrize("version", ["1", "999"])
def test_unsupported_sidecar_schema_is_rejected_before_ddl(
    tmp_path, version: str
) -> None:
    path = tmp_path / "unknown-sidecar"
    path.mkdir()
    sidecar = path / "project.retrieval.db"
    db = sqlite3.connect(sidecar)
    db.execute("CREATE TABLE retrieval_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute(
        "INSERT INTO retrieval_meta(key,value) VALUES ('schema_version',?)",
        (version,),
    )
    db.commit()
    db.close()

    store = RetrievalStore(path)
    with pytest.raises(
        UnsupportedRetrievalSchema, match="unsupported retrieval schema version"
    ):
        store.open()
    with pytest.raises(RuntimeError, match="retrieval store is not open"):
        _ = store.db

    db = sqlite3.connect(sidecar)
    try:
        assert {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == {"retrieval_meta"}
    finally:
        db.close()


def test_sidecar_without_version_raises_typed_schema_error(tmp_path) -> None:
    path = tmp_path / "foreign-sidecar"
    path.mkdir()
    sidecar = path / "project.retrieval.db"
    db = sqlite3.connect(sidecar)
    db.execute("CREATE TABLE foreign_data(value TEXT)")
    db.commit()
    db.close()

    with pytest.raises(UnsupportedRetrievalSchema, match="schema version is missing"):
        RetrievalStore(path).open()
