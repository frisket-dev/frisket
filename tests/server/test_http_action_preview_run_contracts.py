"""HTTP/OpenAPI coverage for the action-preview job lifecycle."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.routes.action_preview_support import (
    register_action_preview_run_routes,
)
from frisket.server.services.action_preview_runs import ActionPreviewRunResponse


class _PreviewRunService:
    def __init__(self) -> None:
        self.start_body: dict[str, Any] | None = None

    def start_preview(
        self,
        project_id: str,
        body: dict[str, Any],
        *,
        request_context: Any,
        on_finished: Any = None,
    ) -> ActionPreviewRunResponse:
        self.start_body = body
        if body.get("kind") == "needs-confirmation":
            return _error(402, "cost_gate")
        return ActionPreviewRunResponse(
            status_code=202,
            payload={
                "schema_version": "frisket.action_preview.v1",
                "preview_id": "preview-1",
                "total": 3,
            },
        )

    def get_preview(self, project_id: str, preview_id: str) -> ActionPreviewRunResponse:
        if preview_id == "missing":
            return _error(404, "preview_not_found")
        if preview_id == "table":
            return ActionPreviewRunResponse(
                status_code=200,
                payload={
                    "schema_version": "frisket.action_preview.v1",
                    "preview_id": preview_id,
                    "status": "done",
                    "progress": {"done": 1, "total": None},
                    "result": {
                        "kind": "table",
                        "columns": [],
                        "rows": [{"title": {"value": "A sample"}}],
                        "sampled": 1,
                        "total": None,
                        "warnings": ["Page image unavailable; text retained."],
                    },
                },
            )
        if preview_id == "done":
            return ActionPreviewRunResponse(
                status_code=200,
                payload={
                    "schema_version": "frisket.action_preview.v1",
                    "preview_id": preview_id,
                    "status": "done",
                    "progress": {"done": 1, "total": 1},
                    "result": {
                        "kind": "row_overlay",
                        "sheet_id": 7,
                        "columns": [
                            {
                                "name": "answer",
                                "column_type": "text",
                                "format": None,
                                "hidden": False,
                                "overwrites_column_id": None,
                            }
                        ],
                        "rows": {"9": {"answer": {"value": "hello"}}},
                        "row_ids": [9],
                        "sampled": 1,
                        "total": 1,
                    },
                },
            )
        return ActionPreviewRunResponse(
            status_code=200,
            payload={
                "schema_version": "frisket.action_preview.v1",
                "preview_id": preview_id,
                "status": "running",
                "progress": {"done": 0, "total": 3},
            },
        )

    def cancel_preview(
        self, project_id: str, preview_id: str
    ) -> ActionPreviewRunResponse:
        return ActionPreviewRunResponse(status_code=204, payload={})


def _error(status_code: int, code: str) -> ActionPreviewRunResponse:
    return ActionPreviewRunResponse(
        status_code=status_code,
        payload={
            "schema_version": "frisket.action_preview.v1",
            "error": {
                "schema_version": "frisket.action_error.v1",
                "code": code,
                "message": f"{code} refusal",
                "action_kind": "map.classify",
                "field": "kind",
                "details": {"source": "preview"},
                "needs_confirmation": status_code == 402,
            },
        },
    )


def _client() -> tuple[TestClient, _PreviewRunService]:
    app = FastAPI()
    service = _PreviewRunService()
    register_action_preview_run_routes(app, service=service)  # type: ignore[arg-type]
    return TestClient(app), service


def test_action_preview_lifecycle_preserves_transport_shape_and_omits_unset_poll_fields() -> (
    None
):
    client, service = _client()

    started = client.post(
        "/api/projects/project-1/actions/v1/preview",
        json={
            "kind": "map.classify",
            "params": {"model": "x", "labels": ["yes", "no"]},
            "transport_extension": {"safe": True},
        },
    )
    assert started.status_code == 202, started.text
    assert started.json() == {
        "schema_version": "frisket.action_preview.v1",
        "preview_id": "preview-1",
        "total": 3,
    }
    assert service.start_body == {
        "kind": "map.classify",
        "params": {"model": "x", "labels": ["yes", "no"]},
        "transport_extension": {"safe": True},
    }

    running = client.get("/api/projects/project-1/actions/v1/preview/preview-1")
    assert running.status_code == 200, running.text
    assert running.json() == {
        "schema_version": "frisket.action_preview.v1",
        "preview_id": "preview-1",
        "status": "running",
        "progress": {"done": 0, "total": 3},
    }

    done = client.get("/api/projects/project-1/actions/v1/preview/done")
    assert done.status_code == 200, done.text
    assert done.json()["result"]["rows"] == {"9": {"answer": {"value": "hello"}}}
    assert "error" not in done.json()

    cancelled = client.delete("/api/projects/project-1/actions/v1/preview/preview-1")
    assert cancelled.status_code == 204
    assert cancelled.content == b""


def test_action_preview_lifecycle_preserves_domain_error_statuses_and_openapi() -> None:
    client, _service = _client()

    confirmation = client.post(
        "/api/projects/project-1/actions/v1/preview",
        json={"kind": "needs-confirmation"},
    )
    assert confirmation.status_code == 402
    assert confirmation.json()["error"]["code"] == "cost_gate"

    missing = client.get("/api/projects/project-1/actions/v1/preview/missing")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "preview_not_found"

    document = client.app.openapi()
    start = document["paths"]["/api/projects/{pid}/actions/v1/preview"]["post"]
    poll = document["paths"]["/api/projects/{pid}/actions/v1/preview/{preview_id}"][
        "get"
    ]
    cancel = document["paths"]["/api/projects/{pid}/actions/v1/preview/{preview_id}"][
        "delete"
    ]
    assert set(start["responses"]) >= {"202", "400", "402", "404", "422"}
    assert set(poll["responses"]) >= {"200", "400", "402", "404", "422"}
    assert set(cancel["responses"]) >= {"204", "404", "422"}
    schema = start["requestBody"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ActionPreviewRunRequest"}
    request_schema = document["components"]["schemas"]["ActionPreviewRunRequest"]
    assert request_schema["type"] == "object"
    assert request_schema["additionalProperties"]


def test_table_preview_has_no_source_identity_or_invented_total() -> None:
    client, _ = _client()
    response = client.get("/api/projects/project-1/actions/v1/preview/table")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["progress"] == {"done": 1, "total": None}
    assert payload["result"] == {
        "kind": "table",
        "columns": [],
        "rows": [{"title": {"value": "A sample"}}],
        "sampled": 1,
        "total": None,
        "warnings": ["Page image unavailable; text retained."],
    }
