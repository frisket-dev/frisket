from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.contracts.action import CURRENT_ACTION_AUTHORING_CONTRACT_VERSION
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
PLUGIN_ID = "demo.receipt_stamp"
ACTION_KIND = "demo.receipt_stamp.stamp"
PLUGIN_ROOT = (
    Path(__file__).parent.parent / "fixtures" / "local_plugins" / "demo_receipt_stamp"
)


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _catalog_kinds(client: TestClient, path: str) -> set[str]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return {str(entry["kind"]) for entry in response.json()["actions"]}


def _install_and_enable(client: TestClient, project_id: str) -> str:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = str(installed.json()["receiptId"])
    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text
    return receipt_id


def _activate_backend(client: TestClient, project_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_project_action_catalog_merges_enabled_plugin_actions_only(
    tmp_path: Path,
) -> None:
    """An installed Action reaches exactly the project that admitted it.

    The published entry is the Action's own canonical catalog entry — the same
    projection a builtin publishes — so there is no plugin-shaped hint dialect
    (`form_params`, `default_outputs`) beside it: every parameter is described
    by `input_schema`, and the outputs by `ui_hints.logical_outputs`.
    """
    client = TestClient(create_app(tmp_path / "workspace"))
    project_a = client.post(
        "/api/projects", json={"name": "Plugin action catalog A"}
    ).json()["id"]
    project_b = client.post(
        "/api/projects", json={"name": "Plugin action catalog B"}
    ).json()["id"]

    assert ACTION_KIND not in _catalog_kinds(client, "/api/actions/v1/catalog")
    assert ACTION_KIND not in _catalog_kinds(
        client, f"/api/projects/{project_a}/actions/v1/catalog"
    )

    _install_and_enable(client, project_a)
    assert ACTION_KIND not in _catalog_kinds(
        client, f"/api/projects/{project_a}/actions/v1/catalog"
    )

    activation = _activate_backend(client, project_a)
    assert activation["registeredRuntimeBindings"]["actions"] == [ACTION_KIND]

    response = client.get(f"/api/projects/{project_a}/actions/v1/catalog")
    assert response.status_code == 200, response.text
    catalog = response.json()
    entry = next(item for item in catalog["actions"] if item["kind"] == ACTION_KIND)
    assert entry["authoring_contract_version"] == (
        CURRENT_ACTION_AUTHORING_CONTRACT_VERSION
    )
    assert entry["title"] == "Stamp receipt proof"
    assert entry["receipt_policy"] == "writes_receipt"
    # Installation, not the catalog entry, is where the trusted-local grant is
    # spent: the Action itself asks only for what any row-writing builtin asks.
    assert entry["required_capabilities"] == ["project:write"]
    assert entry["ui_hints"]["form"] == "generated"
    assert entry["ui_hints"]["category"] == "text"
    assert list(entry["input_schema"]["properties"]) == ["name"]
    assert entry["ui_hints"]["semantic_controls"] == {"name": "column"}
    assert entry["ui_hints"]["source_requirements"] == [
        {
            "id": "name",
            "param": "name",
            "label": "Name",
            "accepted_column_types": ["text"],
            "mode": "column",
            "min": 1,
        }
    ]
    assert entry["ui_hints"]["logical_outputs"] == [
        {"key": "receipt_stamp", "column_type": "text"}
    ]
    assert "form_params" not in entry["ui_hints"]
    assert "default_outputs" not in entry["ui_hints"]

    assert "plugin.load" in {item["kind"] for item in catalog["actions"]}
    assert ACTION_KIND not in _catalog_kinds(
        client, f"/api/projects/{project_b}/actions/v1/catalog"
    )
    assert ACTION_KIND not in _catalog_kinds(client, "/api/actions/v1/catalog")
