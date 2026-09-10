"""Public HTTP contract pins for browser-owned runtime projection operations."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.server.routes.projections import register_runtime_projection_routes
from scripts.ci.export_web_openapi import export_real_compositions


_ROUTES = {
    "status": "/api/projects/project-1/projections/runtime/status",
    "build": "/api/projects/project-1/projections/runtime/build",
    "artifact": "/api/projects/project-1/projections/runtime/artifact",
}

_OPERATION_IDS = {
    "status": "tenant.runtime_projection_status.post",
    "build": "tenant.runtime_projection_build.post",
    "artifact": "tenant.runtime_projection_artifact.post",
}


class _ProjectionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def status(self, project_id: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(("status", project_id, payload))
        return {
            "schemaVersion": "frisket.runtime_projection_status.v1",
            "status": "ready",
            "freshness": {"state": "fresh"},
        }

    def build(self, project_id: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(("build", project_id, payload))
        return {
            "schemaVersion": "frisket.runtime_projection_build_plan.v1",
            "status": "accepted",
            "build": {"operation": payload["mode"], "idempotencyKey": "test-key"},
        }

    def artifact(self, project_id: str, **payload: Any) -> dict[str, Any]:
        self.calls.append(("artifact", project_id, payload))
        return {
            "schemaVersion": "frisket.timeline_projection_artifact.v1",
            "projectionKind": payload["projection_kind"],
            "artifactId": payload["artifact_id"],
            "pluginPayload": {"future": [1]},
        }


def _client() -> tuple[TestClient, _ProjectionService]:
    app = FastAPI()
    service = _ProjectionService()
    register_runtime_projection_routes(app, service=service)  # type: ignore[arg-type]
    return TestClient(app), service


@pytest.mark.parametrize(
    ("route", "body", "expected"),
    [
        (
            "status",
            {
                "projectionKind": "timeline",
                "target": {"future": [1]},
                "params": {"x": True},
            },
            {
                "projection_kind": "timeline",
                "target": {"future": [1]},
                "params": {"x": True},
            },
        ),
        (
            "build",
            {"projectionKind": "timeline"},
            {
                "projection_kind": "timeline",
                "target": {},
                "params": {},
                "mode": "refresh",
            },
        ),
        (
            "artifact",
            {"projectionKind": "timeline", "artifactId": "artifact-1"},
            {
                "projection_kind": "timeline",
                "artifact_id": "artifact-1",
                "target": {},
                "params": {},
            },
        ),
    ],
)
def test_runtime_projection_requests_are_closed_with_open_object_fields(
    route: str,
    body: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    client, service = _client()

    response = client.post(_ROUTES[route], json=body)

    assert response.status_code == 200, response.text
    assert service.calls == [(route, "project-1", expected)]


def test_runtime_artifact_request_uses_public_aliases_without_changing_response_bytes() -> (
    None
):
    client, service = _client()

    response = client.post(
        _ROUTES["artifact"],
        json={"projectionKind": "timeline", "artifactId": "artifact-1"},
    )

    assert response.status_code == 200, response.text
    assert service.calls == [
        (
            "artifact",
            "project-1",
            {
                "projection_kind": "timeline",
                "artifact_id": "artifact-1",
                "target": {},
                "params": {},
            },
        )
    ]
    assert response.content == (
        b'{"schemaVersion":"frisket.timeline_projection_artifact.v1",'
        b'"projectionKind":"timeline","artifactId":"artifact-1",'
        b'"pluginPayload":{"future":[1]}}'
    )


def test_runtime_projection_response_defaults_are_materialized_without_changing_bytes() -> (
    None
):
    client, _service = _client()

    status = client.post(_ROUTES["status"], json={"projectionKind": "timeline"})
    build = client.post(_ROUTES["build"], json={"projectionKind": "timeline"})

    assert status.content == (
        b'{"schemaVersion":"frisket.runtime_projection_status.v1",'
        b'"status":"ready","freshness":{"state":"fresh","generation":null,'
        b'"transient":false},"outputs":{"artifactRefs":[],"metrics":{}},'
        b'"warnings":[]}'
    )
    assert build.content == (
        b'{"schemaVersion":"frisket.runtime_projection_build_plan.v1",'
        b'"status":"accepted","build":{"operation":"refresh",'
        b'"idempotencyKey":"test-key"},"outputs":{"artifactRefs":[],'
        b'"metrics":{}},"warnings":[]}'
    )


@pytest.mark.parametrize(
    "body",
    [
        {"projection_kind": "timeline", "artifactId": "artifact-1"},
        {"projectionKind": "timeline", "artifact_id": "artifact-1"},
    ],
)
def test_runtime_artifact_request_rejects_snake_case_wire_keys(
    body: dict[str, Any],
) -> None:
    client, service = _client()

    response = client.post(_ROUTES["artifact"], json=body)

    assert response.status_code == 422
    assert service.calls == []


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize(
    "body",
    [
        {"projectionKind": ""},
        {"projectionKind": "timeline", "unknown": True},
        {"projectionKind": "timeline", "target": []},
        {"projectionKind": "timeline", "params": "not-an-object"},
    ],
)
def test_runtime_projection_requests_reject_unknown_and_non_object_values(
    route: str, body: dict[str, Any]
) -> None:
    client, service = _client()

    response = client.post(_ROUTES[route], json=body)

    assert response.status_code == 422
    assert service.calls == []


def test_runtime_projection_openapi_has_typed_successes_and_truthful_errors() -> None:
    client, _service = _client()
    document = client.app.openapi()

    status = document["paths"][_ROUTES["status"].replace("project-1", "{pid}")]["post"]
    build = document["paths"][_ROUTES["build"].replace("project-1", "{pid}")]["post"]
    artifact = document["paths"][_ROUTES["artifact"].replace("project-1", "{pid}")][
        "post"
    ]

    assert status["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RuntimeProjectionStatus"
    }
    assert build["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RuntimeProjectionBuildPlan"
    }
    assert artifact["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RuntimeProjectionArtifactResponse"
    }
    schemas = document["components"]["schemas"]
    assert schemas["RuntimeProjectionStatus"]["required"] == [
        "schemaVersion",
        "status",
        "freshness",
        "outputs",
        "warnings",
    ]
    assert schemas["RuntimeProjectionBuildPlan"]["required"] == [
        "schemaVersion",
        "status",
        "build",
        "outputs",
        "warnings",
    ]
    assert schemas["RuntimeProjectionFreshness"]["required"] == [
        "state",
        "generation",
        "transient",
    ]
    assert schemas["RuntimeProjectionOutputs"]["required"] == [
        "artifactRefs",
        "metrics",
    ]
    for operation in (status, build, artifact):
        assert set(operation["responses"]) >= {
            "200",
            "400",
            "401",
            "403",
            "404",
            "422",
            "500",
            "502",
        }


def test_runtime_projection_catalog_preserves_editor_session_policy_and_browser_membership() -> (
    None
):
    policies = {entry.id: entry for entry in BASE_ENDPOINT_CATALOG}

    for operation_id in _OPERATION_IDS.values():
        policy = policies[operation_id]
        assert (
            policy.route_owner,
            policy.method,
            policy.auth,
            policy.browser_client,
            policy.forwards_to_tenant,
            policy.project_role,
            policy.resolvers,
            policy.reserves_funding,
        ) == ("tenant", "POST", "session_or_pat", True, False, "editor", (), False)


def test_runtime_projection_export_adds_all_browser_projection_operations(
    tmp_path: Any,
) -> None:
    document = export_real_compositions(tmp_path / "state")
    operation_ids = {
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
    }

    assert set(_OPERATION_IDS.values()) <= operation_ids
