"""Tests for the explicit Project notification channel resolver seam."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.engine.jobs.notifications_delivery import (
    deliver_notification_request,
)
from frisket.engine.store import Project
from frisket.engine.store import notifications as store_notifications
from frisket.engine.store.notifications import require_notification_channel
from frisket.notifications.delivery import (
    DeliveryProviderResult,
    NotificationDeliveryRuntime,
    NotificationRenderedMessage,
)


class _SecretMarker:
    def __str__(self) -> str:
        return "platform:test-email"


_MARKER = _SecretMarker()


def _attach_email_marker(row: Any) -> Any:
    if row is None or row["secret_ref"] is not None or str(row["kind"]) != "email":
        return row
    channel = dict(row)
    channel["secret_ref"] = _MARKER
    return channel


def test_attached_channel_resolver_honored_by_direct_and_facade_reads(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "resolver.frisket"
    project = Project.create(bundle, name="Resolver")
    try:
        created = project.create_notification_channel(
            kind="email",
            name="Hosted alerts",
            config={"owner_kind": "user", "owner_ref": "user:1"},
            secret_ref=None,
        )
        channel_id = int(created["id"])
        assert created["has_secret"] is False

        project.set_notification_channel_resolver(_attach_email_marker)

        direct = project.notification_channel(channel_id)
        assert direct is not None
        assert direct["secret_ref"] is _MARKER

        required = require_notification_channel(project, channel_id)
        assert required["secret_ref"] is _MARKER

        public = project.public_notification_channel(channel_id)
        assert public["has_secret"] is True

        listed = {int(row["id"]): row for row in project.notification_channels()}
        assert listed[channel_id]["secret_ref"] is _MARKER

        updated = project.update_notification_channel(
            channel_id, name="Hosted alerts renamed"
        )
        assert updated["name"] == "Hosted alerts renamed"
        assert updated["has_secret"] is True

        # The transient in-memory marker is never written back to SQLite.
        stored_secret = project.db.execute(
            "SELECT secret_ref FROM notification_channels WHERE id=?",
            (channel_id,),
        ).fetchone()["secret_ref"]
        assert stored_secret is None

        # Clearing the resolver restores raw stored rows.
        project.set_notification_channel_resolver(None)
        cleared = project.notification_channel(channel_id)
        assert cleared is not None
        assert cleared["secret_ref"] is None
        assert project.public_notification_channel(channel_id)["has_secret"] is False
    finally:
        project.close()


def test_project_init_accepts_notification_channel_resolver(tmp_path: Path) -> None:
    bundle = tmp_path / "init_resolver.frisket"
    created_project = Project.create(bundle, name="Init Resolver")
    try:
        channel_id = int(
            created_project.create_notification_channel(
                kind="email",
                name="Alerts",
                config={"to": "user@example.com"},
                secret_ref=None,
            )["id"]
        )
    finally:
        created_project.close()

    reopened = Project(bundle, notification_channel_resolver=_attach_email_marker)
    try:
        channel = reopened.notification_channel(channel_id)
        assert channel is not None
        assert channel["secret_ref"] is _MARKER
        assert reopened.public_notification_channel(channel_id)["has_secret"] is True
    finally:
        reopened.close()


def test_delivery_only_resolver_is_claim_scoped_and_consumes_on_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "delivery_only.frisket"
    project = Project.create(bundle, name="Delivery Only")
    try:
        channel_id = int(
            project.create_notification_channel(
                kind="email",
                name="Hosted alerts",
                config={"to": "owner@example.com"},
                secret_ref=None,
            )["id"]
        )
        request = project.create_notification_delivery_request(
            route_id=None,
            channel_id=channel_id,
            delivery_kind="test",
            dedupe_key="resolver-claim-test",
        )
        project.set_notification_channel_resolver(
            _attach_email_marker, delivery_only=True
        )

        # Unclaimed reads never receive the transient marker.
        assert project.notification_channel(channel_id)["secret_ref"] is None
        assert project.public_notification_channel(channel_id)["has_secret"] is False
        listed = {int(row["id"]): row for row in project.notification_channels()}
        assert listed[channel_id]["secret_ref"] is None

        # A claimed read receives the exact sentinel once, then resets.
        claimed = project.claim_notification_delivery_request(int(request["id"]))
        assert claimed is not None
        first_read = project.notification_channel(channel_id)
        assert first_read is not None
        assert first_read["secret_ref"] is _MARKER
        second_read = project.notification_channel(channel_id)
        assert second_read is not None
        assert second_read["secret_ref"] is None

        # A failing DB read after a claim still consumes the transient marker.
        claimed_again = project.claim_notification_delivery_request(int(request["id"]))
        assert claimed_again is not None

        real_raw = store_notifications._raw_notification_channel
        failed_once = False

        def fail_once(proj: Any, cid: int) -> Any:
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise RuntimeError("channel read failed")
            return real_raw(proj, cid)

        monkeypatch.setattr(store_notifications, "_raw_notification_channel", fail_once)
        with pytest.raises(RuntimeError, match="channel read failed"):
            project.notification_channel(channel_id)
        after_failure = project.notification_channel(channel_id)
        assert after_failure is not None
        assert after_failure["secret_ref"] is None
    finally:
        project.close()


def test_delivery_worker_honors_delivery_only_channel_resolver(tmp_path: Path) -> None:
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    bundle = workspace_root / "hosted-proj.frisket"
    project = Project.create(bundle, name="Hosted Proj")
    try:
        channel_id = int(
            project.create_notification_channel(
                kind="email",
                name="Hosted alerts",
                config={"to": "owner@example.com"},
                secret_ref=None,
            )["id"]
        )
        route = project.create_notification_route(
            name="Alert email",
            channel_id=channel_id,
            source_kind="alert",
        )
        request = project.create_notification_delivery_request(
            route_id=int(route["id"]),
            channel_id=channel_id,
            delivery_kind="test",
            dedupe_key="worker-resolver-e2e",
        )
        request_id = int(request["id"])
    finally:
        project.close()

    observed_channels: list[dict[str, Any]] = []

    class _Provider:
        def send(
            self,
            message: NotificationRenderedMessage,
            *,
            channel: dict[str, Any],
        ) -> DeliveryProviderResult:
            observed_channels.append(channel)
            return DeliveryProviderResult(status="sent", provider_ref="resend:ok")

    class _Secrets:
        def resolve(self, secret_ref: str, *, project_id: str) -> str | None:
            return "resolved-key" if secret_ref == str(_MARKER) else None

    def opener(_key: Any, path: Path) -> Project:
        opened = Project(path)
        opened.set_notification_channel_resolver(
            _attach_email_marker, delivery_only=True
        )
        return opened

    runtime = NotificationDeliveryRuntime(
        provider_factory=lambda _channel: _Provider(),
        secret_resolver=_Secrets(),
    )
    outcome = deliver_notification_request(
        workspace_root=workspace_root,
        project_id="hosted-proj",
        request_id=request_id,
        job_id=None,
        delivery_runtime=runtime,
        storage_organization_id=1,
        project_opener=opener,
    )
    assert outcome["status"] == "sent"
    assert len(observed_channels) == 1
    assert observed_channels[0]["secret_ref"] is _MARKER
    assert observed_channels[0]["secret_value"] == "resolved-key"
