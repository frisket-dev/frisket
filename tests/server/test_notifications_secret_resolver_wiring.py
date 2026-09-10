from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.engine.jobs import Worker
from frisket.server.notifications.delivery import (
    DeliveryProviderResult,
    NotificationDeliveryRuntime,
    NotificationRenderedMessage,
)
from frisket.server.notifications.secrets import StaticNotificationSecretResolver
from frisket.server.app import create_app


ROOT = Path(__file__).resolve().parents[2]
CLI_SRC = ROOT / "src" / "frisket" / "cli.py"


CSV = (
    "title,status,body\n"
    "Budget hearing,open,The council discussed budget oversight.\n"
    "Budget audit,open,Auditors found budget variances.\n"
)


class RecordingProvider:
    def __init__(self) -> None:
        self.channels: list[dict[str, Any]] = []
        self.messages: list[NotificationRenderedMessage] = []

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        self.messages.append(message)
        self.channels.append(channel)
        return DeliveryProviderResult(status="sent", provider_ref="recorded")


def _seed_watch(client: TestClient) -> tuple[str, dict[str, Any], list[dict]]:
    pid = client.post("/api/projects", json={"name": "Resolver wiring"}).json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("watch.csv", CSV, "text/csv")},
    )
    assert imported.status_code == 200, imported.text
    watch = client.post(
        f"/api/projects/{pid}/watches",
        json={
            "name": "Budget",
            "scope": {"kind": "project"},
            "query": {"kind": "fts", "q": "budget", "limit": 10},
        },
    ).json()
    run = client.post(f"/api/projects/{pid}/watches/{watch['id']}/run").json()["run"]
    events = client.get(
        f"/api/projects/{pid}/watches/{watch['id']}/runs/{run['id']}/events",
        params={"limit": 10},
    ).json()["events"]
    return pid, {"watch": watch, "run": run}, events


def _create_email_route(client: TestClient, pid: str, watch_id: int) -> None:
    channel = client.post(
        f"/api/projects/{pid}/notification-channels",
        json={
            "kind": "email",
            "name": "Email",
            "to": "alerts@example.com",
            "secret_ref": "env:RESEND_API_KEY",
        },
    )
    assert channel.status_code == 200, channel.text
    route = client.post(
        f"/api/projects/{pid}/notification-routes",
        json={
            "name": "Email route",
            "channel_id": channel.json()["id"],
            "source_kind": "watch",
            "source_ref_match": {"watch_id": watch_id},
        },
    )
    assert route.status_code == 200, route.text


def _emit(
    client: TestClient, pid: str, refs: dict[str, Any], events: list[dict]
) -> int:
    response = client.post(
        f"/api/projects/{pid}/notifications/emit",
        json={
            "source_kind": "watch",
            "source_ref": {
                "watch_id": refs["watch"]["id"],
                "run_id": refs["run"]["id"],
            },
            "source_event_ids": [event["id"] for event in events],
            "dedupe_key": "resolver-wiring",
            "title": "Budget rows",
            "summary": "Budget rows matched.",
            "severity": "warning",
            "deep_link": {"kind": "watch_run", "watch_id": refs["watch"]["id"]},
            "payload": {},
        },
    )
    assert response.status_code == 200, response.text
    return int(response.json()["planned_delivery_requests"][0]["id"])


def test_create_app_threads_notification_secret_resolver_into_worker(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider()
    runtime = NotificationDeliveryRuntime(
        provider_factory=lambda _channel: provider,
        secret_resolver=StaticNotificationSecretResolver(
            {"env:RESEND_API_KEY": "static-resend-secret"}
        ),
    )
    client = TestClient(
        create_app(tmp_path / "ws", notification_delivery_runtime=runtime)
    )
    pid, refs, events = _seed_watch(client)
    _create_email_route(client, pid, int(refs["watch"]["id"]))
    request_id = _emit(client, pid, refs, events)
    ws = client.app.state.workspace

    worker = Worker(ws.queue, ws.registry, worker_id="resolver-worker")
    assert worker.run_once() is True

    assert provider.channels[0]["secret_value"] == "static-resend-secret"
    request = ws.get(pid).public_notification_delivery_request(request_id)
    assert request["status"] == "sent"
