from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client
from frisket.engine.jobs import (
    HandlerRegistry,
    NOTIFICATION_DELIVER_KIND,
    Worker,
    default_registry,
    enqueue_notification_delivery,
    enqueue_notification_emit_result,
    open_queue,
    reconcile_notification_delivery_requests,
    register_notification_handlers,
)
from frisket.server.notifications.delivery import (
    DeliveryProviderResult,
    NotificationDeliveryRuntime,
    NotificationRenderedMessage,
)
from frisket.server.notifications.secrets import StaticNotificationSecretResolver
from frisket.server.notifications.service import emit_notification_candidate
from frisket.engine.store import Project


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


class SequencedProvider:
    def __init__(self, outcomes: list[DeliveryProviderResult]) -> None:
        self.outcomes = list(outcomes)
        self.messages: list[NotificationRenderedMessage] = []

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        self.messages.append(message)
        if not self.outcomes:
            return DeliveryProviderResult(status="sent", provider_ref="fake:extra")
        return self.outcomes.pop(0)


@pytest.mark.parametrize(
    "malformed",
    [
        {},
        {"planned_delivery_requests": [{}]},
    ],
)
def test_emit_result_bridge_rejects_malformed_results(
    tmp_path: Path,
    malformed: dict[str, Any],
) -> None:
    queue = open_queue(workspace=tmp_path / "queue")

    with pytest.raises(KeyError):
        enqueue_notification_emit_result(
            queue,
            workspace_root=tmp_path,
            project_id="malformed",
            result=malformed,  # type: ignore[arg-type]
        )


def _seed_watch_events(client: TestClient) -> tuple[str, dict[str, Any], list[dict]]:
    pid = client.post("/api/projects", json={"name": "Notification worker"}).json()[
        "id"
    ]
    import_response = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert import_response.status_code == 200, import_response.text
    watch_response = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Budget watch",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "budget", "limit": 10},
        },
    )
    assert watch_response.status_code == 200, watch_response.text
    watch = watch_response.json()
    run_response = client.post(f"/api/projects/{pid}/watches/{watch['id']}/run")
    assert run_response.status_code == 200, run_response.text
    run = run_response.json()["run"]
    events_response = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs/{run['id']}/events",
        params={"limit": 10},
    )
    assert events_response.status_code == 200, events_response.text
    return pid, {"watch": watch, "run": run}, events_response.json()["events"]


def _create_email_channel(
    client: TestClient,
    pid: str,
    *,
    secret: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "email",
        "name": "Ops email",
        "to": "alerts@example.com",
    }
    if secret:
        body["secret_ref"] = "env:RESEND_API_KEY"
    response = client.post(f"/api/projects/{pid}/notification-channels", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _create_route(
    client: TestClient,
    pid: str,
    *,
    channel_id: int,
    watch_id: int,
) -> dict[str, Any]:
    response = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Budget email",
            "channel_id": channel_id,
            "source_kind": "watch",
            "source_ref_match": {"watch_id": watch_id},
            "delivery_mode": "immediate",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _emit_watch(
    client: TestClient,
    pid: str,
    refs: dict[str, Any],
    events: list[dict],
) -> dict[str, Any]:
    event_ids = [event["id"] for event in events]
    response = client.post(
        f"/api/projects/{pid}/notifications/emit",
        json={
            "source_kind": "watch",
            "source_ref": {
                "watch_id": refs["watch"]["id"],
                "run_id": refs["run"]["id"],
            },
            "source_event_ids": event_ids,
            "dedupe_key": "worker-test",
            "title": "Budget watch found rows",
            "summary": "Budget rows matched.",
            "severity": "warning",
            "deep_link": {"kind": "watch_run", "watch_id": refs["watch"]["id"]},
            "payload": {},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _install_provider(client: TestClient, provider: SequencedProvider) -> None:
    ws = client.app.state.workspace
    replacement_registry = HandlerRegistry()
    register_notification_handlers(
        replacement_registry,
        workspace_root=ws.root,
        delivery_runtime=NotificationDeliveryRuntime(
            provider_factory=lambda _channel: provider,
            secret_resolver=StaticNotificationSecretResolver(
                {
                    "env:RESEND_API_KEY": "resend-secret",
                    "env:SLACK_WEBHOOK_URL": "slack-secret",
                }
            ),
        ),
        queue=ws.queue,
    )
    replacement = replacement_registry.get(NOTIFICATION_DELIVER_KIND)
    assert replacement is not None
    ws.registry.register(NOTIFICATION_DELIVER_KIND, replacement)


def _drain_one(client: TestClient) -> bool:
    ws = client.app.state.workspace
    worker = Worker(
        ws.queue,
        ws.registry,
        worker_id="notification-worker-test",
        retry_base_seconds=0,
        retry_cap_seconds=0,
    )
    return worker.run_once()


def test_notification_delivery_worker_sends_request_and_records_attempt(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, refs, events = _seed_watch_events(client)
    channel = _create_email_channel(client, pid)
    _create_route(client, pid, channel_id=channel["id"], watch_id=refs["watch"]["id"])
    provider = SequencedProvider(
        [
            DeliveryProviderResult(
                status="sent",
                provider_ref="fake-email:1",
                response_meta={"accepted": True},
            )
        ]
    )
    _install_provider(client, provider)

    emitted = _emit_watch(client, pid, refs, events)
    request_id = emitted["planned_delivery_requests"][0]["id"]
    project = client.app.state.workspace.get(pid)
    request = project.public_notification_delivery_request(request_id)
    assert request["status"] == "queued"
    assert request["job_id"] is not None
    assert client.app.state.workspace.queue.get(request["job_id"]).kind == (
        NOTIFICATION_DELIVER_KIND
    )

    assert _drain_one(client) is True

    sent = project.public_notification_delivery_request(request_id)
    assert sent["status"] == "sent"
    assert sent["provider_ref"] == "fake-email:1"
    attempts = project.db.execute(
        "SELECT * FROM notification_delivery_attempts WHERE delivery_request_id=?",
        (request_id,),
    ).fetchall()
    assert len(attempts) == 1
    assert attempts[0]["status"] == "sent"
    assert attempts[0]["provider_ref"] == "fake-email:1"
    assert provider.messages[0].title == "Budget watch found rows"


def test_missing_secret_skips_without_provider_call(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, refs, events = _seed_watch_events(client)
    channel = _create_email_channel(client, pid, secret=False)
    _create_route(client, pid, channel_id=channel["id"], watch_id=refs["watch"]["id"])
    provider = SequencedProvider(
        [DeliveryProviderResult(status="sent", provider_ref="should-not-send")]
    )
    _install_provider(client, provider)

    emitted = _emit_watch(client, pid, refs, events)
    request_id = emitted["planned_delivery_requests"][0]["id"]
    assert _drain_one(client) is True

    project = client.app.state.workspace.get(pid)
    skipped = project.public_notification_delivery_request(request_id)
    assert skipped["status"] == "skipped"
    assert skipped["last_error"] == "email channel missing secret reference"
    assert provider.messages == []


def test_transient_provider_failure_requeues_and_reconciles_request(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    pid, refs, events = _seed_watch_events(client)
    channel = _create_email_channel(client, pid)
    _create_route(client, pid, channel_id=channel["id"], watch_id=refs["watch"]["id"])
    provider = SequencedProvider(
        [
            DeliveryProviderResult(
                status="failed",
                error="temporary provider outage",
                transient=True,
            ),
            DeliveryProviderResult(status="sent", provider_ref="fake-email:retry"),
        ]
    )
    _install_provider(client, provider)
    emitted = _emit_watch(client, pid, refs, events)
    request_id = emitted["planned_delivery_requests"][0]["id"]
    ws = client.app.state.workspace
    project = ws.get(pid)

    assert _drain_one(client) is True
    first = project.public_notification_delivery_request(request_id)
    assert first["status"] == "processing"
    assert ws.queue.get(first["job_id"]).status == "queued"

    assert (
        reconcile_notification_delivery_requests(
            ws.queue,
            workspace_root=ws.root,
            project_id=pid,
        )
        == 1
    )
    assert (
        project.public_notification_delivery_request(request_id)["status"] == "queued"
    )

    assert _drain_one(client) is True
    sent = project.public_notification_delivery_request(request_id)
    assert sent["status"] == "sent"
    assert sent["provider_ref"] == "fake-email:retry"
    attempts = project.db.execute(
        "SELECT status FROM notification_delivery_attempts "
        "WHERE delivery_request_id=? ORDER BY attempt",
        (request_id,),
    ).fetchall()
    assert [row["status"] for row in attempts] == ["failed", "sent"]


def test_retry_exhaustion_marks_one_terminal_failed_request(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pid, refs, events = _seed_watch_events(client)
    channel = _create_email_channel(client, pid)
    _create_route(client, pid, channel_id=channel["id"], watch_id=refs["watch"]["id"])
    provider = SequencedProvider(
        [
            DeliveryProviderResult(status="failed", error="still down", transient=True),
            DeliveryProviderResult(status="failed", error="still down", transient=True),
        ]
    )
    _install_provider(client, provider)
    project = client.app.state.workspace.get(pid)
    event_ids = [event["id"] for event in events]
    result = emit_notification_candidate(
        project,
        {
            "source_kind": "watch",
            "source_ref": {
                "watch_id": refs["watch"]["id"],
                "run_id": refs["run"]["id"],
            },
            "source_event_ids": event_ids,
            "dedupe_key": "worker-exhaustion",
            "title": "Budget watch found rows",
            "summary": "Budget rows matched.",
            "severity": "warning",
            "deep_link": {"kind": "watch_run", "watch_id": refs["watch"]["id"]},
            "payload": {},
        },
    )
    request_id = result["planned_delivery_requests"][0]["id"]
    job_id = enqueue_notification_delivery(
        client.app.state.workspace.queue,
        workspace_root=client.app.state.workspace.root,
        project_id=pid,
        request_id=request_id,
        max_attempts=2,
    )

    assert _drain_one(client) is True
    reconcile_notification_delivery_requests(
        client.app.state.workspace.queue,
        workspace_root=client.app.state.workspace.root,
        project_id=pid,
    )
    assert _drain_one(client) is True

    failed = project.public_notification_delivery_request(request_id)
    assert failed["status"] == "failed"
    assert failed["last_error"] == "still down"
    assert client.app.state.workspace.queue.get(job_id).status == "failed"
    terminal_rows = project.db.execute(
        "SELECT status FROM notification_delivery_requests WHERE id=?",
        (request_id,),
    ).fetchall()
    assert [row["status"] for row in terminal_rows] == ["failed"]


def test_notification_delivery_handler_uses_payload_workspace_root(
    tmp_path: Path,
) -> None:
    registered_root = tmp_path / "data" / "projects"
    org_root = registered_root / "1"
    org_root.mkdir(parents=True)
    project_id = "hosted-delivery-root"
    project = Project.create(org_root / f"{project_id}.frisket", name=project_id)
    try:
        channel = project.create_notification_channel(
            kind="email",
            name="Hosted email",
            config={"to": "alerts@example.com"},
            secret_ref="env:RESEND_API_KEY",
        )
        project.create_notification_route(
            name="Hosted route",
            channel_id=channel["id"],
            source_kind="plugin.hosted",
            delivery_mode="immediate",
        )
        emitted = emit_notification_candidate(
            project,
            {
                "source_kind": "plugin.hosted",
                "source_ref": {"plugin_id": "hosted"},
                "source_event_ids": [],
                "dedupe_key": "hosted-delivery-root",
                "title": "Hosted delivery",
                "summary": "Payload workspace roots must win.",
                "severity": "warning",
                "deep_link": {"kind": "plugin_source", "plugin_id": "hosted"},
                "payload": {},
            },
        )
        request_id = int(emitted["planned_delivery_requests"][0]["id"])
    finally:
        project.close()

    queue = open_queue(workspace=tmp_path / "queue")
    registry = default_registry()
    provider = SequencedProvider(
        [DeliveryProviderResult(status="sent", provider_ref="fake-hosted:1")]
    )
    register_notification_handlers(
        registry,
        workspace_root=registered_root,
        delivery_runtime=NotificationDeliveryRuntime(
            provider_factory=lambda _channel: provider,
            secret_resolver=StaticNotificationSecretResolver(
                {"env:RESEND_API_KEY": "resend-secret"}
            ),
        ),
        queue=queue,
    )
    enqueue_notification_delivery(
        queue,
        workspace_root=org_root,
        project_id=project_id,
        request_id=request_id,
    )

    worker = Worker(queue, registry, worker_id="hosted-root", retry_base_seconds=0)
    assert worker.run_once() is True

    project = Project(org_root / f"{project_id}.frisket")
    try:
        request = project.public_notification_delivery_request(request_id)
        assert request["status"] == "sent"
        assert request["provider_ref"] == "fake-hosted:1"
        assert provider.messages[0].title == "Hosted delivery"
    finally:
        project.close()
