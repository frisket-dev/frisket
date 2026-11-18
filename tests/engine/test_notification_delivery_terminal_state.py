"""A delayed retry or reconciler cannot rewrite a finished delivery."""

import pytest

from frisket.engine.store.notifications import TERMINAL_REQUEST_STATUSES


@pytest.fixture
def delivery_request(make_project):
    project = make_project("deliveries")
    channel = project.create_notification_channel(kind="in_app", name="In app")
    delivery = project.create_notification_delivery_request(
        route_id=None,
        channel_id=channel["id"],
        delivery_kind="immediate",
        dedupe_key="one-delivery",
    )
    yield project, delivery["id"]


@pytest.mark.parametrize("terminal", sorted(TERMINAL_REQUEST_STATUSES))
def test_late_delivery_paths_preserve_terminal_result(delivery_request, terminal):
    project, identity = delivery_request
    project.claim_notification_delivery_request(identity, job_id=42)
    project.terminalize_notification_delivery_request(
        identity, status=terminal, provider_ref="original", last_error="original detail"
    )
    saved = dict(project.notification_delivery_request(identity))
    for later in TERMINAL_REQUEST_STATUSES:
        project.terminalize_notification_delivery_request(
            identity, status=later, provider_ref="late", last_error="late detail"
        )
    project.set_notification_delivery_request_job_id(identity, 999)
    project.record_notification_delivery_request_error(identity, last_error="retry")
    project.mark_notification_delivery_request_queued(identity, last_error="retry")
    assert project.claim_notification_delivery_request(identity, job_id=42) is None
    assert dict(project.notification_delivery_request(identity)) == saved


@pytest.mark.parametrize("status", ["queued", "processing", "typo"])
def test_terminalization_refuses_nonterminal_or_unknown_status(
    delivery_request, status
):
    project, identity = delivery_request
    with pytest.raises(ValueError, match="terminal notification"):
        project.terminalize_notification_delivery_request(identity, status=status)
    assert project.notification_delivery_request(identity)["status"] == "queued"


@pytest.mark.parametrize(
    "path", ["sent", "failed", "skipped", "reconcile", "credential", "recovery"]
)
def test_stale_terminal_worker_records_no_attempt_or_health_failure(
    delivery_request, monkeypatch, path
):
    from frisket.engine.jobs import notifications_delivery as worker
    from frisket.server.notifications.delivery import DeliveryProviderResult

    project, identity = delivery_request
    project.claim_notification_delivery_request(identity, job_id=42)
    stale = project.notification_delivery_request(identity)
    channel = project.notification_channel(stale["channel_id"])
    project.terminalize_notification_delivery_request(
        identity, status="sent", provider_ref="already-delivered"
    )
    health = []
    monkeypatch.setattr(
        worker,
        "_emit_delivery_health_notification",
        lambda *args, **kwargs: health.append(kwargs),
    )
    args = dict(
        project=project, project_id="deliveries", request=stale, channel=channel
    )
    if path == "reconcile":
        result = worker._require_reconciliation(**args)
    elif path == "credential":
        result = worker._fail_request_credential_free(
            **args, error="stale credential failure"
        )
    elif path == "recovery":
        args.pop("channel")
        result = worker._terminalize_recovered_request(
            **args, default_status="failed", error="stale job failure"
        )
    else:
        result = worker._apply_terminal_result(
            **args, result=DeliveryProviderResult(status=path, error="stale result")
        )
    if isinstance(result, dict):
        assert result["status"] == "sent"
    assert (
        project.notification_delivery_request(identity)["provider_ref"]
        == "already-delivered"
    )
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM notification_delivery_attempts"
        ).fetchone()[0]
        == 0
    )
    assert health == []


@pytest.mark.parametrize("terminal", ["empty", "composed"])
def test_late_digest_writer_preserves_finished_composition(delivery_request, terminal):
    project, identity = delivery_request
    channel = project.notification_delivery_request(identity)["channel_id"]
    route = project.create_notification_route(
        name="Digest",
        channel_id=channel,
        delivery_mode="digest",
        digest_cadence="daily",
    )
    digest = project.create_notification_digest_run(
        route_id=route["id"],
        channel_id=channel,
        cadence="daily",
        window_key="one-window",
        window_start_at="2026-11-14 00:00:00",
        window_end_at="2026-11-15 00:00:00",
        status="composing",
    )
    saved = project.set_notification_digest_run_result(
        digest["id"],
        status=terminal,
        item_count=0 if terminal == "empty" else 1,
        delivery_request_id=identity if terminal == "composed" else None,
    )
    for late in ("empty", "composed"):
        assert (
            project.set_notification_digest_run_result(
                digest["id"],
                status=late,
                item_count=7,
                delivery_request_id=identity,
            )
            == saved
        )
    with pytest.raises(ValueError, match="digest"):
        project.set_notification_digest_run_result(
            digest["id"], status="composing", item_count=0
        )


def test_attempt_insert_failure_rolls_back_terminal_state(delivery_request):
    import sqlite3
    from frisket.engine.jobs.notifications_delivery import _apply_terminal_result
    from frisket.server.notifications.delivery import DeliveryProviderResult

    project, identity = delivery_request
    project.claim_notification_delivery_request(identity, job_id=42)
    request = project.notification_delivery_request(identity)
    project.db.execute(
        "CREATE TRIGGER fail_attempt BEFORE INSERT ON notification_delivery_attempts "
        "BEGIN SELECT RAISE(ABORT,'attempt storage failed'); END"
    )
    project.db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="attempt storage failed"):
        _apply_terminal_result(
            project,
            project_id="deliveries",
            request=request,
            channel=project.notification_channel(request["channel_id"]),
            result=DeliveryProviderResult(status="sent", provider_ref="known"),
        )
    assert project.notification_delivery_request(identity)["status"] == "processing"
    assert not project.db.in_transaction


@pytest.mark.parametrize("unknown", [False, True])
def test_late_transient_provider_response_does_not_append_retry_outcome(
    tmp_path, make_project, unknown
):
    from types import SimpleNamespace
    from frisket.engine.jobs.notifications_delivery import deliver_notification_request
    from frisket.server.notifications.delivery import (
        DeliveryProviderResult,
        NotificationDeliveryRuntime,
    )
    from frisket.server.notifications.secrets import StaticNotificationSecretResolver

    project = make_project("late")
    channel = project.create_notification_channel(
        kind="email",
        name="Test",
        config={"to": "test@example.com"},
        secret_ref="env:RESEND_API_KEY",
    )
    request = project.create_notification_delivery_request(
        route_id=None,
        channel_id=channel["id"],
        delivery_kind="test",
        dedupe_key="late",
    )

    def send(message, *, channel):
        project.terminalize_notification_delivery_request(
            request["id"], status="sent", provider_ref="winner"
        )
        return DeliveryProviderResult(
            status="failed",
            error="late failure",
            transient=True,
            outcome_unknown=unknown,
        )

    result = deliver_notification_request(
        workspace_root=tmp_path,
        project_id="late",
        request_id=request["id"],
        job_id=None,
        delivery_runtime=NotificationDeliveryRuntime(
            provider_factory=lambda _: SimpleNamespace(send=send),
            secret_resolver=StaticNotificationSecretResolver(
                {"env:RESEND_API_KEY": "fake"}
            ),
        ),
    )
    assert result == {"request_id": request["id"], "status": "sent"}
    assert project.notification_delivery_request(request["id"])["last_error"] is None
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM notification_delivery_attempts"
        ).fetchone()[0]
        == 0
    )
