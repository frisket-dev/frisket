from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _client(tmp_path) -> TestClient:
    app = create_app(tmp_path)
    return TestClient(app)


def _project_id(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "marketplace-policy"})
    assert response.status_code == 200
    return response.json()["id"]


def test_workbench_marketplace_discovery_policy_is_backend_project_scoped(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    pid = _project_id(client)

    response = client.get(
        f"/api/projects/{pid}/workbench/marketplace",
        params={"contribution_id": "frisket.geo.view.map", "query": "geo"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schemaVersion"] == "frisket.plugin_marketplace_query.v1"
    assert payload["projectId"] == pid
    assert payload["contributionId"] == "frisket.geo.view.map"
    assert payload["text"] == "geo"
    assert payload["policySource"] == "backend"
    assert payload["arbitraryPackageLoadAllowed"] is False
    assert payload["allowedActions"] == ["view_index_entry", "request_review"]
    assert payload["disabledActions"] == ["install", "enable", "trust", "load_package"]
    assert payload["resultCount"] == 2

    entries = {entry["pluginId"]: entry for entry in payload["entries"]}
    assert set(entries) == {"community.geo-map", "community.bottom-dock-network"}
    assert (
        entries["community.geo-map"]["schemaVersion"] == "frisket.plugin_index_entry.v1"
    )
    assert entries["community.geo-map"]["source"] == {
        "kind": "marketplace",
        "value": "community.geo-map",
    }
    assert entries["community.geo-map"]["compatibility"] == "compatible"
    assert (
        entries["community.geo-map"]["disabledReason"] == "marketplace_review_required"
    )
    assert entries["community.geo-map"]["installActionsDisabled"] is True
    assert entries["community.bottom-dock-network"]["compatibility"] == "incompatible"
    assert (
        entries["community.bottom-dock-network"]["disabledReason"]
        == "version_incompatible"
    )

    network = client.get(
        f"/api/projects/{pid}/workbench/marketplace",
        params={"contribution_id": "frisket.geo.view.map", "query": "network"},
    )
    assert network.status_code == 200
    assert network.json()["resultCount"] == 1
    assert network.json()["entries"][0]["pluginId"] == "community.bottom-dock-network"


def test_workbench_marketplace_install_attempt_fails_closed_without_layout_mutation(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    pid = _project_id(client)

    response = client.post(
        f"/api/projects/{pid}/workbench/marketplace/install-attempt",
        json={
            "pluginId": "community.graph",
            "version": "0.9.0",
            "source": {"kind": "marketplace", "value": "community.graph"},
            "arbitraryPackageLoadAllowed": False,
        },
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["schemaVersion"] == "frisket.plugin_install_attempt.v1"
    assert payload["projectId"] == pid
    assert payload["pluginId"] == "community.graph"
    assert payload["source"] == {"kind": "marketplace", "value": "community.graph"}
    assert payload["installState"] == "failed"
    assert payload["failure"] == {
        "code": "checksum_mismatch",
        "message": "Downloaded plugin checksum did not match index entry.",
        "retryable": False,
    }
    assert payload["layoutMutated"] is False
    assert payload["failureVisible"] is True
    assert payload["arbitraryPackageLoadAllowed"] is False
