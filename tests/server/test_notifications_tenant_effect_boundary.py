from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.engine.jobs import (
    JobHandlerContext,
    reconcile_notification_delivery_requests,
    register_notification_handlers,
)
from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY
from frisket.engine.jobs.worker import HandlerRegistry
from frisket.engine.store import Project
from frisket.project_identity import ProjectStorageKey
from frisket.contracts.http.notifications import NotificationDeliveryRequest
from frisket.server.notifications.delivery import (
    DeliveryProviderResult,
    NotificationDeliveryEffect,
    NotificationDeliveryEffectDecision,
    NotificationDeliveryRuntime,
    NotificationRenderedMessage,
)
from frisket.server.notifications.secrets import StaticNotificationSecretResolver


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[NotificationRenderedMessage, dict[str, Any]]] = []

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        self.calls.append((message, channel))
        return DeliveryProviderResult(
            status="sent",
            provider_ref=f"sent:{channel['secret_value']}",
        )


class _RecordingGuard:
    def __init__(self) -> None:
        self.reserved: list[NotificationDeliveryEffect] = []
        self.finished: list[NotificationDeliveryEffect] = []

    def reserve(
        self, effect: NotificationDeliveryEffect
    ) -> NotificationDeliveryEffectDecision:
        self.reserved.append(effect)
        return NotificationDeliveryEffectDecision(status="send")

    def finish(
        self,
        effect: NotificationDeliveryEffect,
        result: DeliveryProviderResult,
    ) -> None:
        assert result.status == "sent"
        self.finished.append(effect)


def _seed_request(
    root: Path,
    key: ProjectStorageKey,
    *,
    kind: str = "email",
) -> int:
    bundle = root / str(key.storage_org_id) / f"{key.project_slug}.frisket"
    project = Project.create(bundle, name=key.project_slug)
    try:
        channel = project.create_notification_channel(
            kind=kind,
            name=f"{kind} channel",
            config={"to": "alerts@example.com"},
            secret_ref="env:DELIVERY_SECRET",
        )
        request = project.create_notification_delivery_request(
            route_id=None,
            channel_id=int(channel["id"]),
            delivery_kind="test",
            dedupe_key=f"delivery:{key.storage_org_id}",
        )
        return int(request["id"])
    finally:
        project.close()


def _opener(_key: ProjectStorageKey, path: Path) -> Project:
    return Project(path)


def _payload(key: ProjectStorageKey, request_id: int) -> dict[str, Any]:
    return {
        CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY: key,
        "project_id": key.project_slug,
        "storage_org_id": key.storage_org_id,
        "request_id": request_id,
    }


def test_runtime_factory_is_tenant_bound_for_same_slug_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "projects"
    keys = [
        ProjectStorageKey(storage_org_id=11, project_slug="same-slug"),
        ProjectStorageKey(storage_org_id=22, project_slug="same-slug"),
    ]
    request_ids = [_seed_request(root, key) for key in keys]
    provider = _RecordingProvider()
    guards = {key.storage_org_id: _RecordingGuard() for key in keys}
    factory_keys: list[ProjectStorageKey] = []
    monkeypatch.setenv("DELIVERY_SECRET", "poisoned-process-secret")

    def runtime_factory(key: ProjectStorageKey) -> NotificationDeliveryRuntime:
        factory_keys.append(key)
        return NotificationDeliveryRuntime(
            provider_factory=lambda _channel: provider,
            secret_resolver=StaticNotificationSecretResolver(
                {"env:DELIVERY_SECRET": f"tenant-{key.storage_org_id}-secret"}
            ),
            effect_guard=guards[key.storage_org_id],
        )

    registry = HandlerRegistry()
    register_notification_handlers(
        registry,
        workspace_root=root,
        delivery_runtime_factory=runtime_factory,
        project_opener=_opener,
    )
    handler = registry.get("notification.deliver")
    assert handler is not None

    for key, request_id in zip(keys, request_ids, strict=True):
        assert handler(
            _payload(key, request_id), JobHandlerContext.without_job_row()
        ) == {"request_id": request_id, "status": "sent"}

    assert factory_keys == keys
    assert [call[1]["secret_value"] for call in provider.calls] == [
        "tenant-11-secret",
        "tenant-22-secret",
    ]
    assert "poisoned-process-secret" not in json.dumps(provider.calls, default=str)
    for key in keys:
        guard = guards[key.storage_org_id]
        assert [effect.project_key for effect in guard.reserved] == [key]
        assert guard.finished == guard.reserved


def test_completed_effect_replay_makes_no_second_provider_call(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    key = ProjectStorageKey(storage_org_id=31, project_slug="replay")
    request_id = _seed_request(root, key)
    provider = _RecordingProvider()
    runtime = NotificationDeliveryRuntime(
        provider_factory=lambda _channel: provider,
        secret_resolver=StaticNotificationSecretResolver(
            {"env:DELIVERY_SECRET": "tenant-secret"}
        ),
    )
    registry = HandlerRegistry()
    register_notification_handlers(
        registry,
        workspace_root=root,
        delivery_runtime=runtime,
        project_opener=_opener,
    )
    handler = registry.get("notification.deliver")
    assert handler is not None

    handler(_payload(key, request_id), JobHandlerContext.without_job_row())
    project = Project(root / "31" / "replay.frisket")
    try:
        project.db.execute(
            "UPDATE notification_delivery_requests SET status='queued', "
            "provider_ref=NULL, sent_at=NULL WHERE id=?",
            (request_id,),
        )
        project.db.commit()
    finally:
        project.close()

    replay = handler(_payload(key, request_id), JobHandlerContext.without_job_row())
    assert replay == {"request_id": request_id, "status": "sent"}
    assert len(provider.calls) == 1


class _SimulatedProcessLoss(BaseException):
    pass


class _SentThenLostProvider:
    def __init__(self) -> None:
        self.calls = 0

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        self.calls += 1
        raise _SimulatedProcessLoss(
            "https://hooks.slack.com/services/TENANT/SECRET token=leaked"
        )


def test_lease_reclaim_refuses_ambiguous_slack_second_send(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    key = ProjectStorageKey(storage_org_id=41, project_slug="ambiguous")
    request_id = _seed_request(root, key, kind="slack")
    provider = _SentThenLostProvider()
    runtime = NotificationDeliveryRuntime(
        provider_factory=lambda _channel: provider,
        secret_resolver=StaticNotificationSecretResolver(
            {"env:DELIVERY_SECRET": ("https://hooks.slack.com/services/TENANT/SECRET")}
        ),
    )
    registry = HandlerRegistry()
    register_notification_handlers(
        registry,
        workspace_root=root,
        delivery_runtime=runtime,
        project_opener=_opener,
    )
    handler = registry.get("notification.deliver")
    assert handler is not None

    with pytest.raises(_SimulatedProcessLoss):
        handler(_payload(key, request_id), JobHandlerContext.without_job_row())
    reclaimed = handler(_payload(key, request_id), JobHandlerContext.without_job_row())

    assert reclaimed == {
        "request_id": request_id,
        "status": "reconciliation_required",
    }
    assert provider.calls == 1
    project = Project(root / "41" / "ambiguous.frisket")
    try:
        request = project.public_notification_delivery_request(request_id)
        attempt = project.db.execute(
            "SELECT status, error FROM notification_delivery_attempts "
            "WHERE delivery_request_id=?",
            (request_id,),
        ).fetchone()
        persisted = json.dumps([request, dict(attempt)], sort_keys=True, default=str)
        assert request["status"] == "reconciliation_required"
        assert NotificationDeliveryRequest.model_validate(request).status == (
            "reconciliation_required"
        )
        assert attempt["status"] == "reconciliation_required"
        assert "SECRET" not in persisted
        assert "token=leaked" not in persisted
    finally:
        project.close()


def test_exhausted_job_recovery_preserves_ambiguous_effect(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    key = ProjectStorageKey(storage_org_id=43, project_slug="exhausted")
    request_id = _seed_request(root, key, kind="slack")
    provider = _SentThenLostProvider()
    registry = HandlerRegistry()
    register_notification_handlers(
        registry,
        workspace_root=root,
        delivery_runtime=NotificationDeliveryRuntime(
            provider_factory=lambda _channel: provider,
            secret_resolver=StaticNotificationSecretResolver(
                {
                    "env:DELIVERY_SECRET": (
                        "https://hooks.slack.com/services/TENANT/SECRET"
                    )
                }
            ),
        ),
        project_opener=_opener,
    )
    handler = registry.get("notification.deliver")
    assert handler is not None
    with pytest.raises(_SimulatedProcessLoss):
        handler(_payload(key, request_id), JobHandlerContext.without_job_row())

    project = Project(root / "43" / "exhausted.frisket")
    try:
        project.db.execute(
            "UPDATE notification_delivery_requests SET job_id=99 WHERE id=?",
            (request_id,),
        )
        project.db.commit()
    finally:
        project.close()

    class _FailedQueue:
        def get(self, _job_id: int) -> Any:
            return SimpleNamespace(
                status="failed",
                error="https://hooks.slack.com/services/TENANT/SECRET token=leaked",
            )

    assert (
        reconcile_notification_delivery_requests(
            _FailedQueue(),  # type: ignore[arg-type]
            workspace_root=root,
            project_opener=_opener,
        )
        == 1
    )
    project = Project(root / "43" / "exhausted.frisket")
    try:
        request = project.public_notification_delivery_request(request_id)
        assert request["status"] == "reconciliation_required"
        assert request["last_error"] == (
            "notification provider outcome is unknown; reconciliation required"
        )
        assert "SECRET" not in json.dumps(request, sort_keys=True, default=str)
    finally:
        project.close()
    assert provider.calls == 1


class _IdempotentSentThenLostProvider:
    def __init__(self) -> None:
        self.idempotency_keys: list[str | None] = []

    def send(
        self,
        message: NotificationRenderedMessage,
        *,
        channel: dict[str, Any],
    ) -> DeliveryProviderResult:
        self.idempotency_keys.append(message.provider_idempotency_key)
        if len(self.idempotency_keys) == 1:
            raise RuntimeError("Bearer leaked-secret at https://provider.invalid/path")
        return DeliveryProviderResult(status="sent", provider_ref="resend:stable")


def test_resend_retry_reuses_the_provider_idempotency_key(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    key = ProjectStorageKey(storage_org_id=45, project_slug="idempotent")
    request_id = _seed_request(root, key)
    provider = _IdempotentSentThenLostProvider()
    registry = HandlerRegistry()
    register_notification_handlers(
        registry,
        workspace_root=root,
        delivery_runtime=NotificationDeliveryRuntime(
            provider_factory=lambda _channel: provider,
            secret_resolver=StaticNotificationSecretResolver(
                {"env:DELIVERY_SECRET": "resend-secret"}
            ),
        ),
        project_opener=_opener,
    )
    handler = registry.get("notification.deliver")
    assert handler is not None

    with pytest.raises(RuntimeError, match="provider outcome unknown"):
        handler(_payload(key, request_id), JobHandlerContext.without_job_row())
    assert handler(_payload(key, request_id), JobHandlerContext.without_job_row()) == {
        "request_id": request_id,
        "status": "sent",
    }

    assert len(provider.idempotency_keys) == 2
    assert provider.idempotency_keys[0] == provider.idempotency_keys[1]
    assert provider.idempotency_keys[0] is not None


def test_runtime_factory_error_drops_tenant_details(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    key = ProjectStorageKey(storage_org_id=47, project_slug="factory")
    request_id = _seed_request(root, key)

    def failing_factory(_key: ProjectStorageKey) -> NotificationDeliveryRuntime:
        raise RuntimeError("tenant token=SECRET https://internal.invalid/path")

    registry = HandlerRegistry()
    register_notification_handlers(
        registry,
        workspace_root=root,
        delivery_runtime_factory=failing_factory,
        project_opener=_opener,
    )
    handler = registry.get("notification.deliver")
    assert handler is not None

    with pytest.raises(RuntimeError) as error:
        handler(_payload(key, request_id), JobHandlerContext.without_job_row())
    assert str(error.value) == "notification delivery runtime unavailable"


class _LeakingResolver:
    def resolve(self, secret_ref: str, *, project_id: str) -> str | None:
        raise RuntimeError(
            "resolver failed for https://hooks.example/tenant/SECRET token=leaked"
        )


def test_resolver_exception_is_credential_free_in_public_state(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    key = ProjectStorageKey(storage_org_id=51, project_slug="resolver")
    request_id = _seed_request(root, key)
    provider = _RecordingProvider()
    registry = HandlerRegistry()
    register_notification_handlers(
        registry,
        workspace_root=root,
        delivery_runtime=NotificationDeliveryRuntime(
            provider_factory=lambda _channel: provider,
            secret_resolver=_LeakingResolver(),
        ),
        project_opener=_opener,
    )
    handler = registry.get("notification.deliver")
    assert handler is not None

    result = handler(_payload(key, request_id), JobHandlerContext.without_job_row())
    assert result == {"request_id": request_id, "status": "failed"}
    assert provider.calls == []

    project = Project(root / "51" / "resolver.frisket")
    try:
        request = project.public_notification_delivery_request(request_id)
        attempts = project.db.execute(
            "SELECT status, error, response_meta_json "
            "FROM notification_delivery_attempts WHERE delivery_request_id=?",
            (request_id,),
        ).fetchall()
        persisted = json.dumps(
            [request, *[dict(attempt) for attempt in attempts]],
            sort_keys=True,
            default=str,
        )
        assert request["last_error"] == "notification secret resolution failed"
        assert "SECRET" not in persisted
        assert "token=leaked" not in persisted
        assert "hooks.example" not in persisted
    finally:
        project.close()
