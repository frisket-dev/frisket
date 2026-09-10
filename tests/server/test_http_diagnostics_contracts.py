"""Public HTTP contract pins for the two Diagnose reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.contracts.http.diagnostics import DiagnosticsReport
from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.server.routes.diagnose import register_diagnose_routes
from scripts.ci.export_web_openapi import export_real_compositions


_ROUTES = {
    "workspace": "/api/diagnose",
    "project": "/api/projects/project%20id%25/diagnose",
}

_OPENAPI_ROUTES = {
    "workspace": "/api/diagnose",
    "project": "/api/projects/{pid}/diagnose",
}

_OPERATION_IDS = {
    "workspace": "tenant.diagnose.get",
    "project": "tenant.project_diagnose.get",
}


class _Workspace:
    queue = object()
    root = Path("/workspace")

    def __init__(self) -> None:
        self.projects: list[str] = []

    def diagnostic_router(self) -> object:
        return object()

    def get(self, project_id: str) -> dict[str, str]:
        self.projects.append(project_id)
        return {"id": project_id}


def _client(
    monkeypatch: Any,
) -> tuple[TestClient, _Workspace, list[tuple[object | None, str | None]]]:
    workspace = _Workspace()
    calls: list[tuple[object | None, str | None]] = []

    def fake_run_diagnostics(**kwargs: Any) -> dict[str, Any]:
        calls.append((kwargs["project"], kwargs["project_id"]))
        return {
            "healthy": True,
            "core": {"store": {"ok": True}},
            "info": {"future_probe": {"nested": [True, None, 3.5]}},
            "future_root": {"retained": [False, None]},
        }

    import frisket.operability.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "run_diagnostics", fake_run_diagnostics)
    app = FastAPI()
    register_diagnose_routes(app, workspace=workspace)  # type: ignore[arg-type]
    return TestClient(app), workspace, calls


def test_diagnose_routes_preserve_open_json_and_project_path_decoding(
    monkeypatch: Any,
) -> None:
    client, workspace, calls = _client(monkeypatch)

    workspace_response = client.get(_ROUTES["workspace"])
    project_response = client.get(_ROUTES["project"])

    expected = {
        "healthy": True,
        "core": {"store": {"ok": True}},
        "info": {"future_probe": {"nested": [True, None, 3.5]}},
        "future_root": {"retained": [False, None]},
    }
    assert workspace_response.status_code == 200, workspace_response.text
    assert project_response.status_code == 200, project_response.text
    assert workspace_response.json() == expected
    assert project_response.json() == expected
    assert workspace.projects == ["project id%"]
    assert calls == [(None, None), ({"id": "project id%"}, "project id%")]


def test_diagnose_openapi_has_typed_lossless_json_responses(monkeypatch: Any) -> None:
    client, _workspace, _calls = _client(monkeypatch)
    document = client.app.openapi()

    for path in _OPENAPI_ROUTES.values():
        operation = document["paths"][path]["get"]
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/DiagnosticsReport"}

    schema = document["components"]["schemas"]["DiagnosticsReport"]
    assert set(schema["required"]) == {"healthy", "core", "info"}
    assert schema["properties"]["healthy"] == {
        "type": "boolean",
        "title": "Healthy",
    }
    assert schema["properties"]["core"]["type"] == "object"
    assert schema["properties"]["info"]["type"] == "object"


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"healthy": "yes", "core": {}, "info": {}},
        {"healthy": True, "core": [], "info": {}},
        {"healthy": True, "core": {}, "info": []},
    ),
)
def test_diagnose_response_refuses_an_unrenderable_product_core(
    payload: object,
) -> None:
    with pytest.raises(ValidationError):
        DiagnosticsReport.model_validate(payload)


def test_diagnose_catalog_keeps_existing_auth_roles_and_effects() -> None:
    policies = {entry.id: entry for entry in BASE_ENDPOINT_CATALOG}

    assert (
        policies[_OPERATION_IDS["workspace"]].route_owner,
        policies[_OPERATION_IDS["workspace"]].method,
        policies[_OPERATION_IDS["workspace"]].auth,
        policies[_OPERATION_IDS["workspace"]].browser_client,
        policies[_OPERATION_IDS["workspace"]].forwards_to_tenant,
        policies[_OPERATION_IDS["workspace"]].project_role,
        policies[_OPERATION_IDS["workspace"]].resolvers,
        policies[_OPERATION_IDS["workspace"]].reserves_funding,
    ) == ("tenant", "GET", "session_or_pat", True, False, None, (), False)
    assert (
        policies[_OPERATION_IDS["project"]].route_owner,
        policies[_OPERATION_IDS["project"]].method,
        policies[_OPERATION_IDS["project"]].auth,
        policies[_OPERATION_IDS["project"]].browser_client,
        policies[_OPERATION_IDS["project"]].forwards_to_tenant,
        policies[_OPERATION_IDS["project"]].project_role,
        policies[_OPERATION_IDS["project"]].resolvers,
        policies[_OPERATION_IDS["project"]].reserves_funding,
    ) == ("tenant", "GET", "session_or_pat", True, False, "viewer", (), False)


def test_diagnose_real_export_adds_exactly_two_operations(tmp_path: Path) -> None:
    document = export_real_compositions(tmp_path / "state")

    for route, path in _OPENAPI_ROUTES.items():
        assert document["paths"][path]["get"]["operationId"] == _OPERATION_IDS[route]
