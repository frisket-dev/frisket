from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.engine.jobs.notifications_delivery import deliver_notification_request
from frisket.server.notifications.delivery import NotificationDeliveryRuntime
from frisket.server.notifications.providers import (
    GenericWebhookNotificationProvider,
    ProviderHttpResponse,
    WEBHOOK_TIMESTAMP_HEADER,
    WebhookDeliverySecurityError,
    _guarded_webhook_post_json,
)
from frisket.server.notifications.secrets import StaticNotificationSecretResolver
from frisket.server.notifications.service import emit_notification_candidate
from frisket.server.app import create_app
from frisket.engine.store import Project


class RecordingPost:
    def __init__(self, response: ProviderHttpResponse | None = None) -> None:
        self.response = response or ProviderHttpResponse(
            status_code=202,
            json_body={"id": "webhook_123"},
        )
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
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.response


class FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeStreamResponse:
    def __init__(
        self,
        *,
        status_code: int,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        redirect_url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._chunks = chunks or []
        self.headers = headers or {}
        self.is_redirect = redirect_url is not None
        self.next_request = FakeRequest(redirect_url or "https://example.test/unused")

    def __enter__(self) -> FakeStreamResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def iter_bytes(self) -> list[bytes]:
        return self._chunks


class FakeClient:
    def __init__(self, responses: list[FakeStreamResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes,
    ) -> FakeStreamResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "content": content,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected webhook HTTP call")
        return self.responses.pop(0)


def _project(tmp_path: Path, project_id: str) -> Project:
    return Project.create(tmp_path / f"{project_id}.frisket", name=project_id)


def _open_project(tmp_path: Path, project_id: str) -> Project:
    return Project(tmp_path / f"{project_id}.frisket")


def _webhook_request(project: Project) -> int:
    channel = project.create_notification_channel(
        kind="webhook",
        name="Ops webhook",
        config={
            "webhook_host": "hooks.example.com",
            "signing_secret_ref": "env:WEBHOOK_SIGNING_SECRET",
            "signature_header": "X-Frisket-Signature",
            "max_response_bytes": 1024,
            "max_request_bytes": 2048,
            "max_redirects": 1,
            "signing_algorithm": "hmac-sha256",
        },
        secret_ref="env:WEBHOOK_URL",
    )
    project.create_notification_route(
        name="Plugin webhook",
        channel_id=channel["id"],
        source_kind="plugin.webhook",
        delivery_mode="immediate",
    )
    result = emit_notification_candidate(
        project,
        {
            "source_kind": "plugin.webhook",
            "source_ref": {"plugin_id": "webhook-security"},
            "source_event_ids": [],
            "dedupe_key": "webhook-security",
            "title": "Webhook security smoke",
            "summary": "A notification webhook is ready.",
            "severity": "warning",
            "deep_link": {"kind": "plugin_source", "plugin_id": "webhook-security"},
            "payload": {"safe": "context"},
        },
    )
    requests = result["planned_delivery_requests"]
    assert len(requests) == 1
    return int(requests[0]["id"])


def _runtime(post: RecordingPost) -> NotificationDeliveryRuntime:
    return NotificationDeliveryRuntime(
        provider_factory=lambda _channel: GenericWebhookNotificationProvider(
            http_post=post,
            safety_check=lambda _url: True,
        ),
        secret_resolver=StaticNotificationSecretResolver(
            {
                "env:WEBHOOK_URL": (
                    "https://hooks.example.com/notify/TENANT/secret?token=hidden"
                ),
                "env:WEBHOOK_SIGNING_SECRET": "webhook-signing-secret",
            }
        ),
    )


def _attempt(project: Project) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT id FROM notification_delivery_attempts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return project.notification_delivery_attempt(int(row["id"]))


def test_webhook_channel_api_redacts_url_and_signing_refs(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Webhook security"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/notification-channels",
        json={
            "kind": "webhook",
            "name": "Ops webhook",
            "webhook_url": "https://hooks.example.com/notify/TENANT/secret",
            "webhook_url_secret_ref": "env:WEBHOOK_URL",
            "signing_secret_ref": "env:WEBHOOK_SIGNING_SECRET",
            "signature_header": "X-Frisket-Signature",
            "max_response_bytes": 1024,
            "max_request_bytes": 2048,
            "max_redirects": 1,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "webhook"
    assert body["has_secret"] is True
    assert body["config"] == {
        "webhook_host": "hooks.example.com",
        "signature_header": "X-Frisket-Signature",
        "signing_algorithm": "hmac-sha256",
        "max_response_bytes": 1024,
        "max_request_bytes": 2048,
        "max_redirects": 1,
        "has_signing_secret": True,
    }
    serialized = json.dumps(body, sort_keys=True)
    assert "WEBHOOK_URL" not in serialized
    assert "WEBHOOK_SIGNING_SECRET" not in serialized
    assert "/notify/TENANT/secret" not in serialized


def test_webhook_provider_signs_payload_and_redacts_persisted_secrets(
    tmp_path: Path,
) -> None:
    project_id = "webhook-provider"
    project = _project(tmp_path, project_id)
    request_id = _webhook_request(project)
    project.close()
    webhook_url = "https://hooks.example.com/notify/TENANT/secret?token=hidden"
    post = RecordingPost(
        ProviderHttpResponse(
            status_code=202,
            json_body={
                "id": "wh_123",
                "url": webhook_url,
                "token": "provider-token",
                "signing_secret": "webhook-signing-secret",
            },
        )
    )

    result = deliver_notification_request(
        workspace_root=tmp_path,
        project_id=project_id,
        request_id=request_id,
        job_id=None,
        delivery_runtime=_runtime(post),
    )

    assert result == {"request_id": request_id, "status": "sent"}
    call = post.calls[0]
    assert call["url"] == webhook_url
    assert call["payload"]["schema_version"] == "frisket.notification_webhook.v1"
    assert call["payload"]["title"] == "Webhook security smoke"
    timestamp = call["headers"][WEBHOOK_TIMESTAMP_HEADER]
    canonical = json.dumps(
        call["payload"],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_signature = hmac.new(
        b"webhook-signing-secret",
        timestamp.encode("utf-8") + b"." + canonical,
        hashlib.sha256,
    ).hexdigest()
    assert call["headers"]["X-Frisket-Signature"] == f"v1={expected_signature}"

    project = _open_project(tmp_path, project_id)
    try:
        request = project.public_notification_delivery_request(request_id)
        assert request["status"] == "sent"
        assert request["provider_ref"] == "webhook:wh_123"
        attempt = _attempt(project)
        serialized = json.dumps(attempt, sort_keys=True)
        assert "hooks.example.com/[redacted]" in serialized
        assert "/notify/TENANT/secret" not in serialized
        assert "token=hidden" not in serialized
        assert "webhook-signing-secret" not in serialized
        assert attempt["response_meta"]["response"]["token"] == "[redacted]"
        assert attempt["response_meta"]["response"]["signing_secret"] == "[redacted]"
    finally:
        project.close()


def test_webhook_provider_blocks_private_url_before_http_call() -> None:
    post = RecordingPost()
    provider = GenericWebhookNotificationProvider(
        http_post=post,
        safety_check=lambda _url: False,
    )

    result = provider.send(
        _message(summary="blocked"),
        channel={
            "kind": "webhook",
            "config": {},
            "secret_value": "https://169.254.169.254/latest/meta-data/?token=leak",
            "signing_secret_value": "signing-secret",
        },
    )

    assert result.status == "failed"
    assert "webhook delivery blocked" in str(result.error)
    assert "/latest/meta-data" not in str(result.error)
    assert "token=leak" not in str(result.error)
    assert post.calls == []


def test_guarded_webhook_post_rechecks_redirect_target() -> None:
    client = FakeClient(
        [
            FakeStreamResponse(
                status_code=302,
                redirect_url="https://127.0.0.1/internal/path?token=leak",
            )
        ]
    )
    checked: list[str] = []

    def safety_check(url: str) -> bool:
        checked.append(url)
        return len(checked) == 1

    with pytest.raises(WebhookDeliverySecurityError, match="blocked webhook redirect"):
        _guarded_webhook_post_json(
            "https://hooks.example.com/notify",
            {"Content-Type": "application/json"},
            {"ok": True},
            1.0,
            client_factory=lambda **_kwargs: client,
            safety_check=safety_check,
        )

    assert checked == [
        "https://hooks.example.com/notify",
        "https://127.0.0.1/internal/path?token=leak",
    ]
    assert len(client.calls) == 1


def test_guarded_webhook_post_caps_response_body() -> None:
    client = FakeClient(
        [
            FakeStreamResponse(
                status_code=200,
                chunks=[b"abcdef"],
                headers={"content-length": "6"},
            )
        ]
    )

    with pytest.raises(WebhookDeliverySecurityError, match="max_response_bytes=5"):
        _guarded_webhook_post_json(
            "https://hooks.example.com/notify",
            {"Content-Type": "application/json"},
            {"ok": True},
            1.0,
            max_response_bytes=5,
            client_factory=lambda **_kwargs: client,
            safety_check=lambda _url: True,
        )


def test_webhook_missing_signing_secret_fails_closed_without_provider_call(
    tmp_path: Path,
) -> None:
    project_id = "webhook-missing-signing"
    project = _project(tmp_path, project_id)
    channel = project.create_notification_channel(
        kind="webhook",
        name="Ops webhook",
        config={"webhook_host": "hooks.example.com"},
        secret_ref="env:WEBHOOK_URL",
    )
    project.create_notification_route(
        name="Plugin webhook",
        channel_id=channel["id"],
        source_kind="plugin.webhook",
        delivery_mode="immediate",
    )
    result = emit_notification_candidate(
        project,
        {
            "source_kind": "plugin.webhook",
            "source_ref": {"plugin_id": "webhook-security"},
            "source_event_ids": [],
            "dedupe_key": "webhook-missing-signing",
            "title": "Webhook missing signing",
            "summary": "Signing secret should be required.",
            "payload": {},
        },
    )
    request_id = int(result["planned_delivery_requests"][0]["id"])
    project.close()
    post = RecordingPost()

    worker_result = deliver_notification_request(
        workspace_root=tmp_path,
        project_id=project_id,
        request_id=request_id,
        job_id=None,
        delivery_runtime=NotificationDeliveryRuntime(
            provider_factory=lambda _channel: GenericWebhookNotificationProvider(
                http_post=post,
                safety_check=lambda _url: True,
            ),
            secret_resolver=StaticNotificationSecretResolver(
                {"env:WEBHOOK_URL": "https://hooks.example.com/notify"}
            ),
        ),
    )

    assert worker_result == {"request_id": request_id, "status": "skipped"}
    assert post.calls == []
    project = _open_project(tmp_path, project_id)
    try:
        request = project.public_notification_delivery_request(request_id)
        assert request["status"] == "skipped"
        assert (
            request["last_error"] == "webhook channel missing signing secret reference"
        )
        assert _attempt(project)["status"] == "skipped"
    finally:
        project.close()


def _message(*, summary: str) -> Any:
    from frisket.server.notifications.delivery import NotificationRenderedMessage

    return NotificationRenderedMessage(
        project_id="project",
        delivery_request_id=1,
        route_id=1,
        channel_id=1,
        delivery_kind="test",
        title="Webhook test",
        summary=summary,
    )
