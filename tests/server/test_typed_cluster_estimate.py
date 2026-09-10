"""Cluster estimates and consent use the same typed public request as execution."""

import pytest
from fastapi.testclient import TestClient

from frisket.engine.runner.validation import NetworkDisabled
from frisket.server.app import create_app
from tests.engine.test_cluster_values_executor import _remote_embedding_router


@pytest.fixture
def cluster_client(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    router, adapter = _remote_embedding_router()
    with TestClient(create_app(tmp_path / "workspace", router=router)) as client:
        pid = client.post("/api/projects", json={"name": "Cluster estimate"}).json()[
            "id"
        ]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Names")
        column = project.add_column(sheet, "name")
        project.add_rows(
            sheet,
            [{"name": value} for value in ("Jon Smith", "Smith Jon")],
            {"name": column},
        )
        project.db.commit()
        request = {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "name", "method": "semantic"},
            "output_names": {"canonical": "Reviewed"},
            "idempotency_key": "cluster-http-consent",
        }
        yield client, pid, project, request, adapter


@pytest.mark.parametrize("method", ["fingerprint", "semantic"])
def test_cluster_estimate_has_no_provider_or_project_effects(cluster_client, method):
    client, pid, project, request, adapter = cluster_client
    request["params"]["method"] = method
    before = project.db.total_changes
    response = client.post(
        f"/api/projects/{pid}/actions/v1/estimate", json={"action": request}
    )
    assert response.status_code == 200, response.text
    estimate = response.json()["estimate"]
    assert estimate["rows"] == 2
    if method == "fingerprint":
        assert estimate["cost"] == 0
    else:
        assert estimate["requires_confirmation"] is True
    assert adapter.calls == []
    assert project.db.total_changes == before


def test_cluster_run_requires_exact_confirmation_before_queueing(cluster_client):
    client, pid, project, request, adapter = cluster_client
    url = f"/api/projects/{pid}/actions/v1/run"
    quote = client.post(url, json=request)
    assert quote.status_code == 402, quote.text
    digest = quote.json()["errors"][0]["details"]["promise_set_hash"]
    assert len(digest) == 64
    wrong = client.post(url, json={**request, "confirmation": "0" * 64})
    assert wrong.status_code == 402, wrong.text
    assert adapter.calls == []
    assert not project.db.execute("SELECT 1 FROM runs").fetchone()
    accepted = client.post(url, json={**request, "confirmation": digest})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "queued"
    assert adapter.calls == []


@pytest.mark.parametrize("endpoint", ["estimate", "run"])
def test_cluster_remote_network_off_is_a_named_refusal(cluster_client, endpoint):
    client, pid, project, request, adapter = cluster_client
    project.set_network_policy(mode="off")
    before = project.db.total_changes
    response = client.post(
        f"/api/projects/{pid}/actions/v1/{endpoint}",
        json={"action": request} if endpoint == "estimate" else request,
    )
    assert response.status_code in {400, 403}, response.text
    body = response.json()
    if endpoint == "estimate":
        assert body["detail"] == str(NetworkDisabled("model:embed")), body
    else:
        assert body["errors"][0]["code"] == "network_disabled", body
    assert adapter.calls == []
    assert project.db.total_changes == before
