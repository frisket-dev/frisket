from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.jobs.notifications_delivery import deliver_notification_request
from frisket.server.notifications.delivery import NotificationDeliveryRuntime
from frisket.server.notifications.providers import (
    DEFAULT_RESEND_ENDPOINT,
    ProviderHttpResponse,
    ResendEmailNotificationProvider,
    SlackIncomingWebhookNotificationProvider,
    notification_provider_for_channel,
)
from frisket.server.notifications.secrets import StaticNotificationSecretResolver
from frisket.server.notifications.service import (
    create_notification_test_request,
    emit_notification_candidate,
    normalize_channel_input,
)
from frisket.engine.store import Project


class RecordingPost:
    def __init__(self, responses: list[ProviderHttpResponse]) -> None:
        self.responses = list(responses)
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
        if not self.responses:
            raise AssertionError("unexpected provider HTTP call")
        return self.responses.pop(0)


def _create_project(tmp_path: Path, project_id: str) -> Project:
    return Project.create(tmp_path / f"{project_id}.frisket", name=project_id)


def _open_project(tmp_path: Path, project_id: str) -> Project:
    return Project(tmp_path / f"{project_id}.frisket")


def _create_item_request(
    project: Project,
    *,
    channel_id: int,
    dedupe_key: str,
) -> int:
    project.create_notification_route(
        name="Plugin alerts",
        channel_id=channel_id,
        source_kind="plugin.acme",
        delivery_mode="immediate",
    )
    result = emit_notification_candidate(
        project,
        {
            "source_kind": "plugin.acme",
            "source_ref": {"plugin_id": "acme"},
            "source_event_ids": [],
            "dedupe_key": dedupe_key,
            "title": "Provider smoke",
            "summary": "A provider test notification is ready.",
            "severity": "warning",
            "deep_link": {"kind": "plugin_source", "plugin_id": "acme"},
            "payload": {},
        },
    )
    requests = result["planned_delivery_requests"]
    assert len(requests) == 1
    return int(requests[0]["id"])


def _runtime(post: RecordingPost) -> NotificationDeliveryRuntime:
    def provider_factory(channel: dict[str, Any]):
        if channel["kind"] == "slack":
            return SlackIncomingWebhookNotificationProvider(
                http_post=post,
                safety_check=lambda _url: True,
            )
        return notification_provider_for_channel(channel, http_post=post)

    return NotificationDeliveryRuntime(
        provider_factory=provider_factory,
        secret_resolver=StaticNotificationSecretResolver(
            {
                "env:RESEND_API_KEY": "resend-token-secret",
                "env:SLACK_WEBHOOK_URL": (
                    "https://hooks.slack.com/services/T000/B000/SECRET"
                ),
            }
        ),
    )


def _attempt(project: Project) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT id FROM notification_delivery_attempts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return project.notification_delivery_attempt(int(row["id"]))


def test_resend_email_provider_sends_request_and_redacts_response_meta(
    tmp_path: Path,
) -> None:
    project_id = "providers-email"
    project = _create_project(tmp_path, project_id)
    channel = project.create_notification_channel(
        kind="email",
        name="Ops email",
        config={
            "to": "alerts@example.com",
            "from": "frisket <alerts@example.com>",
        },
        secret_ref="env:RESEND_API_KEY",
    )
    request_id = _create_item_request(
        project,
        channel_id=channel["id"],
        dedupe_key="provider-email",
    )
    project.close()
    post = RecordingPost(
        [
            ProviderHttpResponse(
                status_code=202,
                text="accepted",
                json_body={
                    "id": "email_123",
                    "api_key": "rk_live_should_not_persist",
                    "url": "https://api.resend.com/emails/internal/path",
                },
            )
        ]
    )

    result = deliver_notification_request(
        workspace_root=tmp_path,
        project_id=project_id,
        request_id=request_id,
        job_id=None,
        delivery_runtime=_runtime(post),
    )

    assert result == {"request_id": request_id, "status": "sent"}
    assert post.calls[0]["url"] == DEFAULT_RESEND_ENDPOINT
    assert post.calls[0]["headers"]["Authorization"] == ("Bearer resend-token-secret")
    assert post.calls[0]["headers"]["Idempotency-Key"].startswith(
        "frisket-notification-"
    )
    assert post.calls[0]["payload"]["to"] == "alerts@example.com"
    assert post.calls[0]["payload"]["from"] == "frisket <alerts@example.com>"
    assert post.calls[0]["payload"]["subject"] == "Provider smoke"

    project = _open_project(tmp_path, project_id)
    try:
        request = project.public_notification_delivery_request(request_id)
        assert request["status"] == "sent"
        assert request["provider_ref"] == "resend:email_123"
        attempt = _attempt(project)
        assert attempt["status"] == "sent"
        assert attempt["response_meta"]["response"]["api_key"] == "[redacted]"
        assert attempt["response_meta"]["response"]["url"] == (
            "https://api.resend.com/[redacted]"
        )
        serialized = json.dumps(attempt["response_meta"], sort_keys=True)
        assert "rk_live_should_not_persist" not in serialized
        assert "resend-token-secret" not in serialized
        assert "/emails/internal/path" not in serialized
    finally:
        project.close()


def test_slack_provider_failure_redacts_webhook_path_tokens_and_meta(
    tmp_path: Path,
) -> None:
    project_id = "providers-slack"
    project = _create_project(tmp_path, project_id)
    channel = project.create_notification_channel(
        kind="slack",
        name="Ops Slack",
        config={
            "channel_label": "#ops",
            "webhook_host": "hooks.slack.com",
        },
        secret_ref="env:SLACK_WEBHOOK_URL",
    )
    request_id = _create_item_request(
        project,
        channel_id=channel["id"],
        dedupe_key="provider-slack",
    )
    project.close()
    post = RecordingPost(
        [
            ProviderHttpResponse(
                status_code=400,
                text=(
                    "invalid token slack-secret-token at "
                    "https://hooks.slack.com/services/T000/B000/SECRET "
                    "api_key=leaked"
                ),
                json_body={
                    "error": (
                        "invalid token at "
                        "https://hooks.slack.com/services/T000/B000/SECRET"
                    ),
                    "token": "slack-secret-token",
                    "request_url": (
                        "https://hooks.slack.com/services/T000/B000/SECRET"
                    ),
                },
                headers={"retry-after": "5"},
            )
        ]
    )

    result = deliver_notification_request(
        workspace_root=tmp_path,
        project_id=project_id,
        request_id=request_id,
        job_id=None,
        delivery_runtime=_runtime(post),
    )

    assert result == {"request_id": request_id, "status": "failed"}
    assert post.calls[0]["url"] == ("https://hooks.slack.com/services/T000/B000/SECRET")
    assert post.calls[0]["payload"]["text"].startswith("*Provider smoke*")

    project = _open_project(tmp_path, project_id)
    try:
        request = project.public_notification_delivery_request(request_id)
        assert request["status"] == "failed"
        assert "https://hooks.slack.com/[redacted]" in request["last_error"]
        assert "/services/T000/B000/SECRET" not in request["last_error"]
        assert "slack-secret-token" not in request["last_error"]
        assert "api_key=leaked" not in request["last_error"]
        attempt = _attempt(project)
        serialized = json.dumps(attempt, sort_keys=True)
        assert "hooks.slack.com/[redacted]" in serialized
        assert "/services/T000/B000/SECRET" not in serialized
        assert "slack-secret-token" not in serialized
        assert "api_key=leaked" not in serialized
        assert attempt["response_meta"]["response"]["token"] == "[redacted]"
    finally:
        project.close()


def test_slack_provider_blocks_non_slack_webhook_without_http_call() -> None:
    post = RecordingPost([])
    provider = SlackIncomingWebhookNotificationProvider(
        http_post=post,
        safety_check=lambda _url: True,
    )

    result = provider.send(
        _live_message(),
        channel={
            "kind": "slack",
            "config": {"webhook_host": "evil.example"},
            "secret_value": "https://evil.example/services/T000/B000/SECRET",
        },
    )

    assert result.status == "failed"
    assert result.transient is False
    assert post.calls == []
    assert "blocked Slack webhook host" in (result.error or "")
    assert "https://evil.example/[redacted]" in (result.error or "")
    assert "/services/T000/B000/SECRET" not in (result.error or "")


def test_route_test_request_with_null_notification_id_records_attempt(
    tmp_path: Path,
) -> None:
    project_id = "providers-test-request"
    project = _create_project(tmp_path, project_id)
    channel = project.create_notification_channel(
        kind="email",
        name="Test email",
        config={"to": "alerts@example.com"},
        secret_ref="env:RESEND_API_KEY",
    )
    route = project.create_notification_route(
        name="Test route",
        channel_id=channel["id"],
        source_kind="plugin.acme",
    )
    request = create_notification_test_request(project, route["id"])
    request_id = int(request["id"])
    assert request["notification_id"] is None
    project.close()
    post = RecordingPost(
        [
            ProviderHttpResponse(
                status_code=202,
                json_body={"id": "email_test_123"},
            )
        ]
    )

    result = deliver_notification_request(
        workspace_root=tmp_path,
        project_id=project_id,
        request_id=request_id,
        job_id=None,
        delivery_runtime=_runtime(post),
    )

    assert result == {"request_id": request_id, "status": "sent"}
    assert post.calls[0]["payload"]["subject"] == "Test notification"
    project = _open_project(tmp_path, project_id)
    try:
        attempt = _attempt(project)
        assert attempt["notification_id"] is None
        assert attempt["delivery_request_id"] == request_id
        assert attempt["provider_ref"] == "resend:email_test_123"
    finally:
        project.close()


def test_generic_webhook_channel_normalization_uses_secret_refs() -> None:
    normalized = normalize_channel_input(
        {
            "kind": "webhook",
            "name": "Generic webhook",
            "webhook_url": "https://hooks.example.com/notify/TENANT/secret",
            "webhook_url_secret_ref": "env:WEBHOOK_URL",
            "signing_secret_ref": "env:WEBHOOK_SIGNING_SECRET",
            "signature_header": "X-Frisket-Signature",
        }
    )

    assert normalized["kind"] == "webhook"
    assert normalized["secret_ref"] == "env:WEBHOOK_URL"
    assert normalized["config"] == {
        "webhook_host": "hooks.example.com",
        "signing_secret_ref": "env:WEBHOOK_SIGNING_SECRET",
        "signature_header": "X-Frisket-Signature",
        "signing_algorithm": "hmac-sha256",
    }
    assert "/notify/TENANT/secret" not in json.dumps(normalized["config"])


@pytest.mark.skipif(
    not (
        os.environ.get("FRISKET_NOTIFICATION_LIVE_PROVIDER_TESTS") == "1"
        and os.environ.get("RESEND_API_KEY")
        and os.environ.get("FRISKET_NOTIFICATION_TEST_EMAIL")
    ),
    reason="set FRISKET_NOTIFICATION_LIVE_PROVIDER_TESTS=1, RESEND_API_KEY, and FRISKET_NOTIFICATION_TEST_EMAIL",
)
def test_live_resend_provider_is_opt_in() -> None:
    provider = ResendEmailNotificationProvider()
    result = provider.send(
        _live_message(),
        channel={
            "kind": "email",
            "config": {"to": os.environ["FRISKET_NOTIFICATION_TEST_EMAIL"]},
            "secret_value": os.environ["RESEND_API_KEY"],
        },
    )
    assert result.status == "sent"


@pytest.mark.skipif(
    not (
        os.environ.get("FRISKET_NOTIFICATION_LIVE_PROVIDER_TESTS") == "1"
        and os.environ.get("SLACK_WEBHOOK_URL")
    ),
    reason="set FRISKET_NOTIFICATION_LIVE_PROVIDER_TESTS=1 and SLACK_WEBHOOK_URL",
)
def test_live_slack_provider_is_opt_in() -> None:
    provider = SlackIncomingWebhookNotificationProvider()
    result = provider.send(
        _live_message(),
        channel={
            "kind": "slack",
            "config": {},
            "secret_value": os.environ["SLACK_WEBHOOK_URL"],
        },
    )
    assert result.status == "sent"


def _live_message():
    from frisket.server.notifications.delivery import NotificationRenderedMessage

    return NotificationRenderedMessage(
        project_id="live-provider-test",
        delivery_request_id=1,
        route_id=None,
        channel_id=1,
        delivery_kind="test",
        title="frisket notification provider live test",
        summary="This live test is opt-in and skipped by default.",
    )
