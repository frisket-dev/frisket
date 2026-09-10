from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from helpers import make_client
from frisket.engine.jobs import Worker
from frisket.server.notifications.delivery import NotificationDeliveryRuntime
from frisket.server.notifications.providers import (
    ProviderHttpResponse,
    SlackIncomingWebhookNotificationProvider,
)
from frisket.server.notifications.secrets import StaticNotificationSecretResolver


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Bridge repair,closed,The bridge repair vote was tabled.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)
SLACK_WEBHOOK = "https://hooks.slack.com/services/T000/B000/SECRET"


class RecordingSlackTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> ProviderHttpResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        return ProviderHttpResponse(status_code=200, text="ok")


def _client(tmp_path: Path, transport: RecordingSlackTransport) -> TestClient:
    runtime = NotificationDeliveryRuntime(
        provider_factory=lambda _channel: SlackIncomingWebhookNotificationProvider(
            http_post=transport,
            safety_check=lambda _url: True,
        ),
        secret_resolver=StaticNotificationSecretResolver(
            {"env:SLACK_WEBHOOK_URL": SLACK_WEBHOOK}
        ),
    )
    return make_client(tmp_path, notification_delivery_runtime=runtime)


def test_watch_event_routes_through_worker_to_recorded_slack_transport(
    tmp_path: Path,
) -> None:
    transport = RecordingSlackTransport()
    client = _client(tmp_path, transport)
    pid = client.post("/api/projects", json={"name": "Watch alerts"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
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
    channel_response = client.post(
        f"/api/projects/{pid}/notification-channels",
        json={
            "kind": "slack",
            "name": "Budget desk Slack",
            "channel_label": "#budget",
            "webhook_host": "hooks.slack.com",
            "secret_ref": "env:SLACK_WEBHOOK_URL",
        },
    )
    assert channel_response.status_code == 200, channel_response.text
    channel = channel_response.json()
    route_response = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Immediate budget alerts",
            "channel_id": channel["id"],
            "source_kind": "watch",
            "source_ref_match": {"watch_id": watch["id"]},
            "delivery_mode": "immediate",
        },
    )
    assert route_response.status_code == 200, route_response.text

    run_response = client.post(f"/api/projects/{pid}/watches/{watch['id']}/run")
    assert run_response.status_code == 200, run_response.text
    assert run_response.json()["run"]["new_rows"] == 2

    queued_page = client.get(
        f"/api/projects/{pid}/notification-delivery-requests"
    ).json()
    assert queued_page["total"] == 1
    queued = queued_page["delivery_requests"][0]
    assert queued["status"] == "queued"
    assert queued["job_id"] is not None
    assert transport.calls == []

    workspace = client.app.state.workspace
    worker = Worker(workspace.queue, workspace.registry, worker_id="notification-e2e")
    assert worker.run_once() is True

    sent_page = client.get(f"/api/projects/{pid}/notification-delivery-requests").json()
    sent = sent_page["delivery_requests"][0]
    assert sent["id"] == queued["id"]
    assert sent["status"] == "sent"
    assert sent["provider_ref"] == f"slack:webhook:{sent['id']}"
    assert sent["last_error"] is None

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == SLACK_WEBHOOK
    assert call["headers"] == {"Content-Type": "application/json"}
    assert call["payload"]["text"].startswith("*Budget watch recorded 2 watch events*")

    project = workspace.get(pid)
    attempts = project.db.execute(
        "SELECT status, provider_ref FROM notification_delivery_attempts "
        "WHERE delivery_request_id=?",
        (sent["id"],),
    ).fetchall()
    assert [(row["status"], row["provider_ref"]) for row in attempts] == [
        ("sent", f"slack:webhook:{sent['id']}")
    ]
