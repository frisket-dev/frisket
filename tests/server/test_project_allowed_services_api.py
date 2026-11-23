"""Project API for Allowed services, usage-data sharing and approved endpoints."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from frisket.engine.store import Project

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
    assert client.get("/api/approved-endpoints").json() == {
        "endpoints": [],
        "editable": True,
    }
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
        ],
        "editable": True,
    }
    bad = client.put(
        "/api/approved-endpoints",
        json={"endpoints": [{"host": "https://evil.example.test/x"}]},
    )
    assert bad.status_code == 422


def _legacy_creation(root, *, state: str, key: str, sensitive: bool) -> str:
    """A project and its idempotent creation record as the previous release
    left them: a v3 record binding ``sensitive``, a manifest and meta with no
    Allowed services or usage-data values."""
    project_id = "legacy"
    bundle = root / f"{project_id}.frisket"
    project = Project.create(bundle, "Legacy")
    storage_id = project.storage_identity
    project.close()
    with sqlite3.connect(bundle / "project.db") as db:
        db.execute(
            "DELETE FROM meta WHERE key IN ('usage_data_sharing', 'allowed_services')"
        )
        db.execute(
            "INSERT INTO meta (key, value) VALUES ('sensitive', ?)",
            ("true" if sensitive else "false",),
        )
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("usage_data_sharing")
    manifest.pop("allowed_services")
    manifest["sensitive"] = sensitive
    manifest_path.write_text(json.dumps(manifest))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    record = {
        "schema_version": "frisket.project_creation_operation.v3",
        "operation_identity_sha256": digest,
        "state": state,
        "project": {
            "id": project_id,
            "name": "Legacy",
            "description": "",
            "sensitive": sensitive,
        },
    }
    if state == "complete":
        record["storage_id"] = storage_id
    operations = root / ".project-creation-operations"
    operations.mkdir()
    (operations / f"{digest}.json").write_text(json.dumps(record))
    return project_id


@pytest.mark.parametrize("state", ["pending", "complete"])
@pytest.mark.parametrize("sensitive", [True, False])
def test_retry_of_a_creation_recorded_by_the_previous_release(
    tmp_path, state: str, sensitive: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project_id = _legacy_creation(
        root, state=state, key="legacy-create", sensitive=sensitive
    )
    client = TestClient(create_app(root))
    retried = client.post(
        "/api/projects",
        json={
            "name": "Legacy",
            "usage_data_sharing": "off" if sensitive else "inherit",
        },
        headers={"Idempotency-Key": "legacy-create"},
    )
    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert body["id"] == project_id
    assert body["allowed_services"] == "all_services"
    assert body["usage_data_sharing"] == ("off" if sensitive else "inherit")


def test_retry_rejects_a_replacement_bundle_without_touching_it(tmp_path) -> None:
    """The completed record names bundle A; the slug now holds a legacy
    bundle B. The retry refuses before opening B, so B is not upgraded."""
    root = tmp_path / "workspace"
    root.mkdir()
    project_id = _legacy_creation(
        root, state="complete", key="replaced", sensitive=False
    )
    (record_path,) = (root / ".project-creation-operations").glob("*.json")
    record = json.loads(record_path.read_text())
    record["storage_id"] = "frisket.bundle.v1:bundle-a-that-was-retired"
    record_path.write_text(json.dumps(record))
    bundle = root / f"{project_id}.frisket"
    manifest_before = (bundle / "manifest.json").read_bytes()

    client = TestClient(create_app(root))
    retried = client.post(
        "/api/projects",
        json={"name": "Legacy"},
        headers={"Idempotency-Key": "replaced"},
    )

    assert retried.status_code == 409, retried.text
    assert (bundle / "manifest.json").read_bytes() == manifest_before
    with sqlite3.connect(bundle / "project.db") as db:
        keys = {row[0] for row in db.execute("SELECT key FROM meta")}
    assert "usage_data_sharing" not in keys
    assert "sensitive" in keys
