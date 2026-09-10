from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.contracts.action import ActionResult
from frisket.server.app import create_app
from frisket.server.notifications.service import LOCAL_ACTOR_ID
from frisket.server.routes.action_runs import register_action_run_routes
from frisket.server.services.action_runs import ActionRunResponse


class _CapturingActionRunService:
    def __init__(self) -> None:
        self.execution_contexts: list[Any] = []

    def run_action(
        self,
        _project_id: str,
        _body: dict[str, Any],
        *,
        request_context: Any,
        **_kwargs: Any,
    ) -> ActionRunResponse:
        self.execution_contexts.append(
            getattr(request_context.state, "execution_composition_context", None)
        )
        return ActionRunResponse(
            status_code=200,
            payload=ActionResult(
                action={"kind": "map.prompt", "action_id": "action"},
                status="completed",
                project_id=_project_id,
            ).model_dump(mode="json"),
        )


def test_direct_base_forged_actor_header_cannot_rescope_project_idempotency(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    request_headers = {"idempotency-key": "stable-project-create"}

    first = client.post(
        "/api/projects",
        json={"name": "Stable project"},
        headers={**request_headers, "x-frisket-hosted-actor-id": "user:41"},
    )
    replay = client.post(
        "/api/projects",
        json={"name": "Stable project"},
        headers={**request_headers, "x-frisket-hosted-actor-id": "user:42"},
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert len(client.get("/api/projects").json()) == 1


def test_direct_base_forged_actor_header_cannot_choose_notification_owner(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Alerts"}).json()["id"]
    emitted = client.post(
        f"/api/projects/{pid}/notifications/emit",
        json={
            "source_kind": "plugin.ingress",
            "source_ref": {"plugin_id": "ingress"},
            "source_event_ids": [1],
            "dedupe_key": "privileged-ingress-state",
            "title": "Ingress alert",
            "summary": "A direct Base request cannot forge its owner.",
            "severity": "warning",
            "deep_link": {"kind": "plugin_source", "plugin_id": "ingress"},
            "payload": {},
        },
    )
    assert emitted.status_code == 200, emitted.text

    marked = client.post(
        f"/api/projects/{pid}/notifications/{emitted.json()['notification_id']}/read",
        headers={
            "x-frisket-hosted-actor-id": "user:999",
            "x-frisket-hosted-actor-auth": "pat",
        },
    )

    assert marked.status_code == 200, marked.text
    assert marked.json()["actor_id"] == LOCAL_ACTOR_ID


def test_direct_base_forged_edition_header_cannot_supply_billing_context() -> None:
    service = _CapturingActionRunService()
    app = FastAPI()
    register_action_run_routes(app, service=service)  # type: ignore[arg-type]
    client = TestClient(app)

    response = client.post(
        "/api/projects/project/actions/v1/run",
        json={"kind": "map.prompt"},
        headers={
            "x-frisket-edition-run-context": ('{"funding_account_id":1,"user_id":999}')
        },
    )

    assert response.status_code == 200, response.text
    assert service.execution_contexts == [None]
