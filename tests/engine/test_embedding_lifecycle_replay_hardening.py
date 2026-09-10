"""Replay hardening for native embedding lifecycle actions.

These tests intentionally tamper with project metadata, sidecar state, and
export artifacts after a first successful action. A same-key replay must reject
the stale live state instead of returning the stored receipt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.embeddings import (
    EmbeddingStore,
    VectorBackend,
    build_batch_result,
    canonical_json,
)
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project
from helpers import replace_test_source_cell

PROJECT_ID = "p"


class FakeGateway:
    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[tuple[list[Any], str | None, str | None, str]] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append((list(texts), provider, model, modality))
        return build_batch_result(
            [
                [float((abs(hash(str(text))) % 97) + 1)] + [0.0] * (self.dim - 1)
                for text in texts
            ],
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            requested_model=model,
            actual_model_id=model or "fake",
            modality=modality,
        )


_OPENROUTER_MODEL = "openai/text-embedding-3-large"


class FakeProbeGateway(FakeGateway):
    def embed(self, texts, *, provider, model, modality):
        self.calls.append((list(texts), provider, model, modality))
        return build_batch_result(
            [[0.1] * self.dim for _ in texts],
            provider_id=provider or "openrouter",
            provider_kind="platform_api",
            requested_model=model,
            actual_model_id=f"resolved/{model}",
            modality=modality,
        )


class _OpenRouterRouter:
    def providers(self):
        return ["openrouter"]


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "emb.frisket", name="emb")
    sheet = p.add_sheet("data")
    cols = {"headline": p.add_column(sheet, "headline")}
    p.add_rows(sheet, [{"headline": f"story {i}"} for i in range(4)], cols)
    yield p, sheet, cols, tmp_path
    p.close()


def _run(project: Project, action: dict[str, Any], *, gateway=None):
    deps = ExecutorDeps(embedding_gateway=gateway) if gateway is not None else None
    return run_action_spec(project, action, project_id=PROJECT_ID, deps=deps)


def _run_with_deps(
    project: Project, action: dict[str, Any], *, gateway=None, router=None
):
    return run_action_spec(
        project,
        action,
        project_id=PROJECT_ID,
        deps=ExecutorDeps(embedding_gateway=gateway, router=router),
    )


def _create_action(
    sheet_id: int,
    *,
    key: str = "create@1",
    source_query: dict[str, Any] | None = None,
    source_columns: list[str] | None = None,
    provider: str = "fastembed",
    model: str | None = None,
    provider_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "sheet_id": sheet_id,
        "source_columns": source_columns or ["headline"],
        "modality": "text",
        "provider": provider,
        "source_policy": {"kind": "text_cell"},
        "provider_policy": provider_policy or {"allow_remote": False},
    }
    if model is not None:
        params["model"] = model
    if source_query is not None:
        params["source_query"] = source_query
    return {
        "action_id": "embedding.index_create",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def _update_action(
    index_id: str,
    *,
    key: str = "policy@1",
    maintenance: dict[str, Any] | None = None,
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"index_id": index_id}
    if maintenance is not None:
        params["maintenance_policy"] = maintenance
    if provider is not None:
        params["provider_policy"] = provider
    return {
        "action_id": "embedding.index_update_policy",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def _refresh_action(
    index_id: str,
    *,
    key: str = "refresh@1",
    mode: str = "full",
    row_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "index_id": index_id,
        "mode": mode,
    }
    if row_scope is not None:
        params["row_scope"] = row_scope
    return {
        "action_id": "embedding.index_refresh",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def _delete_action(
    index_id: str,
    *,
    key: str = "delete@1",
) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_delete",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id},
        "idempotency_key": key,
    }


def _export_action(
    index_id: str,
    dest: Path,
    *,
    key: str = "export@1",
    include_vectors: bool = False,
) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_export",
        "scope": {"kind": "project"},
        "params": {
            "index_id": index_id,
            "destination": {"kind": "local_dir", "path": str(dest)},
            "formats": ["jsonl"],
            "include_vectors": include_vectors,
        },
        "idempotency_key": key,
    }


def _project_action(index_id: str, *, key: str = "project@1") -> dict[str, Any]:
    return {
        "action_id": "embedding.index_project",
        "scope": {"kind": "project"},
        "sheet_name": f"projection-{key}",
        "params": {
            "index_id": index_id,
            "method": "pca",
            "dimensions": 2,
        },
        "idempotency_key": key,
    }


def _cluster_action(index_id: str, *, key: str = "cluster@1") -> dict[str, Any]:
    return {
        "action_id": "embedding.index_cluster",
        "scope": {"kind": "project"},
        "sheet_name": f"cluster-{key}",
        "params": {
            "index_id": index_id,
            "method": "kmeans",
            "k": 2,
            "seed": 0,
        },
        "idempotency_key": key,
    }


def _create(project: Project, sheet: int, **kw) -> str:
    result = _run(project, _create_action(sheet, **kw))
    assert result.status == "completed", result.errors
    return result.outputs[0].ref["index_id"]


def _refresh(project: Project, index_id: str, *, key: str = "refresh@setup"):
    result = _run(
        project,
        _refresh_action(index_id, key=key),
        gateway=FakeGateway(),
    )
    assert result.status == "completed", result.errors
    return result


def _json_obj(value: str | None) -> dict[str, Any]:
    data = json.loads(value or "{}")
    assert isinstance(data, dict)
    return data


def _receipt_ref(project: Project, receipt_id: str, kind: str) -> dict[str, Any]:
    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()["body"]
    )
    for section in ("inputs", "outputs", "evidence"):
        for item in body.get(section) or []:
            ref = item.get("ref")
            if isinstance(ref, dict) and ref.get("kind") == kind:
                return ref
    raise AssertionError(f"receipt ref {kind!r} not found")


def _assert_stale(result):
    assert result.status == "failed"
    assert result.errors[0].code == "stale_replay"


def _source_query(sheet_id: int, text: str) -> dict[str, Any]:
    return {
        "schema_version": "frisket.query.v1",
        "kind": "sheet.filter",
        "scope": {"kind": "sheet", "sheet_id": sheet_id},
        "filter": {"headline": {"contains": text}},
    }


def _visible_row_ids(project: Project, sheet_id: int) -> list[int]:
    return [int(row_id) for row_id in project.visible_row_ids(sheet_id)]


def _child_row_count(project: Project, sheet_name: str) -> int:
    sheet = next(s for s in project.sheets() if s["name"] == sheet_name)
    return project.row_count(int(sheet["id"]))


def _insert_row(project: Project, table: str, row: dict[str, Any]) -> None:
    columns = list(row)
    project.db.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [row[column] for column in columns],
    )


def test_create_replay_rejects_deleted_index(project):
    project, sheet, _cols, _tmp = project
    action = _create_action(sheet, key="create-delete")
    first = _run(project, action)
    assert first.status == "completed", first.errors
    index_id = first.outputs[0].ref["index_id"]
    project.db.execute("DELETE FROM embedding_indexes WHERE id=?", (index_id,))
    project.db.commit()

    _assert_stale(_run(project, action))


def test_create_replay_rejects_deleted_space(project):
    project, sheet, _cols, _tmp = project
    action = _create_action(sheet, key="create-space-delete")
    first = _run(project, action)
    assert first.status == "completed", first.errors
    space_id = first.outputs[0].ref["space_id"]
    project.db.commit()
    project.db.execute("PRAGMA foreign_keys=OFF")
    project.db.execute("DELETE FROM embedding_spaces WHERE id=?", (space_id,))
    project.db.commit()
    project.db.execute("PRAGMA foreign_keys=ON")

    _assert_stale(_run(project, action))


def test_create_replay_rejects_space_dimension_and_source_query_drift(project):
    project, sheet, _cols, _tmp = project
    action = _create_action(
        sheet,
        key="create-drift",
        source_query=_source_query(sheet, "story 1"),
    )
    first = _run(project, action)
    assert first.status == "completed", first.errors
    index_id = first.outputs[0].ref["index_id"]
    index = EmbeddingStore(project).get_index(index_id)
    project.db.execute(
        "UPDATE embedding_spaces SET dimension=? WHERE id=?",
        (768, index["space_id"]),
    )
    project.db.commit()

    _assert_stale(_run(project, action))

    project.db.execute(
        "UPDATE embedding_spaces SET dimension=? WHERE id=?",
        (384, index["space_id"]),
    )
    project.db.execute(
        "UPDATE embedding_indexes SET source_query_json=? WHERE id=?",
        (json.dumps(_source_query(sheet, "story 2"), sort_keys=True), index_id),
    )
    project.db.commit()

    _assert_stale(_run(project, action))


def test_create_replay_rejects_source_column_and_policy_drift(project):
    project, sheet, _cols, _tmp = project
    action = _create_action(sheet, key="create-source-drift")
    first = _run(project, action)
    assert first.status == "completed", first.errors
    index_id = first.outputs[0].ref["index_id"]

    project.db.execute(
        "UPDATE embedding_indexes SET source_columns_json=? WHERE id=?",
        (json.dumps(["headline", "missing"], sort_keys=True), index_id),
    )
    project.db.commit()
    _assert_stale(_run(project, action))

    project.db.execute(
        "UPDATE embedding_indexes SET source_columns_json=?, source_policy_hash=? WHERE id=?",
        (
            json.dumps(["headline"], sort_keys=True),
            "sha256:source-policy-drift",
            index_id,
        ),
    )
    project.db.commit()
    _assert_stale(_run(project, action))


def test_custom_remote_create_replay_does_not_reprobe(project):
    project, sheet, _cols, _tmp = project
    action = _create_action(
        sheet,
        key="create-custom-remote",
        provider="openrouter",
        model=_OPENROUTER_MODEL,
        provider_policy={"allow_remote": True},
    )
    first_gateway = FakeProbeGateway(dim=1024)
    first = _run_with_deps(
        project, action, gateway=first_gateway, router=_OpenRouterRouter()
    )
    assert first.status == "completed", first.errors
    assert len(first_gateway.calls) == 1
    assert first.outputs[0].ref["space_model_id"] == _OPENROUTER_MODEL
    assert first.outputs[0].ref["actual_model_id"] == f"resolved/{_OPENROUTER_MODEL}"

    replay_gateway = FakeProbeGateway(dim=2048)
    replay = _run_with_deps(
        project, action, gateway=replay_gateway, router=_OpenRouterRouter()
    )
    assert replay.status == "completed", replay.errors
    assert replay.receipt_id == first.receipt_id
    assert replay_gateway.calls == []


def test_sparse_create_receipt_fails_closed(project):
    project, sheet, _cols, _tmp = project
    action = _create_action(sheet, key="create-sparse")
    first = _run(project, action)
    assert first.status == "completed", first.errors

    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
        ).fetchone()["body"]
    )
    for output in body["outputs"]:
        if output.get("ref", {}).get("kind") == "embedding_index":
            output["ref"].pop("source_query", None)
            output["ref"].pop("source_policy_hash", None)
    for evidence in body["evidence"]:
        if evidence.get("ref", {}).get("kind") == "embedding_index":
            evidence["ref"].pop("source_query", None)
            evidence["ref"].pop("source_policy_hash", None)
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(body, sort_keys=True), first.receipt_id),
    )
    project.db.commit()

    _assert_stale(_run(project, action))


def test_update_policy_replay_rejects_later_policy_change_and_preserves_conflict(
    project,
):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    action = _update_action(
        index_id,
        key="policy-drift",
        maintenance={"mode": "scheduled", "schedule": "@hourly"},
        provider={"allow_remote": True},
    )
    first = _run(project, action)
    assert first.status == "completed", first.errors

    project.db.execute(
        "UPDATE embedding_indexes SET provider_policy_json=? WHERE id=?",
        (json.dumps({"allow_remote": False}, sort_keys=True), index_id),
    )
    project.db.commit()

    conflict = _run(
        project,
        _update_action(
            index_id,
            key="policy-drift",
            maintenance={"mode": "manual"},
            provider={"allow_remote": True},
        ),
    )
    assert conflict.status == "failed"
    assert conflict.errors[0].code == "idempotency_conflict"

    _assert_stale(_run(project, action))


def test_update_policy_replay_rejects_deleted_index(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    action = _update_action(
        index_id, key="policy-deleted", provider={"allow_remote": True}
    )
    first = _run(project, action)
    assert first.status == "completed", first.errors
    project.db.execute("DELETE FROM embedding_indexes WHERE id=?", (index_id,))
    project.db.commit()

    _assert_stale(_run(project, action))


def test_delete_replay_rejects_recreated_index_sidecar_and_artifact(project):
    project, sheet, _cols, _tmp_path = project
    out = project.path / "exports" / "embeddings"
    out.mkdir(parents=True)
    index_id = _create(project, sheet)
    _refresh(project, index_id)
    exported = _run(project, _export_action(index_id, out, key="export-before-delete"))
    assert exported.status == "completed", exported.errors
    deleted_artifact = Path(exported.outputs[0].ref["path"])

    original_index = dict(
        project.db.execute(
            "SELECT * FROM embedding_indexes WHERE id=?", (index_id,)
        ).fetchone()
    )
    original_space = dict(
        project.db.execute(
            "SELECT * FROM embedding_spaces WHERE id=?", (original_index["space_id"],)
        ).fetchone()
    )
    action = _delete_action(index_id, key="delete-hardening")
    deleted = _run(project, action)
    assert deleted.status == "completed", deleted.errors

    assert _run(project, action).status == "completed"

    _insert_row(project, "embedding_spaces", original_space)
    _insert_row(project, "embedding_indexes", original_index)
    project.db.commit()
    _assert_stale(_run(project, action))

    project.db.execute("DELETE FROM embedding_indexes WHERE id=?", (index_id,))
    project.db.execute(
        "DELETE FROM embedding_spaces WHERE id=?", (original_space["id"],)
    )
    project.db.commit()
    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        backend.upsert_item(
            index_id=index_id,
            space_id=original_space["id"],
            source_key="1",
            source_ref={"sheet_id": sheet, "row_id": 1, "columns": ["headline"]},
            source_hash="sha256:sidecar-restored",
            status="ready",
            vector=[1.0] + [0.0] * 383,
        )
    finally:
        backend.close()
    _assert_stale(_run(project, action))

    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        backend.delete_index(index_id)
    finally:
        backend.close()
    deleted_artifact.write_text("restored")
    _assert_stale(_run(project, action))


def test_retaining_vectors_is_refused_without_deleting_the_index(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    _refresh(project, index_id)
    action = _delete_action(index_id, key="delete-keep-sidecar")
    action["params"]["delete_sidecar_vectors"] = False
    first = _run(project, action)
    assert first.status == "failed"
    assert first.errors[0].code == "invalid_action_request"
    assert EmbeddingStore(project).get_index(index_id) is not None
    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        assert sum(backend.item_counts(index_id).values()) > 0
    finally:
        backend.close()

    assert first.receipt_id is None


def test_refresh_replay_validates_sidecar_without_gateway_call(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    action = _refresh_action(index_id, key="refresh-hardening")
    first_gateway = FakeGateway()
    first = _run(project, action, gateway=first_gateway)
    assert first.status == "completed", first.errors

    replay_gateway = FakeGateway()
    again = _run(project, action, gateway=replay_gateway)
    assert again.status == "completed"
    assert again.receipt_id == first.receipt_id
    assert replay_gateway.calls == []

    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        backend.db.execute(
            "UPDATE embedding_items SET source_hash=? "
            "WHERE index_id=? AND source_key=(SELECT MIN(source_key) FROM embedding_items WHERE index_id=?)",
            ("sha256:tampered", index_id, index_id),
        )
        backend.db.commit()
    finally:
        backend.close()

    _assert_stale(_run(project, action, gateway=FakeGateway()))


def test_refresh_replay_rejects_vector_blob_and_index_definition_drift(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet, source_query=_source_query(sheet, "story"))
    action = _refresh_action(index_id, key="refresh-vector-drift")
    first = _run(project, action, gateway=FakeGateway())
    assert first.status == "completed", first.errors

    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        vid = backend.db.execute(
            "SELECT vector_id FROM embedding_items WHERE index_id=? ORDER BY source_key LIMIT 1",
            (index_id,),
        ).fetchone()["vector_id"]
        stored_vector = backend.db.execute(
            "SELECT vec FROM embedding_vectors_f32 WHERE id=?", (vid,)
        ).fetchone()["vec"]
        stored_values = struct.unpack("<384f", stored_vector)
        tampered_values = (stored_values[0] + 1.0, *stored_values[1:])
        assert tampered_values[0] != stored_values[0]
        tampered_vector = struct.pack("<384f", *tampered_values)
        assert tampered_vector != stored_vector
        backend.db.execute(
            "UPDATE embedding_vectors_f32 SET vec=? WHERE id=?",
            (tampered_vector, vid),
        )
        backend.db.commit()
    finally:
        backend.close()
    _assert_stale(_run(project, action, gateway=FakeGateway()))

    _refresh(project, index_id, key="refresh-reset")
    fresh_action = _refresh_action(index_id, key="refresh-definition-drift")
    assert _run(project, fresh_action, gateway=FakeGateway()).status == "completed"
    project.db.execute(
        "UPDATE embedding_indexes SET source_query_json=? WHERE id=?",
        (json.dumps(_source_query(sheet, "story 1"), sort_keys=True), index_id),
    )
    project.db.commit()
    _assert_stale(_run(project, fresh_action, gateway=FakeGateway()))


def test_refresh_replay_rejects_missing_sidecar_and_deleted_item(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    missing_action = _refresh_action(index_id, key="refresh-missing-sidecar")
    missing = _run(project, missing_action, gateway=FakeGateway())
    assert missing.status == "completed", missing.errors
    (project.path / "project.embeddings.db").unlink()
    _assert_stale(_run(project, missing_action, gateway=FakeGateway()))

    _refresh(project, index_id, key="refresh-reset-missing")
    deleted_action = _refresh_action(index_id, key="refresh-deleted-item")
    deleted = _run(project, deleted_action, gateway=FakeGateway())
    assert deleted.status == "completed", deleted.errors
    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        backend.db.execute(
            "DELETE FROM embedding_items WHERE index_id=? "
            "AND source_key=(SELECT MIN(source_key) FROM embedding_items WHERE index_id=?)",
            (index_id, index_id),
        )
        backend.db.commit()
    finally:
        backend.close()
    _assert_stale(_run(project, deleted_action, gateway=FakeGateway()))


def test_zero_row_refresh_replay_rejects_malformed_sidecar_without_mutating(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(
        project,
        sheet,
        key="create-empty-scope",
        source_query=_source_query(sheet, "no matching story"),
    )
    action = _refresh_action(index_id, key="refresh-zero-rows")
    result = _run(project, action, gateway=FakeGateway())
    assert result.status == "completed", result.errors
    assert result.outputs[0].ref["total_items"] == 0

    sidecar = project.path / "project.embeddings.db"
    sidecar.unlink()
    conn = sqlite3.connect(sidecar)
    try:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    _assert_stale(_run(project, action, gateway=FakeGateway()))
    conn = sqlite3.connect(sidecar)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "embedding_items" not in tables
    assert "embedding_vectors_f32" not in tables


def test_refresh_replay_rejects_malformed_count_schema(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    action = _refresh_action(index_id, key="refresh-bad-count-schema")
    result = _run(project, action, gateway=FakeGateway())
    assert result.status == "completed", result.errors

    sidecar = project.path / "project.embeddings.db"
    conn = sqlite3.connect(sidecar)
    try:
        conn.execute("DROP TABLE embedding_items")
        conn.execute("DROP TABLE embedding_vectors_f32")
        conn.execute("CREATE TABLE embedding_items (not_index_id TEXT)")
        conn.execute("CREATE TABLE embedding_vectors_f32 (not_id INTEGER)")
        conn.commit()
    finally:
        conn.close()

    _assert_stale(_run(project, action, gateway=FakeGateway()))


def test_refresh_replay_rejects_malformed_vector_payload(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    action = _refresh_action(index_id, key="refresh-bad-vector-payload")
    result = _run(project, action, gateway=FakeGateway())
    assert result.status == "completed", result.errors

    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        vector_id = backend.db.execute(
            "SELECT vector_id FROM embedding_items WHERE index_id=? ORDER BY source_key LIMIT 1",
            (index_id,),
        ).fetchone()["vector_id"]
        backend.db.execute(
            "UPDATE embedding_vectors_f32 SET vec=? WHERE id=?",
            ("not-bytes", vector_id),
        )
        backend.db.commit()
    finally:
        backend.close()

    _assert_stale(_run(project, action, gateway=FakeGateway()))


def test_refresh_replay_preserves_conflict_precedence(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    action = _refresh_action(index_id, key="refresh-conflict")
    first = _run(project, action, gateway=FakeGateway())
    assert first.status == "completed", first.errors
    (project.path / "project.embeddings.db").unlink()

    conflict = _run(
        project,
        _refresh_action(index_id, key="refresh-conflict", mode="incremental"),
        gateway=FakeGateway(),
    )
    assert conflict.status == "failed"
    assert conflict.errors[0].code == "idempotency_conflict"


def test_whole_index_refresh_replay_rejects_count_drift_and_supersession(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    action = _refresh_action(index_id, key="refresh-counts")
    first = _run(project, action, gateway=FakeGateway())
    assert first.status == "completed", first.errors

    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        backend.upsert_item(
            index_id=index_id,
            space_id=first.outputs[0].ref["space_id"],
            source_key="9999",
            source_ref={"sheet_id": sheet, "row_id": 9999, "columns": ["headline"]},
            source_hash="sha256:count-drift",
            status="ready",
            vector=[4.0] + [0.0] * 383,
        )
    finally:
        backend.close()
    _assert_stale(_run(project, action, gateway=FakeGateway()))

    _refresh(project, index_id, key="refresh-reset-counts")
    superseded_action = _refresh_action(index_id, key="refresh-superseded")
    superseded = _run(project, superseded_action, gateway=FakeGateway())
    assert superseded.status == "completed", superseded.errors
    newer = _run(
        project,
        _refresh_action(index_id, key="refresh-newer"),
        gateway=FakeGateway(),
    )
    assert newer.status == "completed", newer.errors

    _assert_stale(_run(project, superseded_action, gateway=FakeGateway()))


def test_refresh_replay_ignores_later_source_drift_but_not_recorded_scope_drift(
    project,
):
    project, sheet, cols, _tmp = project
    index_id = _create(project, sheet)
    row_id = _visible_row_ids(project, sheet)[0]
    action = _refresh_action(
        index_id,
        key="refresh-scope",
        mode="incremental",
        row_scope={"kind": "row_ids", "row_ids": [row_id]},
    )
    first = _run(project, action, gateway=FakeGateway())
    assert first.status == "completed", first.errors

    replace_test_source_cell(
        project,
        row_id=row_id,
        column_id=cols["headline"],
        value="ordinary later edit",
    )
    replay_gateway = FakeGateway()
    again = _run(project, action, gateway=replay_gateway)
    assert again.status == "completed"
    assert replay_gateway.calls == []

    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        item = backend.get_item(index_id, str(row_id))
        source_ref = _json_obj(item["source_ref_json"])
        source_ref["row_id"] = row_id + 1000
        backend.db.execute(
            "UPDATE embedding_items SET source_ref_json=? WHERE index_id=? AND source_key=?",
            (json.dumps(source_ref, sort_keys=True), index_id, str(row_id)),
        )
        backend.db.commit()
    finally:
        backend.close()
    _assert_stale(_run(project, action, gateway=FakeGateway()))


def test_sparse_refresh_receipt_fails_closed(project):
    project, sheet, _cols, _tmp = project
    index_id = _create(project, sheet)
    action = _refresh_action(index_id, key="refresh-sparse")
    result = _run(project, action, gateway=FakeGateway())
    assert result.status == "completed", result.errors

    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["body"]
    )
    body["evidence"] = [
        ev
        for ev in body["evidence"]
        if ev.get("ref", {}).get("kind") != "embedding_index_refresh_items"
    ]
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(body, sort_keys=True), result.receipt_id),
    )
    project.db.commit()

    _assert_stale(_run(project, action, gateway=FakeGateway()))


def test_export_replay_rejects_metadata_and_sidecar_drift(project):
    project, sheet, _cols, tmp_path = project
    out = tmp_path / "out"
    out.mkdir()
    index_id = _create(project, sheet)
    _refresh(project, index_id)
    action = _export_action(index_id, out, key="export-drift")
    first = _run(project, action)
    assert first.status == "completed", first.errors

    project.db.execute(
        "UPDATE embedding_indexes SET source_policy_hash=? WHERE id=?",
        ("sha256:tampered", index_id),
    )
    project.db.commit()
    _assert_stale(_run(project, action))

    index = EmbeddingStore(project).get_index(index_id)
    project.db.execute(
        "UPDATE embedding_indexes SET source_policy_hash=? WHERE id=?",
        (
            _receipt_ref(
                project, first.receipt_id, "embedding_index_export_provenance"
            )["source_policy_hash"],
            index_id,
        ),
    )
    project.db.commit()
    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        vid = backend.db.execute(
            "SELECT vector_id FROM embedding_items WHERE index_id=? ORDER BY source_key LIMIT 1",
            (index["id"],),
        ).fetchone()["vector_id"]
        stored_vector = backend.db.execute(
            "SELECT vec FROM embedding_vectors_f32 WHERE id=?", (vid,)
        ).fetchone()["vec"]
        stored_values = struct.unpack("<384f", stored_vector)
        tampered_values = (stored_values[0] + 1.0, *stored_values[1:])
        assert tampered_values[0] != stored_values[0]
        tampered_vector = struct.pack("<384f", *tampered_values)
        assert tampered_vector != stored_vector
        backend.db.execute(
            "UPDATE embedding_vectors_f32 SET vec=? WHERE id=?",
            (tampered_vector, vid),
        )
        backend.db.commit()
    finally:
        backend.close()

    _assert_stale(_run(project, action))


def test_export_replay_rejects_backend_provenance_mismatch(project):
    project, sheet, _cols, tmp_path = project
    out = tmp_path / "out"
    out.mkdir()
    index_id = _create(project, sheet)
    _refresh(project, index_id)
    action = _export_action(index_id, out, key="export-backend-drift")
    result = _run(project, action)
    assert result.status == "completed", result.errors

    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["body"]
    )
    for evidence in body["evidence"]:
        ref = evidence.get("ref", {})
        if ref.get("kind") == "embedding_index_export_provenance":
            ref["vector_backend_version"] = "stale-version"
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(body, sort_keys=True), result.receipt_id),
    )
    project.db.commit()

    _assert_stale(_run(project, action))


def test_export_replay_rejects_digest_evidence_without_source_keys(project):
    project, sheet, _cols, tmp_path = project
    out = tmp_path / "out"
    out.mkdir()
    index_id = _create(project, sheet)
    _refresh(project, index_id)
    action = _export_action(index_id, out, key="export-digest-no-keys")
    result = _run(project, action)
    assert result.status == "completed", result.errors

    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["body"]
    )
    for evidence in body["evidence"]:
        ref = evidence.get("ref", {})
        if ref.get("kind") != "embedding_index_export_items":
            continue
        tuples = [
            [
                item["source_key"],
                item["source_hash"],
                canonical_json(item["source_ref"]),
                item["vector_id"],
                item["vector_sha256"],
                item["dimension"],
            ]
            for item in ref.pop("items")
        ]
        ref.pop("source_keys", None)
        ref["item_count"] = len(tuples)
        ref["items_digest"] = (
            "sha256:"
            + hashlib.sha256(canonical_json(tuples).encode("utf-8")).hexdigest()
        )
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(body, sort_keys=True), result.receipt_id),
    )
    project.db.commit()

    _assert_stale(_run(project, action))


def test_export_replay_rejects_source_freshness_drift_with_artifact_intact(project):
    project, sheet, cols, tmp_path = project
    out = tmp_path / "out"
    out.mkdir()
    index_id = _create(project, sheet)
    _refresh(project, index_id)
    action = _export_action(index_id, out, key="export-source-drift")
    result = _run(project, action)
    assert result.status == "completed", result.errors
    artifact_path = Path(result.outputs[0].ref["path"])
    before = artifact_path.read_bytes()
    row_id = _visible_row_ids(project, sheet)[0]
    replace_test_source_cell(
        project,
        row_id=row_id,
        column_id=cols["headline"],
        value="source freshness drift",
    )

    _assert_stale(_run(project, action))
    assert artifact_path.read_bytes() == before


def test_export_replay_still_reports_deleted_artifact_as_artifact_missing(project):
    project, sheet, _cols, tmp_path = project
    out = tmp_path / "out"
    out.mkdir()
    index_id = _create(project, sheet)
    _refresh(project, index_id)
    action = _export_action(index_id, out, key="export-artifact")
    first = _run(project, action)
    assert first.status == "completed", first.errors
    Path(first.outputs[0].ref["path"]).unlink()

    result = _run(project, action)
    assert result.status == "failed"
    assert result.errors[0].code == "embedding_export_artifact_missing"


def test_sparse_export_receipt_fails_closed(project):
    project, sheet, _cols, tmp_path = project
    out = tmp_path / "out"
    out.mkdir()
    index_id = _create(project, sheet)
    _refresh(project, index_id)
    action = _export_action(index_id, out, key="export-sparse")
    result = _run(project, action)
    assert result.status == "completed", result.errors

    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()["body"]
    )
    body["evidence"] = [
        ev
        for ev in body["evidence"]
        if ev.get("ref", {}).get("kind") != "embedding_index_export_items"
    ]
    for output in body["outputs"]:
        output.get("ref", {}).pop("row_count", None)
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(body, sort_keys=True), result.receipt_id),
    )
    project.db.commit()

    _assert_stale(_run(project, action))


def test_export_filters_out_of_scope_ready_sidecar_rows(project):
    project, sheet, _cols, tmp_path = project
    out = tmp_path / "out"
    out.mkdir()
    index_id = _create(
        project,
        sheet,
        source_query=_source_query(sheet, "story 1"),
        key="create-filtered",
    )
    _refresh(project, index_id)
    store = EmbeddingStore(project)
    space_id = store.get_index(index_id)["space_id"]
    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        backend.upsert_item(
            index_id=index_id,
            space_id=space_id,
            source_key="9999",
            source_ref={"sheet_id": sheet, "row_id": 9999, "columns": ["headline"]},
            source_hash="sha256:orphan",
            status="ready",
            vector=[9.0] + [0.0] * 383,
        )
    finally:
        backend.close()

    result = _run(project, _export_action(index_id, out, key="export-filtered"))
    assert result.status == "completed", result.errors
    lines = Path(result.outputs[0].ref["path"]).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["values"]["headline"] == "story 1"


def test_project_and_cluster_filter_out_of_scope_rows_and_snapshot_replay(project):
    project, sheet, cols, _tmp = project
    index_id = _create(
        project,
        sheet,
        source_query=_source_query(sheet, "story"),
        key="create-analysis-filter",
    )
    _refresh(project, index_id)
    hidden_row_id = _visible_row_ids(project, sheet)[-1]
    backend = VectorBackend(project)
    backend.ensure_schema()
    try:
        assert backend.get_item(index_id, str(hidden_row_id)) is not None
    finally:
        backend.close()
    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (hidden_row_id,))
    project.db.commit()

    project_action = _project_action(index_id, key="project-filter")
    projected = _run(project, project_action)
    assert projected.status == "completed", projected.errors
    assert _child_row_count(project, "projection-project-filter") == 3

    cluster_action = _cluster_action(index_id, key="cluster-filter")
    clustered = _run(project, cluster_action)
    assert clustered.status == "completed", clustered.errors
    assert _child_row_count(project, "cluster-cluster-filter") == 3

    row_id = _visible_row_ids(project, sheet)[0]
    replace_test_source_cell(
        project,
        row_id=row_id,
        column_id=cols["headline"],
        value="source drift after snapshot",
    )
    again = _run(project, project_action)
    assert again.status == "completed"
    assert again.receipt_id == projected.receipt_id
    cluster_again = _run(project, cluster_action)
    assert cluster_again.status == "completed"
    assert cluster_again.receipt_id == clustered.receipt_id
