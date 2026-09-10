from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.engine.executor import run_action_spec
from frisket.engine.jobs.notifications_delivery import deliver_notification_request
from frisket.server.notifications.delivery import (
    DeliveryProviderResult,
    NotificationDeliveryRuntime,
    NotificationRenderedMessage,
)
from frisket.server.notifications.producers import (
    NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND,
    delivery_health_notification_candidate,
    export_notification_candidate,
    source_health_notification_candidate,
)
from frisket.server.notifications.secrets import StaticNotificationSecretResolver
from frisket.server.notifications.service import (
    LOCAL_ACTOR_ID,
    emit_notification_candidate,
)
from frisket.engine.store import Project


class FailingProvider:
    def __init__(self, error: str = "provider down") -> None:
        self.error = error
        self.messages: list[NotificationRenderedMessage] = []

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        self.messages.append(message)
        return DeliveryProviderResult(status="failed", error=self.error)


def _project(tmp_path: Path, project_id: str) -> Project:
    return Project.create(tmp_path / f"{project_id}.frisket", name=project_id)


def _external_route(project: Project) -> dict[str, Any]:
    channel = project.create_notification_channel(
        kind="email",
        name="Ops email",
        config={"to": "ops@example.com"},
        secret_ref="env:RESEND_API_KEY",
    )
    return project.create_notification_route(
        name="All notifications",
        channel_id=channel["id"],
        delivery_mode="immediate",
    )


def _plugin_candidate(project_id: str) -> dict[str, Any]:
    return {
        "source_kind": "plugin.producer_expansion",
        "source_ref": {"project_id": project_id, "plugin_id": "producer-expansion"},
        "dedupe_key": f"plugin:{project_id}:producer-expansion",
        "title": "Producer expansion fixture",
        "summary": "A normal notification that should use external routes.",
        "severity": "warning",
        "deep_link": {"kind": "plugin_source", "plugin_id": "producer-expansion"},
        "payload": {},
    }


def test_source_health_candidates_cover_failure_stale_and_recovery() -> None:
    failed = source_health_notification_candidate(
        project_id="proj",
        source_id=7,
        source_name="Court RSS",
        source_run_id=44,
        status="failed",
        error="feed timeout",
    )
    assert failed is not None
    assert failed["source_kind"] == "source"
    assert failed["source_ref"] == {
        "project_id": "proj",
        "source_id": 7,
        "status": "failed",
        "source_run_id": 44,
    }
    assert failed["dedupe_key"] == "source:proj:7:failed:44"
    assert failed["severity"] == "warning"
    assert failed["payload"]["error"] == "feed timeout"

    stale = source_health_notification_candidate(
        project_id="proj",
        source_id=7,
        source_name="Court RSS",
        status="stale",
        stale_reason="missed_two_intervals",
        schedule="@hourly",
    )
    assert stale is not None
    assert stale["dedupe_key"] == "source:proj:7:stale:missed_two_intervals"
    assert stale["source_ref"]["stale_reason"] == "missed_two_intervals"
    assert stale["payload"]["schedule"] == "@hourly"

    recovered = source_health_notification_candidate(
        project_id="proj",
        source_id=7,
        source_name="Court RSS",
        source_run_id=45,
        status="recovered",
    )
    assert recovered is not None
    assert recovered["dedupe_key"] == "source:proj:7:recovered:45"
    assert recovered["severity"] == "info"
    assert (
        source_health_notification_candidate(
            project_id="proj",
            source_id=7,
            status="running",
        )
        is None
    )


def test_export_candidates_cover_ready_and_failed_reports() -> None:
    ready = export_notification_candidate(
        project_id="proj",
        export_kind="export.google_sheets",
        status="completed",
        receipt_id="receipt_123",
        artifact_ref={"kind": "google_sheet", "url": "https://docs.example/sheet"},
    )
    assert ready is not None
    assert ready["source_kind"] == "export"
    assert ready["source_ref"] == {
        "project_id": "proj",
        "export_kind": "export.google_sheets",
        "status": "ready",
        "receipt_id": "receipt_123",
    }
    assert ready["dedupe_key"] == ("export:proj:export.google_sheets:receipt_123:ready")
    assert ready["severity"] == "info"
    assert ready["payload"]["artifact_ref"]["kind"] == "google_sheet"

    failed = export_notification_candidate(
        project_id="proj",
        export_kind="export.work_log",
        status="failed",
        receipt_id="receipt_124",
        error="destination denied",
    )
    assert failed is not None
    assert failed["dedupe_key"] == "export:proj:export.work_log:receipt_124:failed"
    assert failed["severity"] == "warning"
    assert failed["payload"]["error"] == "destination denied"


def test_export_runner_emits_export_notification_candidate(
    tmp_path: Path,
) -> None:
    project_id = "export-producer-integration"
    project = _project(tmp_path, project_id)
    try:
        result = run_action_spec(
            project,
            {
                "action_id": "export.work_log",
                "scope": {"kind": "project"},
                "params": {
                    "destination": {
                        "kind": "local_file",
                        "path": str(tmp_path / "work-log.md"),
                    },
                    "include_receipts": True,
                },
                "idempotency_key": "notifications/export_work_log@sha256:v1",
            },
            project_id=project_id,
        )

        assert result.status == "completed"
        rows = project.db.execute(
            "SELECT * FROM notification_items WHERE source_kind='export'"
        ).fetchall()
        assert len(rows) == 1
        source_ref = json.loads(rows[0]["source_ref"])
        assert source_ref["project_id"] == project_id
        assert source_ref["export_kind"] == "export.work_log"
        assert source_ref["status"] == "ready"
        assert source_ref["receipt_id"] == result.receipt_id
    finally:
        project.close()


def test_delivery_health_items_are_feed_only_to_prevent_feedback_loops(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "delivery-health-feed-only")
    try:
        _external_route(project)
        candidate = delivery_health_notification_candidate(
            project_id="delivery-health-feed-only",
            request_id=99,
            status="failed",
            route_id=4,
            channel_id=3,
            error="notification delivery job missing",
        )
        assert candidate is not None
        result = emit_notification_candidate(project, candidate)

        assert result["planned_delivery_requests"] == []
        assert (
            project.db.execute(
                "SELECT COUNT(*) AS n FROM notification_delivery_requests"
            ).fetchone()["n"]
            == 0
        )
        item = project.notification_item(result["notification_id"])
        assert item is not None
        assert item["source_kind"] == NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND
    finally:
        project.close()


def test_failed_delivery_emits_health_notification_without_external_feedback(
    tmp_path: Path,
) -> None:
    project_id = "delivery-health-integration"
    project = _project(tmp_path, project_id)
    try:
        _external_route(project)
        emitted = emit_notification_candidate(project, _plugin_candidate(project_id))
        request_id = int(emitted["planned_delivery_requests"][0]["id"])
        assert (
            project.db.execute(
                "SELECT COUNT(*) AS n FROM notification_delivery_requests"
            ).fetchone()["n"]
            == 1
        )
    finally:
        project.close()

    provider = FailingProvider()
    result = deliver_notification_request(
        workspace_root=tmp_path,
        project_id=project_id,
        request_id=request_id,
        job_id=None,
        delivery_runtime=NotificationDeliveryRuntime(
            provider_factory=lambda _channel: provider,
            secret_resolver=StaticNotificationSecretResolver(
                {"env:RESEND_API_KEY": "resend-secret"}
            ),
        ),
    )
    assert result == {"request_id": request_id, "status": "failed"}
    assert provider.messages

    project = Project(tmp_path / f"{project_id}.frisket")
    try:
        health_rows = project.db.execute(
            "SELECT * FROM notification_items WHERE source_kind=?",
            (NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND,),
        ).fetchall()
        assert len(health_rows) == 1
        health_ref = json.loads(health_rows[0]["source_ref"])
        assert health_ref["request_id"] == request_id
        assert health_ref["status"] == "failed"
        assert "provider down" in health_rows[0]["summary"]
        assert (
            project.db.execute(
                "SELECT COUNT(*) AS n FROM notification_delivery_requests"
            ).fetchone()["n"]
            == 1
        )
        summary = project.notification_summary(actor_id=LOCAL_ACTOR_ID)
        assert summary["by_source_kind"][NOTIFICATION_DELIVERY_HEALTH_SOURCE_KIND] == 1
    finally:
        project.close()
