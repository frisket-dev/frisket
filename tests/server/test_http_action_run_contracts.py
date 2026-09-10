"""HTTP/OpenAPI coverage for the v1 action-launch transport."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.routes.action_runs import register_action_run_routes
from frisket.server.services.action_runs import ActionRunResponse


class _ActionRunService:
    def __init__(self) -> None:
        self.body: dict[str, Any] | None = None

    def run_action(
        self,
        project_id: str,
        body: dict[str, Any],
        *,
        request_context: Any,
    ) -> ActionRunResponse:
        assert request_context is not None
        self.body = body
        if body.get("kind") == "needs-confirmation":
            return _action_response(402, "needs_confirmation")
        return _action_response(200, "completed")


def _action_response(status_code: int, status: str) -> ActionRunResponse:
    return ActionRunResponse(
        status_code=status_code,
        payload={
            "schema_version": "frisket.action_result.v1",
            "action": {
                "kind": "map.classify",
                "action_id": "action-1",
            },
            "status": status,
            "project_id": "project-1",
            "run_id": None,
            "receipt_id": None,
            "errors": [],
        },
    )


def _client() -> tuple[TestClient, _ActionRunService]:
    app = FastAPI()
    service = _ActionRunService()
    register_action_run_routes(app, service=service)  # type: ignore[arg-type]
    return TestClient(app), service


def test_action_launch_preserves_tolerant_recursive_json_and_action_result_statuses() -> (
    None
):
    client, service = _client()
    body = {
        "kind": "map.classify",
        "params": {"labels": ["yes", "no"], "nested": {"kept": True}},
        "producer_extension": {"open": [1, None]},
    }

    completed = client.post("/api/projects/project-1/actions/v1/run", json=body)
    assert completed.status_code == 200, completed.text
    assert completed.json()["schema_version"] == "frisket.action_result.v1"
    assert completed.json()["status"] == "completed"
    assert service.body == body

    confirmation = client.post(
        "/api/projects/project-1/actions/v1/run",
        json={"kind": "needs-confirmation", "params": {}},
    )
    assert confirmation.status_code == 402, confirmation.text
    assert confirmation.json()["schema_version"] == "frisket.action_result.v1"
    assert confirmation.json()["status"] == "needs_confirmation"


def test_action_launch_openapi_projects_action_results_and_generic_transport_errors() -> (
    None
):
    client, _service = _client()
    operation = client.get("/openapi.json").json()["paths"][
        "/api/projects/{pid}/actions/v1/run"
    ]["post"]

    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    if "$ref" in request_schema:
        request_schema = client.get("/openapi.json").json()["components"]["schemas"][
            request_schema["$ref"].rsplit("/", 1)[-1]
        ]
    # The open object is the behavior contract, not a component-name/source
    # shape requirement.
    assert request_schema["type"] == "object"
    assert "additionalProperties" in request_schema
    for status in ("200", "400", "402", "409", "500"):
        assert operation["responses"][status]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/ActionResult"}
    for status in ("401", "403", "404", "422"):
        assert operation["responses"][status]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/HttpError"}
