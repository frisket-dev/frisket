"""HTTP contract for the read-only cluster preview route.

POST /api/projects/{pid}/clusters/v1/preview is the receipt-free, non-action
twin of the cluster commit action. It writes nothing (no receipt, no columns,
no ops) and returns the group data + value_hash the web review UI renders.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from helpers import make_client as _client


def _seed(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "Cluster Preview"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("orgs")
    cols = {"org": project.add_column(sheet_id, "org")}
    project.add_rows(
        sheet_id,
        [
            {"org": "ACME Corp"},
            {"org": "ACME Corp"},
            {"org": "acme  corp"},
            {"org": "Banana Farms"},
            {"org": "Banana Farms"},
            {"org": "banana farms"},
        ],
        cols,
    )
    return pid, sheet_id


def test_cluster_preview_returns_groups_without_side_effects(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    project = client.app.state.workspace.get(pid)
    ops_before = len(project.history())

    resp = client.post(
        f"/api/projects/{pid}/clusters/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "org", "method": "fingerprint"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["method"] == "fingerprint"
    assert body["count"] == 2
    assert body["value_hash"].startswith("sha256:")
    assert body["semantic"] is False
    keys = {c["key"] for c in body["clusters"]}
    assert len(keys) == 2

    # read-only: no receipt, no op, no new column
    assert len(project.history()) == ops_before
    assert [c["name"] for c in project.columns(sheet_id)] == ["org"]


def test_cluster_preview_ngram_method(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "P"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("orgs")
    cols = {"c": project.add_column(sheet_id, "c")}
    project.add_rows(
        sheet_id, [{"c": "Sao Paulo"}, {"c": "SaoPaulo"}, {"c": "sao paulo"}], cols
    )
    resp = client.post(
        f"/api/projects/{pid}/clusters/v1/preview",
        json={
            "sheet_id": sheet_id,
            "input_column": "c",
            "method": "ngram_fingerprint",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["method"] == "ngram_fingerprint"
    assert body["ngram_size"] == 2
    assert body["count"] == 1


def test_cluster_preview_bad_column_is_bare_v1_error(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    resp = client.post(
        f"/api/projects/{pid}/clusters/v1/preview",
        json={"sheet_id": sheet_id, "input_column": "missing"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "detail" not in body
    assert body["code"] == "invalid_input_ref"
    assert body["field"] == "input_column"


def test_cluster_preview_rejects_irrelevant_knob(tmp_path):
    client = _client(tmp_path)
    pid, sheet_id = _seed(client)
    resp = client.post(
        f"/api/projects/{pid}/clusters/v1/preview",
        json={
            "sheet_id": sheet_id,
            "input_column": "org",
            "method": "fingerprint",
            "threshold": 0.9,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["field"] == "threshold"
