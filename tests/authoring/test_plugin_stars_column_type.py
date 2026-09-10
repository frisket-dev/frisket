from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.authoring.column_types import get_column_type, validate_value
from frisket.ai.llm import ModelRouter
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from http_test_helpers import (
    post_cell_edit_as_v1_action,
    post_column_set_type_as_v1_action,
)


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ID = "demo.stars"
PLUGIN_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_MANIFEST = ROOT / "tests/fixtures/local_plugins/demo_stars/plugin.json"


def _client(tmp_path: Path) -> TestClient:
    router = ModelRouter(cache=None, cache_mode="off")
    return TestClient(create_app(tmp_path / "ws", router=router))


def _load_enable_and_activate_backend(client: TestClient, project_id: str) -> str:
    # install-local writes the workspace catalog identity that /activate
    # reads (a raw plugin.load action no longer populates it).
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_MANIFEST)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]
    assert receipt_id

    enabled = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [PLUGIN_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert enabled.status_code == 200, enabled.text

    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
        json={"trustAcknowledged": True, "arbitraryPackageLoadAllowed": False},
    )
    assert backend.status_code == 200, backend.text
    assert backend.json()["registeredBackendContributions"]["columnTypes"] == ["stars"]
    return receipt_id


def _import_ratings(client: TestClient, project_id: str) -> tuple[int, dict[str, Any]]:
    response = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("ratings.csv", "title,rating\nA,4.5\nB,3\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    sheet_id = int(response.json()["sheet_id"])
    data = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data").json()
    rating = next(column for column in data["columns"] if column["name"] == "rating")
    return sheet_id, rating


def test_demo_stars_plugin_registers_column_type_metadata_and_validator(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = _client(tmp_path)
        project_id = client.post("/api/projects", json={"name": "Stars"}).json()["id"]

        before = client.get("/api/column-types")
        assert before.status_code == 200
        assert "stars" not in {item["name"] for item in before.json()}

        _load_enable_and_activate_backend(client, project_id)

        spec = get_column_type("stars")
        assert spec is not None
        assert spec.core is False
        assert spec.plugin == PLUGIN_ID
        assert spec.presentation == {
            "renderer": "stars",
            "base": "number",
            "owner": "plugin",
            "plugin": PLUGIN_ID,
        }
        assert validate_value("stars", 0)
        assert validate_value("stars", 4.5)
        assert validate_value("stars", 5)
        assert not validate_value("stars", -0.1)
        assert not validate_value("stars", 5.1)
        assert not validate_value("stars", "*****")

        global_registry = client.get("/api/column-types")
        assert global_registry.status_code == 200
        assert "stars" not in {item["name"] for item in global_registry.json()}

        registry = client.get(f"/api/projects/{project_id}/column-types")
        assert registry.status_code == 200
        stars = next(item for item in registry.json() if item["name"] == "stars")
        assert stars["core"] is False
        assert stars["plugin"] == PLUGIN_ID
        assert stars["has_validator"] is True
        assert stars["presentation"] == spec.presentation

        project_b = client.post(
            "/api/projects", json={"name": "Stars disabled"}
        ).json()["id"]
        disabled_registry = client.get(f"/api/projects/{project_b}/column-types")
        assert disabled_registry.status_code == 200
        assert "stars" not in {item["name"] for item in disabled_registry.json()}
        _sheet_b, rating_b = _import_ratings(client, project_b)
        rejected = post_column_set_type_as_v1_action(
            client, project_b, rating_b["id"], "stars"
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["errors"][0]["code"] == "invalid_column_type"

        disabled = client.post(
            f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/disable"
        )
        assert disabled.status_code == 200, disabled.text
        after_disable = client.get(f"/api/projects/{project_id}/column-types")
        assert "stars" not in {item["name"] for item in after_disable.json()}
    finally:
        _reset_default_registry_for_tests()


def test_stars_column_type_set_type_and_cell_edit_validate_numeric_range(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        client = _client(tmp_path)
        project_id = client.post("/api/projects", json={"name": "Stars values"}).json()[
            "id"
        ]
        _load_enable_and_activate_backend(client, project_id)
        sheet_id, rating = _import_ratings(client, project_id)

        changed = post_column_set_type_as_v1_action(
            client, project_id, rating["id"], "stars"
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["status"] == "completed"

        data = client.get(
            f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=2"
        ).json()
        assert (
            next(column for column in data["columns"] if column["name"] == "rating")[
                "type"
            ]
            == "stars"
        )
        row_id = int(data["rows"][0]["id"])

        valid_edit = post_cell_edit_as_v1_action(
            client,
            project_id,
            [
                {
                    "row_id": row_id,
                    "column_id": rating["id"],
                    "value": 2.5,
                }
            ],
        )
        assert valid_edit.status_code == 200, valid_edit.text
        assert valid_edit.json()["status"] == "completed"

        invalid_edit = post_cell_edit_as_v1_action(
            client,
            project_id,
            [
                {
                    "row_id": row_id,
                    "column_id": rating["id"],
                    "value": 6,
                }
            ],
            idempotency_key="plugin-stars-invalid-edit@sha256:v1",
        )
        assert invalid_edit.status_code == 400, invalid_edit.text
        assert (
            invalid_edit.json()["errors"][0]["code"] == "column_value_validation_failed"
        )

        project = client.app.state.workspace.get(project_id)
        assert project.get_values(sheet_id, rating["id"])[row_id] == 2.5
    finally:
        _reset_default_registry_for_tests()
