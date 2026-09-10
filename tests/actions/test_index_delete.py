from __future__ import annotations

import json
from typing import Any

import pytest

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DeletedEmbeddingIndex,
    IndexDeleter,
)
from frisket.ai.embeddings import EmbeddingStore, VectorBackend
from frisket.engine.executor import resolve_map_preview, run_action_spec
from frisket.engine.executor.action_families.embeddings import (
    run_typed_embedding_action,
)
from frisket.engine.store import Project
from tests.engine.test_embedding_index_export_executor import (
    _index_create_action,
    _refresh,
    _seed,
)


def _request(index_id: str, **params: Any) -> dict[str, Any]:
    return {
        "action_id": "embedding.index_delete",
        "scope": {"kind": "project"},
        "params": {"index_id": index_id, **params},
        "idempotency_key": "typed-delete",
    }


class CustomDeleteParams(ActionParams):
    selected: str


def custom_delete(
    params: CustomDeleteParams, indexes: IndexDeleter
) -> DeletedEmbeddingIndex:
    result = indexes.delete(params.selected.removeprefix("chosen:"))
    # Reconstructing the domain result is allowed, but it cannot rewrite facts.
    return result.model_copy(update={"index_id": "invented", "deleted_items": 999})


def _bound(selected: str) -> BoundTypedActionRequest:
    definition = action(
        name="discard",
        title="Discard index",
        description="Delete the selected index.",
        category=ActionCategory.SOURCES,
        run=custom_delete,
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.discard")
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.discard",
            scope={"kind": "project"},
            params={"selected": selected},
            idempotency_key="custom-delete",
        ),
    )


def test_reused_deleter_uses_actual_arguments_not_returned_claims(tmp_path):
    project = Project.create(tmp_path / "project")
    try:
        seeded = _seed(project, tmp_path)
        index_id = seeded["index_id"]
        bound = _bound(f"chosen:{index_id}")
        result = run_typed_embedding_action(project, "p", bound)
        assert result.status == "completed", result.errors
        ref = result.outputs[0].ref
        assert ref["kind"] == "embedding_index_delete"
        assert ref["index_id"] == index_id
        assert ref["deleted_items"] == 3
        assert EmbeddingStore(project).get_index(index_id) is None
        backend = VectorBackend(project)
        backend.ensure_schema()
        try:
            assert backend.item_counts(index_id) == {}
        finally:
            backend.close()
        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()[0]
        )
        assert receipt["action_kind"] == "custom.discard"
        assert receipt["outputs"][0]["ref"] == ref
        replay = run_typed_embedding_action(project, "p", bound)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()


def test_delete_preserves_sources_blobs_and_other_indexes(tmp_path):
    from frisket.engine.executor.embedding_export import embedding_export_dir

    project = Project.create(tmp_path / "project")
    try:
        seeded = _seed(project, tmp_path)
        index_id = seeded["index_id"]
        other = run_action_spec(
            project,
            _index_create_action(seeded["sheet_id"], key="other-index"),
            project_id="p",
        )
        assert other.status == "completed", other.errors
        other_id = other.outputs[0].ref["index_id"]
        _refresh(project, other_id, key="other-refresh")
        store = EmbeddingStore(project)
        space_id = store.get_index(index_id)["space_id"]
        other_metadata = dict(store.get_index(other_id))
        assert other_metadata["space_id"] == space_id
        space = dict(store.get_space(space_id))

        content = b"original source attachment"
        digest = project.add_blob(content, filename="source.txt", mime="text/plain")
        source_sheet = project.add_sheet("Attachments")
        source_column = project.add_column(source_sheet, "source", "file")
        project.add_rows(
            source_sheet,
            [
                {
                    "source": {
                        "blob": digest,
                        "filename": "source.txt",
                        "mime": "text/plain",
                    }
                }
            ],
            {"source": source_column},
        )

        def source_snapshot():
            return {
                table: [
                    tuple(row) for row in project.db.execute(f"SELECT * FROM {table}")
                ]
                for table in ("sheets", "columns", "rows", "cells", "blobs")
            }

        sources = source_snapshot()
        exports = embedding_export_dir(project)
        exports.mkdir(parents=True)
        deleted_export = exports / f"{index_id}.embeddings.jsonl"
        kept_export = exports / f"{other_id}.embeddings.jsonl"
        deleted_export.write_text("selected index")
        kept_export.write_text("other index")
        backend = VectorBackend(project)
        try:
            other_vectors = backend.index_binding(other_id)
            result = run_action_spec(project, _request(index_id), project_id="p")
            assert result.status == "completed", result.errors
            assert result.outputs[0].ref["deleted_items"] == 3
            assert backend.item_counts(index_id) == {}
            assert backend.item_counts(other_id) == {"ready": 3}
            assert backend.index_binding(other_id) == other_vectors
            assert (
                backend.db.execute(
                    "SELECT COUNT(*) FROM embedding_vectors_f32"
                ).fetchone()[0]
                == 3
            )
        finally:
            backend.close()
        assert store.get_index(index_id) is None
        assert dict(store.get_index(other_id)) == other_metadata
        assert dict(store.get_space(space_id)) == space
        assert source_snapshot() == sources
        assert project.read_blob(digest) == content
        assert not deleted_export.exists()
        assert kept_export.read_text() == "other index"
        assert run_action_spec(project, _request(index_id), project_id="p") == result
    finally:
        project.close()


@pytest.mark.parametrize("selected", ["chosen:", "chosen: ", " "])
def test_reused_deleter_validates_derived_arguments_before_mutation(tmp_path, selected):
    project = Project.create(tmp_path / "project")
    try:
        seeded = _seed(project, tmp_path)
        index_id = seeded["index_id"]
        bound = _bound(selected)
        result = run_typed_embedding_action(project, "p", bound)
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_params"
        assert EmbeddingStore(project).get_index(index_id) is not None
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key='custom-delete'"
            ).fetchone()[0]
            == 0
        )
        backend = VectorBackend(project)
        backend.ensure_schema()
        try:
            assert sum(backend.item_counts(index_id).values()) == 3
        finally:
            backend.close()
    finally:
        project.close()


@pytest.mark.parametrize(
    "params",
    [
        {"index_id": " "},
        {"delete_sidecar_vectors": False},
        {"delete_sidecar_vectors": True},
        {"delete_sidecar_vectors": 1},
        {"delete_sidecar_vectors": []},
        {"unrecognized": True},
    ],
)
def test_typed_delete_params_are_strict(params):
    request = _request("index")
    request["params"].update(params)
    result = validate_root_action(request)
    assert result.ok is False
    assert result.error.code == "invalid_action_request"


def test_delete_catalog_has_no_vector_retention_option():
    catalog = ACTION_REGISTRY.get("embedding.index_delete").catalog_entry()
    assert set(catalog["input_schema"]["properties"]) == {"index_id"}
    assert catalog["examples"][0]["params"] == {"index_id": "example-index"}
    assert "delete_sidecar_vectors" not in catalog["output_schema"]["properties"]


def test_delete_preview_refuses_before_any_effect(tmp_path):
    project = Project.create(tmp_path / "project")
    try:
        seeded = _seed(project, tmp_path)
        index_id = seeded["index_id"]
        before = project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        result = resolve_map_preview(project, _request(index_id))
        assert result.code == "unsupported_action_kind"
        assert EmbeddingStore(project).get_index(index_id) is not None
        assert (
            project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == before
        )
        backend = VectorBackend(project)
        backend.ensure_schema()
        try:
            assert sum(backend.item_counts(index_id).values()) == 3
        finally:
            backend.close()
    finally:
        project.close()


def test_retired_delete_envelope_refuses(tmp_path):
    project = Project.create(tmp_path / "project")
    try:
        result = run_action_spec(
            project,
            {
                "schema_version": "frisket.action.v2",
                "kind": "embedding.index_delete",
                "capabilities": ["project:write"],
                "params": {"index_id": "missing", "delete_sidecar_vectors": True},
                "idempotency_key": "retired-delete",
            },
            project_id="p",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_action_request"
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        project.close()


def test_receipt_failure_rolls_back_project_not_external_sidecar_or_artifacts(
    tmp_path, monkeypatch
):
    from frisket.engine.executor import embedding_delete
    from frisket.engine.executor.embedding_export import embedding_export_dir

    project = Project.create(tmp_path / "project")
    try:
        seeded = _seed(project, tmp_path)
        index_id = seeded["index_id"]
        store = EmbeddingStore(project)
        before_index = dict(store.get_index(index_id))
        before_space = dict(store.get_space(before_index["space_id"]))
        before_receipts = project.db.execute(
            "SELECT COUNT(*) FROM receipts"
        ).fetchone()[0]
        directory = embedding_export_dir(project)
        directory.mkdir(parents=True)
        artifact = directory / f"{index_id}.embeddings.jsonl"
        artifact.write_text("exported artifact")

        def fail_receipt(project, receipt):
            assert project.db.in_transaction
            assert EmbeddingStore(project).get_index(index_id) is None
            raise RuntimeError("injected receipt write failure")

        monkeypatch.setattr(embedding_delete, "_insert_receipt", fail_receipt)
        result = run_action_spec(project, _request(index_id), project_id="p")
        assert result.status == "failed"
        assert result.receipt_id is None
        assert dict(store.get_index(index_id)) == before_index
        assert dict(store.get_space(before_index["space_id"])) == before_space
        assert (
            project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            == before_receipts
        )
        # These effects use separate storage and are not undone by project SQL.
        assert not artifact.exists()
        backend = VectorBackend(project)
        backend.ensure_schema()
        try:
            assert backend.item_counts(index_id) == {}
        finally:
            backend.close()
    finally:
        project.close()


@pytest.mark.parametrize("transport", ["cli", "mcp"])
def test_generic_transport_runs_typed_index_delete(tmp_path, capsys, transport):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project_path = workspace / "demo.frisket"
    project = Project.create(project_path)
    try:
        seeded = _seed(project, tmp_path)
        index_id = seeded["index_id"]
    finally:
        project.close()
    request = _request(index_id)
    if transport == "cli":
        from frisket.cli.action import action as cli_action

        request_file = tmp_path / "request.json"
        request_file.write_text(json.dumps(request))
        assert (
            cli_action(
                [
                    "run",
                    "--project",
                    str(project_path),
                    "--project-id",
                    "demo",
                    str(request_file),
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "completed"
        assert result["run_id"] is None
    else:
        from frisket.server.mcp import LocalBackend
        from tests.server.test_mcp_server import call

        backend = LocalBackend(workspace)
        try:
            result = call(
                backend, "run_action", {"project_id": "demo", "action": request}
            )
            assert result["status"] == "completed"
            assert result["done"] is True
        finally:
            backend.ws.get("demo").close()
    assert result["receipt_id"]
    assert result["outputs"][0]["ref"]["index_id"] == index_id
    assert result["outputs"][0]["ref"]["deleted_items"] == 3
    project = Project(project_path)
    try:
        assert EmbeddingStore(project).get_index(index_id) is None
        backend = VectorBackend(project)
        backend.ensure_schema()
        try:
            assert backend.item_counts(index_id) == {}
        finally:
            backend.close()
    finally:
        project.close()
