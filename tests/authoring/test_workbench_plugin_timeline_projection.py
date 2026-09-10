from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
)
from frisket.server.app import create_app


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ID = "demo.timeline"
PROJECTION_KIND = "demo.timeline.projection.timeline"
VIEW_ID = "demo.timeline.view.timeline"
HANDLER_KEY = "demo.timeline:timeline"
PLUGIN_ROOT = ROOT / "tests/fixtures/local_plugins/demo_timeline"
PLUGIN_MANIFEST = PLUGIN_ROOT / "plugin.json"
TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def _run_action(
    client: TestClient,
    project_id: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=spec,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed", result
    return result


def _project_with_timeline_rows(client: TestClient) -> tuple[str, int, dict[str, int]]:
    project_id = client.post("/api/projects", json={"name": "Timeline plugin"}).json()[
        "id"
    ]
    imported = client.post(
        f"/api/projects/{project_id}/import/csv",
        files={
            "file": (
                "cases.csv",
                "case_id,event_date,title\n"
                "CASE-2,2026-03-02,Second event\n"
                "CASE-1,2026-01-15,First event\n"
                "CASE-3,,No date skipped\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    data = _sheet_data(client, project_id, sheet_id)
    columns = {column["name"]: int(column["id"]) for column in data["columns"]}
    _run_action(
        client,
        project_id,
        {
            "action_id": "column.set_type",
            "scope": {"kind": "project"},
            "params": {"column_id": columns["event_date"], "type": "date"},
            "idempotency_key": "timeline-date-column@sha256:v1",
        },
    )
    return project_id, sheet_id, columns


def _sheet_data(client: TestClient, project_id: str, sheet_id: int) -> dict[str, Any]:
    response = client.get(
        f"/api/projects/{project_id}/sheets/{sheet_id}/data?offset=0&limit=20"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _activate_timeline_plugin(client: TestClient, project_id: str) -> None:
    installed = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(PLUGIN_ROOT)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    assert installed.status_code == 200, installed.text
    receipt_id = installed.json()["receiptId"]
    assert receipt_id
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
    backend = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{PLUGIN_ID}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    assert backend.status_code == 200, backend.text
    body = backend.json()
    assert body["registeredRuntimeBindings"]["projections"] == [PROJECTION_KIND]


def _projection_request(
    sheet_id: int,
    columns: dict[str, int],
    *,
    artifact_id: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "projectionKind": PROJECTION_KIND,
        "target": {
            "sheetId": sheet_id,
            "dateColumnId": columns["event_date"],
            "titleColumnId": columns["title"],
            "caseColumnId": columns["case_id"],
        },
        "params": {},
    }
    if artifact_id is not None:
        body["artifactId"] = artifact_id
    if mode is not None:
        body["mode"] = mode
    return body


def _post_projection(
    client: TestClient,
    project_id: str,
    endpoint: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{project_id}/projections/runtime/{endpoint}",
        json=body,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _host_modules_loaded_from(root: Path) -> list[str]:
    root = root.resolve()
    loaded: list[str] = []
    for module_name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        try:
            path = Path(filename).resolve()
        except OSError:
            continue
        if path.is_relative_to(root):
            loaded.append(module_name)
    return sorted(loaded)


def test_timeline_projection_builds_host_owned_artifact_without_mutating_source(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    project_id, sheet_id, columns = _project_with_timeline_rows(client)
    before = _sheet_data(client, project_id, sheet_id)
    before_row_count = len(before["rows"])
    before_column_ids = [column["id"] for column in before["columns"]]

    _activate_timeline_plugin(client, project_id)
    binding = default_registry().runtime_binding_specs("projections")[0]
    assert binding.kind == PROJECTION_KIND
    assert binding.handler_key == HANDLER_KEY
    assert binding.handler_api == "plugin_projection_subprocess"
    assert _host_modules_loaded_from(PLUGIN_ROOT) == []

    runtime_index = client.get(f"/api/projects/{project_id}/workbench/plugins").json()
    plugin = next(
        item for item in runtime_index["plugins"] if item["pluginId"] == PLUGIN_ID
    )
    assert plugin["contributionSummary"] == [
        {"kind": "workbench_view", "count": 1, "ids": [VIEW_ID]},
        {"kind": "projection", "count": 1, "ids": [PROJECTION_KIND]},
    ]
    descriptor = plugin["workbenchDescriptorManifests"][0]
    assert descriptor["id"] == VIEW_ID
    assert descriptor["projectionKind"] == PROJECTION_KIND
    assert descriptor["dataRequirements"] == [
        {"kind": "activeSheet"},
        {"kind": "sheetHasColumnType", "columnType": "date"},
    ]
    assert "componentKey" not in descriptor

    status = _post_projection(
        client,
        project_id,
        "status",
        _projection_request(sheet_id, columns),
    )
    assert status["status"] == "missing"
    assert status["freshness"]["state"] == "missing"
    assert status["outputs"]["artifactRefs"] == []
    first_generation = status["freshness"]["generation"]

    build = _post_projection(
        client,
        project_id,
        "build",
        _projection_request(sheet_id, columns, mode="refresh"),
    )
    assert build["status"] == "accepted"
    assert build["build"]["operation"] == "refresh"
    artifact_ref = build["outputs"]["artifactRefs"][0]
    assert artifact_ref["kind"] == "projection_artifact"
    assert artifact_ref["projectionKind"] == PROJECTION_KIND
    artifact_id = artifact_ref["artifactId"]
    assert artifact_id.startswith("projection://demo.timeline.projection.timeline/")
    assert not artifact_id.startswith(("/", "file:", "sqlite:", "sql:"))

    artifact = _post_projection(
        client,
        project_id,
        "artifact",
        _projection_request(sheet_id, columns, artifact_id=artifact_id),
    )
    assert artifact["schemaVersion"] == "frisket.timeline_projection_artifact.v1"
    assert artifact["projectionKind"] == PROJECTION_KIND
    assert artifact["generation"] == first_generation
    assert artifact["metrics"]["sourceRowCount"] == 3
    assert artifact["metrics"]["timelineItemCount"] == 2
    assert [
        (item["date"], item["title"], item["caseId"]) for item in artifact["items"]
    ] == [
        ("2026-01-15", "First event", "CASE-1"),
        ("2026-03-02", "Second event", "CASE-2"),
    ]
    assert _host_modules_loaded_from(PLUGIN_ROOT) == []

    after = _sheet_data(client, project_id, sheet_id)
    assert len(after["rows"]) == before_row_count
    assert [column["id"] for column in after["columns"]] == before_column_ids

    ready = _post_projection(
        client,
        project_id,
        "status",
        _projection_request(sheet_id, columns),
    )
    assert ready["status"] == "ready"
    assert ready["freshness"]["state"] == "fresh"
    assert ready["outputs"]["artifactRefs"][0]["artifactId"] == artifact_id

    first_row_id = int(after["rows"][0]["id"])
    _run_action(
        client,
        project_id,
        {
            "action_id": "cell.edit",
            "scope": {"kind": "project"},
            "params": {
                "edits": [
                    {
                        "row_id": first_row_id,
                        "column_id": columns["title"],
                        "value": "Second event edited",
                    }
                ]
            },
            "idempotency_key": "timeline-source-edit@sha256:v1",
        },
    )
    stale = _post_projection(
        client,
        project_id,
        "status",
        _projection_request(sheet_id, columns),
    )
    assert stale["status"] == "stale"
    assert stale["freshness"]["state"] == "stale"
    assert stale["freshness"]["generation"] != first_generation
    assert stale["outputs"]["artifactRefs"][0]["artifactId"] == artifact_id

    rebuild = _post_projection(
        client,
        project_id,
        "build",
        _projection_request(sheet_id, columns, mode="rebuild"),
    )
    assert rebuild["status"] == "accepted"
    assert rebuild["build"]["operation"] == "rebuild"
    rebuilt_artifact_id = rebuild["outputs"]["artifactRefs"][0]["artifactId"]
    assert rebuilt_artifact_id != artifact_id

    rebuilt_artifact = _post_projection(
        client,
        project_id,
        "artifact",
        _projection_request(sheet_id, columns, artifact_id=rebuilt_artifact_id),
    )
    assert [item["title"] for item in rebuilt_artifact["items"]] == [
        "First event",
        "Second event edited",
    ]
    assert _host_modules_loaded_from(PLUGIN_ROOT) == []
