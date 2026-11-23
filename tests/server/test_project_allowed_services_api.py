"""Project API for Allowed services, usage-data sharing and approved endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_create_accepts_both_settings_and_names_an_empty_project(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    made = client.post(
        "/api/projects",
        json={
            "name": "  ",
            "allowed_services": "approved_services",
            "usage_data_sharing": "off",
        },
    )
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["name"] == "Untitled project"
    assert body["allowed_services"] == "approved_services"
    assert body["usage_data_sharing"] == "off"
    assert body["sensitive"] is True
    listed = client.get("/api/projects").json()
    assert [row["allowed_services"] for row in listed] == ["approved_services"]


def test_restricted_services_leave_usage_data_sharing_alone(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Notes"}).json()["id"]
    changed = client.patch(
        f"/api/projects/{pid}/allowed-services",
        json={"allowed_services": "this_machine_only"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["usage_data_sharing"] == "inherit"
    assert changed.json()["sensitive"] is False


def test_looser_change_needs_the_value_confirmed(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post(
        "/api/projects",
        json={"name": "Notes", "allowed_services": "this_machine_only"},
    ).json()["id"]
    refused = client.patch(
        f"/api/projects/{pid}/allowed-services",
        json={"allowed_services": "all_services"},
    )
    assert refused.status_code == 400, refused.text
    assert client.get(f"/api/projects/{pid}").json()["allowed_services"] == (
        "this_machine_only"
    )
    confirmed = client.patch(
        f"/api/projects/{pid}/allowed-services",
        json={
            "allowed_services": "all_services",
            "confirm_allowed_services": "all_services",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    history = client.app.state.workspace.get(pid).history()
    assert history[-1]["kind"] == "allowed_services"


def test_usage_data_sharing_is_changeable(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Notes"}).json()["id"]
    changed = client.patch(
        f"/api/projects/{pid}/usage-data-sharing", json={"usage_data_sharing": "on"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["usage_data_sharing"] == "on"
    assert changed.json()["allowed_services"] == "all_services"


def test_cloud_does_not_offer_this_machine_only(tmp_path) -> None:
    local = TestClient(create_app(tmp_path / "local"))
    assert local.get("/api/config").json()["allowed_services_offered"] == [
        "this_machine_only",
        "approved_services",
        "all_services",
    ]
    cloud = TestClient(create_app(tmp_path / "cloud", edition="cloud"))
    assert cloud.get("/api/config").json()["allowed_services_offered"] == [
        "approved_services",
        "all_services",
    ]
    refused = cloud.post(
        "/api/projects",
        json={"name": "Notes", "allowed_services": "this_machine_only"},
    )
    assert refused.status_code == 400, refused.text
    assert "not offered on Frisket Cloud" in refused.text
    pid = cloud.post("/api/projects", json={"name": "Notes"}).json()["id"]
    changed = cloud.patch(
        f"/api/projects/{pid}/allowed-services",
        json={"allowed_services": "this_machine_only"},
    )
    assert changed.status_code == 400, changed.text


def test_approved_endpoints_round_trip_and_start_empty(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    assert client.get("/api/approved-endpoints").json() == {"endpoints": []}
    saved = client.put(
        "/api/approved-endpoints",
        json={
            "endpoints": [
                {"host": "LLM.Newsroom.example.test", "port": 443, "label": "Newsroom"}
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    assert client.get("/api/approved-endpoints").json() == {
        "endpoints": [
            {"host": "llm.newsroom.example.test", "port": 443, "label": "Newsroom"}
        ]
    }
    bad = client.put(
        "/api/approved-endpoints",
        json={"endpoints": [{"host": "https://evil.example.test/x"}]},
    )
    assert bad.status_code == 422
