"""A configured plugin secret must never surface anywhere durable.

test_native_plugin_secrets.py already proves the reader itself: a missing
secret refuses before any publication, and a configured one reaches every
native host without mutating the process environment. What is NOT covered
there, and is what this file keeps from the retired subprocess entrypoint
suite, is the disclosure surface around it: the configuration route must not
echo the value back, and a completed run must not leave it in a written cell
or in the receipt.

Everything else that used to live here went away with the trusted-local
SUBPROCESS action transport. An installed Action now runs in process on the
same native hosts as a builtin, so "ran in a child" and "the child's wire
shape" have no subject, and ambient-environment isolation is explicitly NOT a
guarantee of this architecture: installed plugin code is trusted like a
builtin and may read the host environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app


TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"
ENV_PLUGIN_ID = "demo.env_probe"
ENV_ACTION_KIND = "demo.env_probe.prove_env"
SECRET_NAME = "DEMO_API_KEY"
SECRET_VALUE = "s3cret-value-must-not-leak"
FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "local_plugins"
ENV_PLUGIN_ROOT = FIXTURE_ROOT / "demo_env_probe"


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _project_with_people(client: TestClient) -> tuple[str, int, list[int]]:
    project_id = client.post(
        "/api/projects", json={"name": "Plugin subprocess"}
    ).json()["id"]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={"file": ("people.csv", "name\nAda\nGrace\n", "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    sheet_id = imported.json()["sheet_id"]
    data = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=10"
    )
    assert data.status_code == 200, data.text
    row_ids = [int(row["id"]) for row in data.json()["rows"]]
    assert len(row_ids) == 2
    return project_id, sheet_id, row_ids


def _install_activate_backend(
    client: TestClient,
    project_id: str,
    *,
    plugin_id: str,
    plugin_root: Path,
    action_kind: str,
) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(plugin_root)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]
    assert receipt_id
    activated = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": [TRUSTED_LOCAL_BACKEND_CAPABILITY],
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert activated.status_code == 200, activated.text
    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    assert backend.json()["registeredRuntimeBindings"]["actions"] == [action_kind]


def _run_plugin_action(
    client: TestClient,
    project_id: str,
    *,
    action_kind: str,
    sheet_id: int,
    row_ids: list[int] | None = None,
    key: str,
    expected_status: int = 200,
) -> dict[str, Any]:
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            "action_id": action_kind,
            "scope": scope,
            "params": {"name": "name"},
            "idempotency_key": key,
        },
    )
    assert response.status_code == expected_status, response.text
    return response.json()


def _configure_secret(client: TestClient, project_id: str) -> Any:
    return client.post(
        f"/api/projects/{project_id}/workbench/plugins/{ENV_PLUGIN_ID}/env",
        json={"name": SECRET_NAME, "value": SECRET_VALUE},
    )


def _sheet_data(client: TestClient, project_id: str, sheet_id: int) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=20"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _column_id(data: dict[str, Any], name: str) -> int:
    column = next(column for column in data["columns"] if column["name"] == name)
    return int(column["id"])


def test_configured_secret_is_not_echoed_by_the_configuration_route(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, _sheet_id, _row_ids = _project_with_people(client)
    _install_activate_backend(
        client,
        project_id,
        plugin_id=ENV_PLUGIN_ID,
        plugin_root=ENV_PLUGIN_ROOT,
        action_kind=ENV_ACTION_KIND,
    )

    saved = _configure_secret(client, project_id)
    assert saved.status_code == 200, saved.text
    assert SECRET_VALUE not in saved.text

    listed = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert listed.status_code == 200, listed.text
    assert SECRET_VALUE not in listed.text


def test_configured_secret_never_reaches_a_cell_or_a_receipt(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, _row_ids = _project_with_people(client)
    _install_activate_backend(
        client,
        project_id,
        plugin_id=ENV_PLUGIN_ID,
        plugin_root=ENV_PLUGIN_ROOT,
        action_kind=ENV_ACTION_KIND,
    )
    assert _configure_secret(client, project_id).status_code == 200

    result = _run_plugin_action(
        client,
        project_id,
        action_kind=ENV_ACTION_KIND,
        sheet_id=sheet_id,
        key="env-probe-configured@sha256:v1",
    )
    assert result["status"] == "completed"
    assert SECRET_VALUE not in json.dumps(result)

    # The handler reached `secrets.require`, so the value WAS provisioned.
    data = _sheet_data(client, project_id, sheet_id)
    column_id = _column_id(data, "env_probe")
    assert [row["cells"][str(column_id)] for row in data["rows"]] == [
        "configured",
        "configured",
    ]
    assert SECRET_VALUE not in json.dumps(data)

    receipt = client.get(
        f"/api/projects/{project_id}/actions/v1/receipts/{result['receipt_id']}"
    )
    assert receipt.status_code == 200, receipt.text
    assert SECRET_VALUE not in receipt.text
