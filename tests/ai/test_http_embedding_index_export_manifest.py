"""export artifact manifest (latest-current) + orphan cleanup.

GET .../indexes/{id}/export returns a manifest joining the on-disk artifacts to the
originating export receipt's provenance + live freshness, so an external analyst can
identify the model/space/time from the manifest alone. Deleting the index unlinks the
orphan artifact files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.ai.embeddings import build_batch_result
from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.action_families.embeddings import (
    run_typed_embedding_action,
)
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.sdk import ActionParams, IndexExport, IndexExporter, action


class _ArchiveParams(ActionParams):
    index: str
    directory: str


def _archive(params: _ArchiveParams, files: IndexExporter) -> IndexExport:
    return files.export(params.index, path=params.directory, formats=["jsonl"])


class _Gateway:
    def embed(self, texts, *, provider, model, modality):
        return build_batch_result(
            [[1.0] + [0.0] * 383 for _ in texts],
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _client(tmp_path):
    return TestClient(create_app(tmp_path / "ws", router=ModelRouter(keys={})))


def _seed(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("s")
    col = project.add_column(sheet, "headline")
    project.add_rows(
        sheet, [{"headline": t} for t in ("cat", "kitten")], {"headline": col}
    )
    return pid, project, sheet, col


def _create(project, pid, sheet):
    r = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": "fastembed",
                "source_policy": {"kind": "text_cell"},
                "provider_policy": {"allow_remote": False},
            },
            "idempotency_key": "c@1",
        },
        project_id=pid,
    )
    return r.outputs[0].ref["index_id"]


def _refresh(project, pid, index_id, key="r@1"):
    run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": key,
        },
        project_id=pid,
        deps=ExecutorDeps(embedding_gateway=_Gateway()),
    )


def _delete(project, pid, index_id):
    return run_action_spec(
        project,
        {
            "action_id": "embedding.index_delete",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id},
            "idempotency_key": "d@1",
        },
        project_id=pid,
    )


def _export(client, pid, index_id, **body):
    return client.post(
        f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export", json=body
    )


def _append_row(project, sheet, headline):
    col = next(c["id"] for c in project.columns(sheet) if c["name"] == "headline")
    project.add_rows(sheet, [{"headline": headline}], {"headline": col})


def _manifest(client, pid, index_id):
    return client.get(f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export")


def test_export_manifest_lists_current_artifacts_with_full_provenance(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, _col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    assert (
        _export(client, pid, index_id, formats=["jsonl", "parquet"]).status_code == 200
    )

    resp = _manifest(client, pid, index_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["index_id"] == index_id
    assert body["freshness"]["downloadable"] is True
    assert body["freshness"]["error_code"] is None

    prov = body["provenance"]
    # enough to identify the model/space that produced the vectors
    for key in (
        "provider_id",
        "actual_model_id",
        "dimension",
        "distance_metric",
        "source_policy_hash",
        "vector_backend_id",
    ):
        assert prov.get(key) not in (None, ""), key

    arts = {a["format"]: a for a in body["artifacts"]}
    assert set(arts) == {"jsonl", "parquet"}
    for art in arts.values():
        assert art["download_url"].endswith(f"/export/{art['format']}")
        assert art["row_count"] == 2
        assert art["byte_count"] > 0
        assert art["sha256_recorded"].startswith("sha256:")
        assert art["sha256_current"] == art["sha256_recorded"]
        assert art["matches_receipt"] is True
        assert art["exported_at"]
        assert art["receipt_id"]


def test_export_manifest_reports_stale_not_downloadable_after_drift(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, _col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    assert _export(client, pid, index_id, formats=["jsonl"]).status_code == 200

    _append_row(project, sheet, "lion")  # index now incomplete; file unchanged
    body = _manifest(client, pid, index_id).json()
    assert body["freshness"]["downloadable"] is False
    assert body["freshness"]["error_code"] == "embedding_index_incomplete"
    art = body["artifacts"][0]
    assert art["matches_receipt"] is True  # the bytes still match their receipt
    # and the {fmt} download agrees (409)
    assert (
        client.get(
            f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/jsonl"
        ).status_code
        == 409
    )


def test_delete_index_removes_export_artifacts_and_manifest_404s(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, _col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    assert (
        _export(client, pid, index_id, formats=["jsonl", "parquet"]).status_code == 200
    )
    art_dir = project.path / "exports" / "embeddings"
    assert list(art_dir.glob(f"{index_id}.embeddings.*"))  # files exist

    res = _delete(project, pid, index_id)
    assert res.status == "completed", res.errors
    assert res.outputs[0].ref["deleted_export_artifacts"] >= 2
    assert not list(art_dir.glob(f"{index_id}.embeddings.*"))  # files gone

    assert _manifest(client, pid, index_id).status_code == 404
    assert (
        client.get(
            f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/jsonl"
        ).status_code
        == 404
    )


def _download(client, pid, index_id, fmt="jsonl"):
    return client.get(
        f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/{fmt}"
    )


def test_download_refuses_tampered_artifact(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, _ = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    exported = _export(client, pid, index_id, formats=["jsonl"])
    assert exported.status_code == 200, exported.text
    artifact = project.path / "exports" / "embeddings" / f"{index_id}.embeddings.jsonl"
    artifact.write_text("tampered\n")

    blocked = _download(client, pid, index_id)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "embedding_export_artifact_missing"
    manifest = _manifest(client, pid, index_id).json()
    assert manifest["freshness"]["downloadable"] is True  # index freshness only
    assert manifest["artifacts"][0]["matches_receipt"] is False


def test_refreshed_index_does_not_validate_old_export_snapshot(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, _ = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    assert _export(client, pid, index_id, formats=["jsonl"]).status_code == 200
    _append_row(project, sheet, "lion")
    _refresh(project, pid, index_id, key="r@2")

    manifest = _manifest(client, pid, index_id).json()
    assert manifest["freshness"]["downloadable"] is True
    assert manifest["artifacts"][0]["matches_receipt"] is True
    blocked = _download(client, pid, index_id)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "stale_replay"
    assert _export(client, pid, index_id, formats=["jsonl"]).status_code == 200
    assert _download(client, pid, index_id).status_code == 200


def test_successive_format_exports_keep_each_artifacts_receipt(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, _ = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    first = _export(client, pid, index_id, formats=["jsonl", "arrow"])
    assert first.status_code == 200, first.text
    second = _export(client, pid, index_id, formats=["arrow"], include_vectors=False)
    assert second.status_code == 200, second.text
    assert first.json()["receipt_id"] != second.json()["receipt_id"]

    manifest = _manifest(client, pid, index_id).json()
    artifacts = {item["format"]: item for item in manifest["artifacts"]}
    assert artifacts["jsonl"]["receipt_id"] == first.json()["receipt_id"]
    assert artifacts["arrow"]["receipt_id"] == second.json()["receipt_id"]
    for fmt, artifact in artifacts.items():
        assert artifact["matches_receipt"] is True
        # JSONL's older receipt also describes the now-replaced Arrow. Download
        # must validate only the requested file, not reject its changed sibling.
        assert _download(client, pid, index_id, fmt).status_code == 200


def test_external_destination_export_does_not_claim_managed_artifact(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, _ = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    managed = _export(client, pid, index_id, formats=["jsonl"])
    assert managed.status_code == 200, managed.text
    (tmp_path / "elsewhere").mkdir()
    external = run_action_spec(
        project,
        {
            "action_id": "embedding.index_export",
            "scope": {"kind": "project"},
            "params": {
                "index_id": index_id,
                "destination": {
                    "kind": "local_dir",
                    "path": str(tmp_path / "elsewhere"),
                },
                "formats": ["jsonl"],
                "include_vectors": False,
            },
            "idempotency_key": "external-export",
        },
        project_id=pid,
    )
    assert external.status == "completed", external.errors
    manifest = _manifest(client, pid, index_id).json()
    assert manifest["artifacts"][0]["receipt_id"] == managed.json()["receipt_id"]
    assert _download(client, pid, index_id).status_code == 200


def test_download_refuses_file_without_receipt(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, _ = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    artifact = project.path / "exports" / "embeddings" / f"{index_id}.embeddings.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("not an export\n")
    assert _download(client, pid, index_id).status_code == 409


@pytest.mark.parametrize("aliased", [False, True])
def test_custom_action_receipt_supports_managed_manifest_and_download(
    tmp_path, aliased
):
    client = _client(tmp_path)
    pid, project, sheet, _ = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    directory = project.path / "exports" / "embeddings"
    directory.mkdir(parents=True)
    destination = directory / ".." / "embeddings" if aliased else directory
    definition = action(
        name="archive",
        title="Archive",
        description="Export an index.",
        category=ActionCategory.SOURCES,
        run=_archive,
    )
    registered = ActionRegistry(
        (ActionNamespace("custom", actions=(definition,)),)
    ).get("custom.archive")
    request = BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id="custom.archive",
            scope={"kind": "project"},
            params={"index": index_id, "directory": str(destination)},
            idempotency_key="custom-managed-export",
        ),
    )
    result = run_typed_embedding_action(project, pid, request)
    assert result.status == "completed", result.errors
    assert Path(result.outputs[0].ref["path"]).parent == directory.resolve()
    manifest = _manifest(client, pid, index_id).json()
    assert manifest["artifacts"][0]["receipt_id"] == result.receipt_id
    assert manifest["artifacts"][0]["matches_receipt"] is True
    assert manifest["provenance"]["index_id"] == index_id
    assert _download(client, pid, index_id).status_code == 200
