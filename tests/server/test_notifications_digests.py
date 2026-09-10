from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from frisket.engine.jobs import (
    Worker,
    default_registry,
    enqueue_due_notification_digests,
    open_queue,
    register_notification_digest_handlers,
    register_notification_handlers,
)
from frisket.server.notifications.delivery import (
    DeliveryProviderResult,
    NotificationDeliveryRuntime,
    NotificationRenderedMessage,
)
from frisket.server.notifications.digests import (
    compose_notification_digest,
    due_digest_window_for_route,
    manual_digest_window,
)
from frisket.server.notifications.secrets import StaticNotificationSecretResolver
from frisket.server.notifications.service import (
    LOCAL_ACTOR_ID,
    emit_notification_candidate,
    normalize_route_input,
)
from frisket.engine.store import Project


class RecordingProvider:
    def __init__(self) -> None:
        self.messages: list[NotificationRenderedMessage] = []
        self.channels: list[dict[str, Any]] = []

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        self.messages.append(message)
        self.channels.append(channel)
        return DeliveryProviderResult(status="sent", provider_ref="digest:sent")


def _create_project(tmp_path: Path, project_id: str) -> Project:
    return Project.create(tmp_path / f"{project_id}.frisket", name=project_id)


def _open_project(tmp_path: Path, project_id: str) -> Project:
    return Project(tmp_path / f"{project_id}.frisket")


def _channels(project: Project) -> tuple[dict[str, Any], dict[str, Any]]:
    slack = project.create_notification_channel(
        kind="slack",
        name="Immediate Slack",
        config={"channel_label": "#ops", "webhook_host": "hooks.slack.com"},
        secret_ref="env:SLACK_WEBHOOK_URL",
    )
    email = project.create_notification_channel(
        kind="email",
        name="Digest email",
        config={"to": "alerts@example.com"},
        secret_ref="env:RESEND_API_KEY",
    )
    return slack, email


def _emit_plugin_item(
    project: Project,
    *,
    dedupe_key: str,
    created_at: str,
    severity: str = "warning",
) -> dict[str, Any]:
    result = emit_notification_candidate(
        project,
        {
            "source_kind": "plugin.acme",
            "source_ref": {"plugin_id": "acme"},
            "source_event_ids": [],
            "dedupe_key": dedupe_key,
            "title": f"Notification {dedupe_key}",
            "summary": f"Summary for {dedupe_key}.",
            "severity": severity,
            "deep_link": {"kind": "plugin_source", "plugin_id": "acme"},
            "payload": {},
        },
    )
    project.db.execute(
        "UPDATE notification_items SET created_at=?, updated_at=? WHERE id=?",
        (created_at, created_at, int(result["notification_id"])),
    )
    project.db.commit()
    return result


def _runtime(provider: RecordingProvider) -> NotificationDeliveryRuntime:
    return NotificationDeliveryRuntime(
        provider_factory=lambda _channel: provider,
        secret_resolver=StaticNotificationSecretResolver(
            {
                "env:RESEND_API_KEY": "resend-secret",
                "env:SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/S",
            }
        ),
    )


def test_daily_digest_route_composes_membership_without_immediate_email_request(
    tmp_path: Path,
) -> None:
    project_id = "digest-coexistence"
    project = _create_project(tmp_path, project_id)
    slack, email = _channels(project)
    slack_route = project.create_notification_route(
        name="Immediate Slack",
        channel_id=slack["id"],
        source_kind="plugin.acme",
        delivery_mode="immediate",
    )
    digest_route = project.create_notification_route(
        name="Daily email",
        channel_id=email["id"],
        source_kind="plugin.acme",
        delivery_mode="digest",
        digest_cadence="daily",
        digest_timezone="UTC",
        digest_anchor_time="09:00",
    )

    emitted = _emit_plugin_item(
        project,
        dedupe_key="digest-item-1",
        created_at="2026-06-29 10:15:00",
    )

    assert [
        request["route_id"] for request in emitted["planned_delivery_requests"]
    ] == [slack_route["id"]]
    assert emitted["planned_delivery_requests"][0]["channel_id"] == slack["id"]
    run = compose_notification_digest(
        project,
        digest_route["id"],
        now=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
    )

    assert run["status"] == "composed"
    assert run["item_count"] == 1
    assert run["window_start_at"] == "2026-06-29 09:00:00"
    assert run["window_end_at"] == "2026-06-30 09:00:00"
    assert project.notification_digest_item_ids(run["id"]) == [
        emitted["notification_id"]
    ]
    request = project.public_notification_delivery_request(run["delivery_request_id"])
    assert request["delivery_kind"] == "digest"
    assert request["digest_run_id"] == run["id"]
    assert request["notification_id"] is None
    assert request["channel_id"] == email["id"]

    repeated = compose_notification_digest(
        project,
        digest_route["id"],
        now=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
    )
    assert repeated["id"] == run["id"]
    assert repeated["delivery_request_id"] == run["delivery_request_id"]
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_requests "
            "WHERE delivery_kind='digest'"
        ).fetchone()["n"]
        == 1
    )
    project.close()


def test_acknowledged_items_are_excluded_and_empty_digest_does_not_send(
    tmp_path: Path,
) -> None:
    project_id = "digest-ack"
    project = _create_project(tmp_path, project_id)
    _slack, email = _channels(project)
    digest_route = project.create_notification_route(
        name="Daily email",
        channel_id=email["id"],
        source_kind="plugin.acme",
        delivery_mode="digest",
        digest_cadence="daily",
        digest_timezone="UTC",
        digest_anchor_time="09:00",
    )
    emitted = _emit_plugin_item(
        project,
        dedupe_key="acknowledged-before-compose",
        created_at="2026-06-29 10:15:00",
    )
    project.acknowledge_notification(emitted["notification_id"], LOCAL_ACTOR_ID)

    run = compose_notification_digest(
        project,
        digest_route["id"],
        now=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
    )

    assert run["status"] == "empty"
    assert run["item_count"] == 0
    assert run["delivery_request_id"] is None
    assert project.notification_digest_item_ids(run["id"]) == []
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_requests "
            "WHERE delivery_kind='digest'"
        ).fetchone()["n"]
        == 0
    )
    project.close()


def test_digest_window_policy_is_utc_only_hourly_daily_and_manual(
    tmp_path: Path,
) -> None:
    project = _create_project(tmp_path, "digest-window-policy")
    _slack, email = _channels(project)
    hourly = project.create_notification_route(
        name="Hourly digest",
        channel_id=email["id"],
        delivery_mode="digest",
        digest_cadence="hourly",
        digest_timezone="UTC",
    )
    hourly_row = project.notification_route(hourly["id"])
    assert hourly_row is not None
    hourly_window = due_digest_window_for_route(
        hourly_row,
        now=datetime(2026, 6, 29, 14, 33, tzinfo=UTC),
    )
    assert hourly_window is not None
    assert hourly_window.start_sql == "2026-06-29 13:00:00"
    assert hourly_window.end_sql == "2026-06-29 14:00:00"

    daily = project.create_notification_route(
        name="Daily digest",
        channel_id=email["id"],
        delivery_mode="digest",
        digest_cadence="daily",
        digest_timezone="UTC",
        digest_anchor_time="09:00",
    )
    daily_row = project.notification_route(daily["id"])
    assert daily_row is not None
    daily_window = due_digest_window_for_route(
        daily_row,
        now=datetime(2026, 6, 29, 8, 30, tzinfo=UTC),
    )
    assert daily_window is not None
    assert daily_window.start_sql == "2026-06-27 09:00:00"
    assert daily_window.end_sql == "2026-06-28 09:00:00"

    manual = project.create_notification_route(
        name="Manual digest",
        channel_id=email["id"],
        delivery_mode="digest",
        digest_cadence="manual",
        digest_timezone="UTC",
    )
    manual_row = project.notification_route(manual["id"])
    assert manual_row is not None
    assert due_digest_window_for_route(manual_row) is None
    with pytest.raises(ValueError, match="manual notification digest requires"):
        compose_notification_digest(project, manual["id"])
    explicit = manual_digest_window(
        start_at=datetime(2026, 6, 29, 0, 0, tzinfo=UTC),
        end_at=datetime(2026, 6, 30, 0, 0, tzinfo=UTC),
    )
    assert explicit.window_key == ("manual:2026-06-29T00:00:00Z:2026-06-30T00:00:00Z")

    with pytest.raises(ValueError, match="timezone must be UTC"):
        normalize_route_input(
            {
                "name": "Local digest",
                "channel_id": email["id"],
                "delivery_mode": "digest",
                "digest_cadence": "daily",
                "digest_timezone": "America/New_York",
            },
            project_id="digest-window-policy",
        )
    project.close()


def test_digest_job_composes_and_enqueues_delivery_request(
    tmp_path: Path,
) -> None:
    project_id = "digest-job"
    project = _create_project(tmp_path, project_id)
    _slack, email = _channels(project)
    route = project.create_notification_route(
        name="Daily job email",
        channel_id=email["id"],
        source_kind="plugin.acme",
        delivery_mode="digest",
        digest_cadence="daily",
        digest_timezone="UTC",
        digest_anchor_time="09:00",
    )
    emitted = _emit_plugin_item(
        project,
        dedupe_key="digest-job-item",
        created_at="2026-06-29 10:15:00",
    )
    assert emitted["planned_delivery_requests"] == []
    project.close()

    provider = RecordingProvider()
    queue = open_queue(workspace=tmp_path)
    registry = default_registry()
    register_notification_handlers(
        registry,
        workspace_root=tmp_path,
        delivery_runtime=_runtime(provider),
        queue=queue,
    )
    register_notification_digest_handlers(
        registry,
        workspace_root=tmp_path,
        queue=queue,
    )

    assert (
        enqueue_due_notification_digests(
            queue,
            workspace_root=tmp_path,
            project_id=project_id,
            now=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
        )
        == 1
    )
    assert (
        enqueue_due_notification_digests(
            queue,
            workspace_root=tmp_path,
            project_id=project_id,
            now=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
        )
        == 0
    )
    worker = Worker(queue, registry, worker_id="digest-worker", retry_base_seconds=0)
    assert worker.run_once() is True

    project = _open_project(tmp_path, project_id)
    try:
        rows = project.db.execute("SELECT * FROM notification_digest_runs").fetchall()
        assert len(rows) == 1
        run = project.public_notification_digest_run(int(rows[0]["id"]))
        assert run["route_id"] == route["id"]
        assert run["item_count"] == 1
        request = project.public_notification_delivery_request(
            run["delivery_request_id"]
        )
        assert request["status"] == "queued"
        assert request["job_id"] is not None
    finally:
        project.close()

    assert worker.run_once() is True
    assert provider.messages[0].delivery_kind == "digest"
    assert provider.messages[0].digest_run_id == run["id"]
    assert provider.messages[0].title == "Daily job email digest"
    assert "Notification digest-job-item" in provider.messages[0].summary
    assert provider.channels[0]["secret_value"] == "resend-secret"


def test_composing_digest_run_is_resumed_instead_of_stranded(
    tmp_path: Path,
) -> None:
    project_id = "digest-resume-composing"
    project = _create_project(tmp_path, project_id)
    _slack, email = _channels(project)
    route = project.create_notification_route(
        name="Daily recoverable digest",
        channel_id=email["id"],
        source_kind="plugin.acme",
        delivery_mode="digest",
        digest_cadence="daily",
        digest_timezone="UTC",
        digest_anchor_time="09:00",
    )
    emitted = _emit_plugin_item(
        project,
        dedupe_key="digest-resume-item",
        created_at="2026-06-29 10:15:00",
    )
    route_row = project.notification_route(route["id"])
    assert route_row is not None
    window = due_digest_window_for_route(
        route_row,
        now=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
    )
    assert window is not None
    stranded = project.create_notification_digest_run(
        route_id=route["id"],
        channel_id=email["id"],
        cadence=window.cadence,
        window_key=window.window_key,
        window_start_at=window.start_sql,
        window_end_at=window.end_sql,
        status="composing",
    )

    run = compose_notification_digest(project, route["id"], window=window)

    assert run["id"] == stranded["id"]
    assert run["status"] == "composed"
    assert run["item_count"] == 1
    assert project.notification_digest_item_ids(run["id"]) == [
        emitted["notification_id"]
    ]
    request = project.public_notification_delivery_request(run["delivery_request_id"])
    assert request["delivery_kind"] == "digest"
    assert request["route_id"] == route["id"]
    repeated = compose_notification_digest(project, route["id"], window=window)
    assert repeated["id"] == run["id"]
    assert (
        project.db.execute(
            "SELECT COUNT(*) AS n FROM notification_delivery_requests "
            "WHERE delivery_kind='digest'"
        ).fetchone()["n"]
        == 1
    )
    project.close()


def test_notification_digest_handler_uses_payload_workspace_root(
    tmp_path: Path,
) -> None:
    registered_root = tmp_path / "data" / "projects"
    org_root = registered_root / "1"
    org_root.mkdir(parents=True)
    project_id = "hosted-digest-root"
    project = _create_project(org_root, project_id)
    _slack, email = _channels(project)
    route = project.create_notification_route(
        name="Hosted digest",
        channel_id=email["id"],
        source_kind="plugin.acme",
        delivery_mode="digest",
        digest_cadence="daily",
        digest_timezone="UTC",
        digest_anchor_time="09:00",
    )
    _emit_plugin_item(
        project,
        dedupe_key="hosted-digest-root",
        created_at="2026-06-29 10:15:00",
    )
    project.close()

    queue = open_queue(workspace=tmp_path / "queue")
    registry = default_registry()
    register_notification_digest_handlers(
        registry,
        workspace_root=registered_root,
        queue=queue,
    )
    assert (
        enqueue_due_notification_digests(
            queue,
            workspace_root=org_root,
            project_id=project_id,
            now=datetime(2026, 6, 30, 9, 5, tzinfo=UTC),
        )
        == 1
    )

    worker = Worker(queue, registry, worker_id="hosted-digest-root")
    assert worker.run_once() is True

    project = _open_project(org_root, project_id)
    try:
        rows = project.db.execute("SELECT * FROM notification_digest_runs").fetchall()
        assert len(rows) == 1
        run = project.public_notification_digest_run(int(rows[0]["id"]))
        assert run["route_id"] == route["id"]
        assert run["status"] == "composed"
        assert run["delivery_request_id"] is not None
        request = project.public_notification_delivery_request(
            run["delivery_request_id"]
        )
        assert request["job_id"] is not None
    finally:
        project.close()
