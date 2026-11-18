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
    # Only the workspace report counts drafts that no project can list.
    assert workspace_response.json() == {
        **expected,
        "info": {
            **expected["info"],
            "import_drafts": {
                "unreadable": 0,
                "summary": "every import draft can be read",
            },
        },
    }
    assert project_response.json() == expected
    assert workspace.projects == ["project id%"]
    assert calls == [(None, None), ({"id": "project id%"}, "project id%")]


@pytest.mark.parametrize("route", _ROUTES.values())
def test_diagnose_keeps_probe_exception_details_in_operator_logs(
    monkeypatch: Any, caplog: Any, route: str
) -> None:
    from frisket.operability import diagnostics

    # Isolate optional/network probes; exercise the real aggregate and HTTP route.
    for name in (
        "python_version_report",
        "provider_report",
        "local_model_endpoints_report",
        "provider_key_validation_report",
        "replay_mode_report",
        "plugin_health_report",
        "engines_report",
        "retrieval_report",
        "spacy_model_report",
        "entities_report",
        "models_sidecar_report",
        "media_toolbelt_report",
        "ytdlp_update_report",
        "static_assets_report",
    ):
        monkeypatch.setattr(diagnostics, name, lambda *args, **kwargs: {"ok": True})

    detail = "internal-db.example:5432 operator_private_sql /srv/private/project.db"

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError(detail)

    monkeypatch.setattr(diagnostics, "store_roundtrip_report", fail)
    monkeypatch.setattr(diagnostics, "queue_health_report", fail)
    monkeypatch.setattr(diagnostics.shutil, "disk_usage", fail)
    app = FastAPI()
    register_diagnose_routes(app, workspace=_Workspace())  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.get(route)

    assert response.status_code == 200
    assert detail not in response.text
    report = response.json()
    assert report["healthy"] is False
    for failure in (
        report["core"]["project_store"],
        report["info"]["queue_health"],
        report["info"]["disk_free"],
    ):
        assert failure["ok"] is False
        assert failure["error"] == "OSError"
        assert failure["diag_id"].startswith("diag-")
        assert failure["diag_id"] in failure["summary"]
        assert failure["diag_id"] in caplog.text
    assert detail in caplog.text


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


def test_diagnose_real_export_adds_exactly_two_operations(
    real_openapi_document,
) -> None:
    for route, path in _OPENAPI_ROUTES.items():
        assert (
            real_openapi_document["paths"][path]["get"]["operationId"]
            == _OPERATION_IDS[route]
        )
