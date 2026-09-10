"""POST /embeddings/v1/indexes/{index_id}/export route.

Surfaces embedding.index_export from the product: runs the action to a server-side
per-project export dir, returns artifact refs, and applies the stale/incomplete
preflight (no CSV vectors, no browser-specified path).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.ai.embeddings import build_batch_result
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from helpers import replace_test_source_cell


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


def _export(client, pid, index_id, **body):
    return client.post(
        f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export", json=body
    )


def test_export_returns_artifacts_with_sha256(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    resp = _export(client, pid, index_id, formats=["jsonl", "parquet"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["receipt_id"]
    by_fmt = {a["format"]: a for a in body["artifacts"]}
    assert set(by_fmt) == {"jsonl", "parquet"}
    for art in by_fmt.values():
        assert Path(art["path"]).is_file()
        assert art["sha256"].startswith("sha256:")
        assert art["row_count"] == 2
    # written under the server per-project export dir, not a browser path
    assert "exports/embeddings" in by_fmt["jsonl"]["path"]


def test_export_blocks_stale_index(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    # edit a source cell -> the stored vector is now stale
    cat = next(rid for rid, v in project.get_values(sheet, col).items() if v == "cat")
    replace_test_source_cell(
        project,
        row_id=cat,
        column_id=col,
        value="panther",
    )
    resp = _export(client, pid, index_id)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "embedding_source_stale"


def test_export_no_ready_vectors_fails(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create(project, pid, sheet)  # never refreshed
    resp = _export(client, pid, index_id)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] in (
        "embedding_export_no_ready_vectors",
        "embedding_export_sidecar_missing",
    )


def _append_row(project, sheet, headline):
    col = next(c["id"] for c in project.columns(sheet) if c["name"] == "headline")
    project.add_rows(sheet, [{"headline": headline}], {"headline": col})


def test_export_blocks_incomplete_index_then_succeeds_after_refresh(tmp_path):
    # An appended (but unembedded) source row makes the index incomplete; the export
    # must NOT silently write a partial artifact of only the old ready rows.
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    _append_row(project, sheet, "lion")

    artifact = project.path / "exports" / "embeddings" / f"{index_id}.embeddings.jsonl"
    resp = _export(client, pid, index_id, formats=["jsonl"])
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "embedding_index_incomplete"
    assert detail["freshness_reason"] == "missing_rows"
    assert not artifact.exists()  # nothing written

    # refresh embeds the appended row -> export now succeeds and covers all 3 rows
    _refresh(project, pid, index_id, key="r@2")
    ok = _export(client, pid, index_id, formats=["jsonl"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["artifacts"][0]["row_count"] == 3


def test_download_409_when_index_no_longer_fresh(tmp_path):
    # A previously-exported artifact must not stream once the index drifts; the
    # "latest by index/format" file is a stale snapshot -> 409, not 200.
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    assert _export(client, pid, index_id, formats=["jsonl"]).status_code == 200
    url = f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/jsonl"
    assert client.get(url).status_code == 200  # fresh -> downloadable

    _append_row(project, sheet, "lion")
    blocked = client.get(url)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "embedding_index_incomplete"

    # re-export after refresh restores a fresh, downloadable artifact
    _refresh(project, pid, index_id, key="r@2")
    assert _export(client, pid, index_id, formats=["jsonl"]).status_code == 200
    again = client.get(url)
    assert again.status_code == 200
    assert len(again.text.strip().splitlines()) == 3


def test_export_artifact_is_downloadable(tmp_path):
    client = _client(tmp_path)
    pid, project, sheet, col = _seed(client)
    index_id = _create(project, pid, sheet)
    _refresh(project, pid, index_id)
    # before any export -> 404
    assert (
        client.get(
            f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/jsonl"
        ).status_code
        == 404
    )
    # run the export, then download the JSONL artifact
    assert _export(client, pid, index_id, formats=["jsonl"]).status_code == 200
    dl = client.get(
        f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/jsonl"
    )
    assert dl.status_code == 200
    assert "attachment" in dl.headers.get("content-disposition", "")
    assert dl.headers["content-type"].startswith("application/x-ndjson")
    # one JSONL line per ready row, with a vector
    lines = dl.text.strip().splitlines()
    assert len(lines) == 2
    assert "vector" in json.loads(lines[0])
    # an unknown format -> 404
    assert (
        client.get(
            f"/api/projects/{pid}/embeddings/v1/indexes/{index_id}/export/csv"
        ).status_code
        == 404
    )
