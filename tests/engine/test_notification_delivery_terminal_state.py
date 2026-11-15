"""A delayed retry or reconciler cannot rewrite a finished delivery."""

import pytest

from frisket.engine.store import Project
from frisket.engine.store.notifications import TERMINAL_REQUEST_STATUSES


@pytest.fixture
def delivery_request(tmp_path):
    project = Project.create(tmp_path / "deliveries.frisket")
    try:
        channel = project.create_notification_channel(kind="in_app", name="In app")
        delivery = project.create_notification_delivery_request(
            route_id=None,
            channel_id=channel["id"],
            delivery_kind="immediate",
            dedupe_key="one-delivery",
        )
        yield project, delivery["id"]
    finally:
        project.close()


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
