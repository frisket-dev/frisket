"""Queued notification delivery request worker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from frisket.engine.jobs.queue import JobQueue, claimed_project_location
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.worker import HandlerRegistration, HandlerRegistry
from frisket.server.notifications.delivery import (
    DeliveryProviderResult,
    NotificationDeliveryEffect,
    NotificationDeliveryEffectDecision,
    NotificationDeliveryEffectGuard,
    NotificationDeliveryRuntime,
    NotificationDeliveryRuntimeFactory,
    NotificationDeliveryTransientError,
    NotificationRenderedMessage,
    default_delivery_runtime,
    redact_delivery_provider_result,
)
from frisket.server.notifications.producers import (
    delivery_health_notification_candidate,
)
from frisket.server.notifications.service import NotificationEmitResult
from frisket.project_identity import ProjectStorageKey
from frisket.engine.store import Project
from frisket.engine.store.effect_checkpoints import (
    AmbiguousReserved,
    EffectCheckpointStore,
)

from .project_opener import (
    ProjectOpener,
    claimed_opener_key,
    require_opener_storage_org_id,
)


NOTIFICATION_DELIVER_KIND = "notification.deliver"
TERMINAL_REQUEST_STATUSES = {
    "sent",
    "failed",
    "skipped",
    "cancelled",
    "reconciliation_required",
}
_EFFECT_FAMILY = "notification_delivery"
_RECONCILIATION_ERROR = (
    "notification provider outcome is unknown; reconciliation required"
)


def notification_delivery_dedupe_key(request_id: int) -> str:
    return f"notification.delivery_request:{int(request_id)}"


def enqueue_notification_delivery(
    queue: JobQueue,
    *,
    workspace_root: str | Path,
    project_id: str,
    storage_org_id: int | None = None,
    request_id: int,
    max_attempts: int = 3,
    project_opener: ProjectOpener | None = None,
) -> int:
    workspace = Path(workspace_root)
    bundle = workspace / f"{project_id}.frisket"
    project = (
        Project(bundle)
        if project_opener is None
        else project_opener(
            ProjectStorageKey(
                storage_org_id=require_opener_storage_org_id(storage_org_id),
                project_slug=project_id,
            ),
            bundle,
        )
    )
    try:
        request = project.notification_delivery_request(request_id)
        if request is None:
            raise ValueError("notification delivery request not found")
        if request["status"] != "queued":
            job_id = request["job_id"]
            return int(job_id) if job_id is not None else 0
        dedupe_key = notification_delivery_dedupe_key(request_id)
        existing = queue.find_job_by_refs(
            NOTIFICATION_DELIVER_KIND,
            statuses=("queued", "running"),
            project_id=project_id,
            storage_org_id=storage_org_id,
            dedupe_key=dedupe_key,
        )
        if existing is not None:
            project.set_notification_delivery_request_job_id(request_id, existing.id)
            return existing.id
        job_id = queue.enqueue(
            NOTIFICATION_DELIVER_KIND,
            {
                "workspace_root": str(workspace),
                "project_id": project_id,
                **(
                    {"storage_org_id": storage_org_id}
                    if storage_org_id is not None
                    else {}
                ),
                "request_id": int(request_id),
                "dedupe_key": dedupe_key,
            },
            max_attempts=max_attempts,
        )
        project.set_notification_delivery_request_job_id(request_id, job_id)
        return int(job_id)
    finally:
        project.close()


def enqueue_notification_emit_result(
    queue: JobQueue,
    *,
    workspace_root: str | Path,
    project_id: str,
    result: NotificationEmitResult | None,
    storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> list[int]:
    """Turn an emitted notification's durable outbox rows into queue jobs.

    ``emit_notification_candidate`` deliberately commits notification items and
    delivery requests in the project store; this is the single bridge from
    those planned requests to the shared worker queue. Keeping the bridge next
    to ``enqueue_notification_delivery`` lets HTTP and background producers use
    identical dedupe, storage-identity, and retry behavior.
    """
    if result is None:
        return []
    job_ids: list[int] = []
    for request in result["planned_delivery_requests"]:
        job_ids.append(
            enqueue_notification_delivery(
                queue,
                workspace_root=workspace_root,
                project_id=project_id,
                storage_org_id=storage_org_id,
                request_id=int(request["id"]),
                project_opener=project_opener,
            )
        )
    return job_ids


def register_notification_handlers(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    delivery_runtime: NotificationDeliveryRuntime | None = None,
    delivery_runtime_factory: NotificationDeliveryRuntimeFactory | None = None,
    queue: JobQueue | None = None,
    storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    if delivery_runtime is not None and delivery_runtime_factory is not None:
        raise ValueError(
            "notification delivery accepts either a runtime or a runtime factory"
        )
    if delivery_runtime_factory is not None and project_opener is None:
        raise ValueError(
            "notification delivery runtime factories require a claimed project opener"
        )
    process_runtime = (
        delivery_runtime or default_delivery_runtime()
        if delivery_runtime_factory is None
        else None
    )
    workspace = Path(workspace_root)

    def _handle(payload: dict[str, Any], _context: JobHandlerContext) -> dict[str, Any]:
        if project_opener is None:
            # Local/team tier: the trusted operator's own worker, so the
            # payload path is theirs to choose. Unchanged direct open.
            handler_workspace_root: str | Path = (
                payload.get("workspace_root") or workspace
            )
            handler_project_id = str(payload["project_id"])
            handler_storage_org_id = payload.get("storage_org_id", storage_org_id)
            project_key = None
        else:
            # With an injected opener, the hosted composition derives the
            # project location only from the claimed queue
            # identity (reconstructed by the worker from trusted queue columns),
            # never the caller-controlled payload workspace_root/project_id.
            # claimed_opener_key + claimed_project_location(require_storage_
            # identity=True) FAIL CLOSED when no claimed key is present rather
            # than falling back to the payload-derived path.
            claimed = claimed_opener_key(payload)
            handler_project_id, handler_project_root, _bundle = (
                claimed_project_location(
                    payload,
                    workspace_root=workspace,
                    require_storage_identity=True,
                    workspace_root_storage_org_id=storage_org_id,
                )
            )
            handler_workspace_root = handler_project_root
            handler_storage_org_id = claimed.storage_org_id
            project_key = claimed
        try:
            runtime = (
                delivery_runtime_factory(project_key)
                if delivery_runtime_factory is not None and project_key is not None
                else process_runtime
            )
        except Exception:  # noqa: BLE001 - factory details may contain tenant data
            raise NotificationDeliveryTransientError(
                "notification delivery runtime unavailable"
            ) from None
        if not isinstance(runtime, NotificationDeliveryRuntime):
            raise TypeError(
                "notification delivery runtime factory must return "
                "NotificationDeliveryRuntime"
            )
        return deliver_notification_request(
            workspace_root=handler_workspace_root,
            project_id=handler_project_id,
            request_id=int(payload["request_id"]),
            job_id=int(payload["job_id"])
            if payload.get("job_id") is not None
            else None,
            delivery_runtime=runtime,
            # Worker-written attempt-finality: the claim
            # already knows whether this is the last attempt; re-reading the
            # job row here raced recovery/requeue.
            final_attempt=bool(payload.get("job_final_attempt")),
            storage_org_id=handler_storage_org_id,
            project_key=project_key,
            project_opener=project_opener,
        )

    registration = registry.add(
        NOTIFICATION_DELIVER_KIND,
        _handle,
        origin="frisket.production.notification_delivery",
    )
    registry.register_recovery_hook(
        lambda job_queue: reconcile_notification_delivery_requests(
            job_queue,
            workspace_root=workspace,
            storage_org_id=storage_org_id,
            project_opener=project_opener,
        )
    )
    return registration


def deliver_notification_request(
    *,
    workspace_root: str | Path,
    project_id: str,
    request_id: int,
    job_id: int | None,
    delivery_runtime: NotificationDeliveryRuntime,
    final_attempt: bool = False,
    storage_org_id: int | None = None,
    project_key: ProjectStorageKey | None = None,
    project_opener: ProjectOpener | None = None,
) -> dict[str, Any]:
    if project_key is None and storage_org_id is not None:
        project_key = ProjectStorageKey(
            storage_org_id=require_opener_storage_org_id(storage_org_id),
            project_slug=project_id,
        )
    bundle = Path(workspace_root) / f"{project_id}.frisket"
    project = (
        Project(bundle)
        if project_opener is None
        else project_opener(
            ProjectStorageKey(
                storage_org_id=require_opener_storage_org_id(storage_org_id),
                project_slug=project_id,
            ),
            bundle,
        )
    )
    try:
        request = project.notification_delivery_request(request_id)
        if request is None:
            raise ValueError("notification delivery request not found")
        if request["status"] in TERMINAL_REQUEST_STATUSES:
            return {"request_id": request_id, "status": request["status"]}
        claimed = project.claim_notification_delivery_request(request_id, job_id=job_id)
        if claimed is None:
            current = project.public_notification_delivery_request(request_id)
            return {"request_id": request_id, "status": current["status"]}
        request = project.notification_delivery_request(request_id)
        if request is None:
            raise ValueError("notification delivery request not found")
        channel = project.notification_channel(int(request["channel_id"]))
        if channel is None:
            project.terminalize_notification_delivery_request(
                request_id,
                status="failed",
                last_error="notification channel not found",
            )
            _emit_delivery_health_notification(
                project,
                project_id=project_id,
                request=request,
                status="failed",
                error="notification channel not found",
            )
            return {"request_id": request_id, "status": "failed"}
        if not bool(channel["enabled"]):
            return _skip_request(
                project,
                request=request,
                channel=channel,
                error="channel disabled",
            )
        channel_kind = str(channel["kind"])
        if channel_kind in {"email", "slack", "webhook"} and not channel["secret_ref"]:
            secret_label = "URL secret" if channel_kind == "webhook" else "secret"
            return _skip_request(
                project,
                request=request,
                channel=channel,
                error=f"{channel_kind} channel missing {secret_label} reference",
            )
        secret_value = None
        if channel["secret_ref"]:
            try:
                secret_value = delivery_runtime.secret_resolver.resolve(
                    str(channel["secret_ref"]),
                    project_id=project_id,
                )
            except Exception:  # noqa: BLE001 - resolver details may contain secrets
                return _fail_request_credential_free(
                    project,
                    project_id=project_id,
                    request=request,
                    channel=channel,
                    error="notification secret resolution failed",
                )
            if secret_value is None:
                return _skip_request(
                    project,
                    request=request,
                    channel=channel,
                    error=f"{channel_kind} channel secret unavailable",
                )
        signing_secret_value = None
        if channel_kind == "webhook":
            config = _loads_object(channel["config_json"])
            signing_ref = str(config.get("signing_secret_ref") or "").strip()
            if not signing_ref:
                return _skip_request(
                    project,
                    request=request,
                    channel=channel,
                    error="webhook channel missing signing secret reference",
                )
            try:
                signing_secret_value = delivery_runtime.secret_resolver.resolve(
                    signing_ref,
                    project_id=project_id,
                )
            except Exception:  # noqa: BLE001 - resolver details may contain secrets
                return _fail_request_credential_free(
                    project,
                    project_id=project_id,
                    request=request,
                    channel=channel,
                    error="notification secret resolution failed",
                )
            if signing_secret_value is None:
                return _skip_request(
                    project,
                    request=request,
                    channel=channel,
                    error="webhook channel signing secret unavailable",
                )
        effect = _notification_delivery_effect(
            project_key=project_key,
            project_id=project_id,
            request=request,
            channel_kind=channel_kind,
        )
        message = _render_message(
            project,
            project_id=project_id,
            request=request,
            provider_idempotency_key=effect.provider_idempotency_key,
        )
        channel_payload = _channel_payload(
            channel,
            secret_value=secret_value,
            signing_secret_value=signing_secret_value,
        )
        try:
            provider = delivery_runtime.provider_factory(channel_payload)
        except Exception:  # noqa: BLE001 - factory details may contain secrets
            return _fail_request_credential_free(
                project,
                project_id=project_id,
                request=request,
                channel=channel,
                error="notification provider construction failed",
            )
        effect_guard: NotificationDeliveryEffectGuard = (
            delivery_runtime.effect_guard or _ProjectNotificationEffectGuard(project)
        )
        try:
            decision = effect_guard.reserve(effect)
        except Exception:  # noqa: BLE001 - guard details may contain tenant data
            raise NotificationDeliveryTransientError(
                "notification effect reservation unavailable"
            ) from None
        if decision.status == "completed":
            if decision.result is None:
                return _require_reconciliation(
                    project,
                    project_id=project_id,
                    request=request,
                    channel=channel,
                )
            result = decision.result
        elif decision.status == "reconciliation_required":
            return _require_reconciliation(
                project,
                project_id=project_id,
                request=request,
                channel=channel,
            )
        elif decision.status != "send":
            return _require_reconciliation(
                project,
                project_id=project_id,
                request=request,
                channel=channel,
            )
        else:
            try:
                result = provider.send(message, channel=channel_payload)
            except Exception:  # noqa: BLE001 - provider exceptions may contain secrets
                result = DeliveryProviderResult(
                    status="failed",
                    error=_RECONCILIATION_ERROR,
                    transient=True,
                    outcome_unknown=True,
                )
        result = redact_delivery_provider_result(
            result,
            secret_values=(secret_value, signing_secret_value),
        )
        if result.outcome_unknown:
            if effect.provider_idempotency_key is None:
                return _require_reconciliation(
                    project,
                    project_id=project_id,
                    request=request,
                    channel=channel,
                )
            if final_attempt:
                return _require_reconciliation(
                    project,
                    project_id=project_id,
                    request=request,
                    channel=channel,
                )
            _record_attempt(project, request=request, channel=channel, result=result)
            project.record_notification_delivery_request_error(
                request_id,
                last_error="notification provider outcome unknown; retry is "
                "protected by provider idempotency",
            )
            raise NotificationDeliveryTransientError(
                "notification provider outcome unknown; retry is protected by "
                "provider idempotency"
            )
        if decision.status == "send":
            try:
                effect_guard.finish(effect, result)
            except Exception:  # noqa: BLE001 - provider may have succeeded already
                return _require_reconciliation(
                    project,
                    project_id=project_id,
                    request=request,
                    channel=channel,
                )
        if result.transient:
            _record_attempt(project, request=request, channel=channel, result=result)
            project.record_notification_delivery_request_error(
                request_id,
                last_error=result.error or "transient notification delivery error",
            )
            if final_attempt:
                project.terminalize_notification_delivery_request(
                    request_id,
                    status="failed",
                    last_error=result.error
                    or "notification delivery retries exhausted",
                )
                _emit_delivery_health_notification(
                    project,
                    project_id=project_id,
                    request=request,
                    status="failed",
                    error=result.error or "notification delivery retries exhausted",
                )
            raise NotificationDeliveryTransientError(
                result.error or "transient notification delivery error"
            )
        _apply_terminal_result(
            project,
            project_id=project_id,
            request=request,
            channel=channel,
            result=result,
        )
        terminal = project.public_notification_delivery_request(request_id)
        return {"request_id": request_id, "status": terminal["status"]}
    finally:
        project.close()


class _ProjectNotificationEffectGuard:
    """Local/team durable effect lifecycle in the project bundle."""

    def __init__(self, project: Project) -> None:
        self._store = EffectCheckpointStore(project.db)

    def reserve(
        self, effect: NotificationDeliveryEffect
    ) -> NotificationDeliveryEffectDecision:
        fields = _effect_checkpoint_fields(effect)
        if self._store.reserve(
            effect.effect_id,
            **fields,
            claimless_direct_effect=True,
        ):
            return NotificationDeliveryEffectDecision(status="send")
        try:
            checkpoint = self._store.returned_for_replay(**fields)
        except AmbiguousReserved:
            if effect.provider_idempotency_key is not None:
                return NotificationDeliveryEffectDecision(status="send")
            return NotificationDeliveryEffectDecision(status="reconciliation_required")
        payload = checkpoint.get("payload")
        result = _delivery_result_from_payload(payload)
        if result is None:
            return NotificationDeliveryEffectDecision(status="reconciliation_required")
        return NotificationDeliveryEffectDecision(status="completed", result=result)

    def finish(
        self,
        effect: NotificationDeliveryEffect,
        result: DeliveryProviderResult,
    ) -> None:
        fields = _effect_checkpoint_fields(effect)
        self._store.complete(
            effect.effect_id,
            **fields,
            payload=_delivery_result_payload(result),
            claimless_direct_effect=True,
        )
        if result.transient:
            self._store.consume_and_retire(
                effect.effect_id,
                **fields,
                claimless_direct_effect=True,
            )


def _effect_checkpoint_fields(effect: NotificationDeliveryEffect) -> dict[str, str]:
    identity = json.dumps(
        {
            "effect_id": effect.effect_id,
            "project_key": (
                {
                    "storage_org_id": effect.project_key.storage_org_id,
                    "project_slug": effect.project_key.project_slug,
                }
                if effect.project_key is not None
                else None
            ),
            "delivery_request_id": effect.delivery_request_id,
            "provider_kind": effect.provider_kind,
            "provider_idempotency_key": effect.provider_idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "family": _EFFECT_FAMILY,
        "group_key": effect.effect_id,
        "unit_key": str(effect.delivery_request_id),
        "action_kind": effect.provider_kind,
        "identity": identity,
    }


def _delivery_result_payload(result: DeliveryProviderResult) -> dict[str, Any]:
    return {
        "schema_version": "frisket.notification_delivery_effect.v1",
        "status": result.status,
        "provider_ref": result.provider_ref,
        "retry_after_seconds": result.retry_after_seconds,
        "error": result.error,
        "response_meta": result.response_meta,
        "transient": result.transient,
        "outcome_unknown": result.outcome_unknown,
    }


def _delivery_result_from_payload(payload: Any) -> DeliveryProviderResult | None:
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        "frisket.notification_delivery_effect.v1"
    ):
        return None
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        return None
    response_meta = payload.get("response_meta")
    return DeliveryProviderResult(
        status=status,
        provider_ref=(
            str(payload["provider_ref"])
            if payload.get("provider_ref") is not None
            else None
        ),
        retry_after_seconds=(
            float(payload["retry_after_seconds"])
            if payload.get("retry_after_seconds") is not None
            else None
        ),
        error=str(payload["error"]) if payload.get("error") is not None else None,
        response_meta=response_meta if isinstance(response_meta, dict) else {},
        transient=bool(payload.get("transient")),
        outcome_unknown=bool(payload.get("outcome_unknown")),
    )


def _notification_delivery_effect(
    *,
    project_key: ProjectStorageKey | None,
    project_id: str,
    request: Any,
    channel_kind: str,
) -> NotificationDeliveryEffect:
    identity = json.dumps(
        {
            "storage_org_id": (
                project_key.storage_org_id if project_key is not None else None
            ),
            "project_slug": (
                project_key.project_slug if project_key is not None else project_id
            ),
            "delivery_request_id": int(request["id"]),
            "dedupe_key": str(request["dedupe_key"]),
            "channel_id": int(request["channel_id"]),
            "provider_kind": channel_kind,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return NotificationDeliveryEffect(
        effect_id=f"notification-delivery:{digest}",
        project_key=project_key,
        delivery_request_id=int(request["id"]),
        provider_kind=channel_kind,
        provider_idempotency_key=(
            f"frisket-notification-{digest}" if channel_kind == "email" else None
        ),
    )


def _require_reconciliation(
    project: Project,
    *,
    project_id: str,
    request: Any,
    channel: Any,
) -> dict[str, Any]:
    result = DeliveryProviderResult(
        status="reconciliation_required",
        error=_RECONCILIATION_ERROR,
        outcome_unknown=True,
    )
    _record_attempt(project, request=request, channel=channel, result=result)
    project.terminalize_notification_delivery_request(
        int(request["id"]),
        status="reconciliation_required",
        last_error=_RECONCILIATION_ERROR,
    )
    _emit_delivery_health_notification(
        project,
        project_id=project_id,
        request=request,
        status="failed",
        error=_RECONCILIATION_ERROR,
    )
    return {
        "request_id": int(request["id"]),
        "status": "reconciliation_required",
    }


def _fail_request_credential_free(
    project: Project,
    *,
    project_id: str,
    request: Any,
    channel: Any,
    error: str,
) -> dict[str, Any]:
    result = DeliveryProviderResult(status="failed", error=error)
    _record_attempt(project, request=request, channel=channel, result=result)
    project.terminalize_notification_delivery_request(
        int(request["id"]),
        status="failed",
        last_error=error,
    )
    _emit_delivery_health_notification(
        project,
        project_id=project_id,
        request=request,
        status="failed",
        error=error,
    )
    return {"request_id": int(request["id"]), "status": "failed"}


def _apply_terminal_result(
    project: Project,
    *,
    project_id: str,
    request: Any,
    channel: Any,
    result: DeliveryProviderResult,
) -> None:
    _record_attempt(project, request=request, channel=channel, result=result)
    request_id = int(request["id"])
    if result.status == "sent":
        project.terminalize_notification_delivery_request(
            request_id,
            status="sent",
            provider_ref=result.provider_ref,
        )
        return
    if result.status == "skipped":
        project.terminalize_notification_delivery_request(
            request_id,
            status="skipped",
            last_error=result.error,
            provider_ref=result.provider_ref,
        )
        return
    error = (
        result.error
        if result.status == "failed"
        else "unsupported notification provider result"
    ) or "notification delivery failed"
    project.terminalize_notification_delivery_request(
        request_id,
        status="failed",
        last_error=error,
        provider_ref=result.provider_ref,
    )
    _emit_delivery_health_notification(
        project,
        project_id=project_id,
        request=request,
        status="failed",
        error=error,
    )


def _recovery_targets(
    workspace: Path,
    *,
    project_id: str | None,
    storage_org_id: int | None,
    project_opener: ProjectOpener | None,
) -> list[tuple[ProjectStorageKey | None, Path]]:
    """Bundles this recovery pass may open, each with its storage identity.

    Without an opener this keeps the historical flat scan of ``workspace``
    and carries no identity. With an opener every open must be keyed, so the
    identity has to come from somewhere trustworthy: either the caller's
    DECLARED ``storage_org_id`` (a per-org root), or — for the global hosted
    recovery root, which is ``<data_dir>/projects`` and not pre-selected per
    org — the per-org subdirectory layout that root is defined to have.
    Directories that are not a positive integer org id are not part of that
    layout and are skipped rather than opened under a guessed key.
    """
    if project_opener is None:
        flat = (
            [workspace / f"{project_id}.frisket"]
            if project_id is not None
            else sorted(workspace.glob("*.frisket"))
        )
        return [(None, path) for path in flat]
    if storage_org_id is not None:
        org_id = require_opener_storage_org_id(storage_org_id)
        paths = (
            [workspace / f"{project_id}.frisket"]
            if project_id is not None
            else sorted(workspace.glob("*.frisket"))
        )
        return [
            (ProjectStorageKey(storage_org_id=org_id, project_slug=path.stem), path)
            for path in paths
        ]
    targets: list[tuple[ProjectStorageKey | None, Path]] = []
    for org_dir in sorted(workspace.iterdir()) if workspace.exists() else []:
        if not org_dir.is_dir():
            continue
        try:
            org_id = require_opener_storage_org_id(int(org_dir.name))
        except ValueError:
            continue
        for path in sorted(org_dir.glob("*.frisket")):
            if project_id is not None and path.stem != project_id:
                continue
            targets.append(
                (ProjectStorageKey(storage_org_id=org_id, project_slug=path.stem), path)
            )
    return targets


def reconcile_notification_delivery_requests(
    queue: JobQueue,
    *,
    workspace_root: str | Path,
    project_id: str | None = None,
    storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> int:
    workspace = Path(workspace_root)
    targets = _recovery_targets(
        workspace,
        project_id=project_id,
        storage_org_id=storage_org_id,
        project_opener=project_opener,
    )
    reconciled = 0
    for storage_key, project_path in targets:
        if not (project_path / "project.db").exists():
            continue
        project = (
            Project(project_path)
            if project_opener is None
            else project_opener(storage_key, project_path)
        )
        try:
            for request in project.processing_notification_delivery_requests():
                job = queue.get(int(request["job_id"]))
                if job is None:
                    _terminalize_recovered_request(
                        project,
                        project_id=project_path.stem,
                        request=request,
                        default_status="failed",
                        error="notification delivery job missing",
                    )
                    reconciled += 1
                elif job.status == "queued":
                    project.mark_notification_delivery_request_queued(
                        int(request["id"]),
                        last_error=job.error,
                    )
                    reconciled += 1
                elif job.status == "failed":
                    _terminalize_recovered_request(
                        project,
                        project_id=project_path.stem,
                        request=request,
                        default_status="failed",
                        error=job.error or request["last_error"],
                    )
                    reconciled += 1
                elif job.status == "cancelled":
                    _terminalize_recovered_request(
                        project,
                        project_id=project_path.stem,
                        request=request,
                        default_status="cancelled",
                        error=job.error or "notification delivery job cancelled",
                    )
                    reconciled += 1
        finally:
            project.close()
    return reconciled


def _terminalize_recovered_request(
    project: Project,
    *,
    project_id: str,
    request: Any,
    default_status: str,
    error: str | None,
) -> None:
    ambiguous = project.db.execute(
        "SELECT 1 FROM effect_checkpoints WHERE family=? AND unit_key=? "
        "AND state='reserved' LIMIT 1",
        (_EFFECT_FAMILY, str(request["id"])),
    ).fetchone()
    status = "reconciliation_required" if ambiguous is not None else default_status
    public_error = _RECONCILIATION_ERROR if ambiguous is not None else error
    project.terminalize_notification_delivery_request(
        int(request["id"]),
        status=status,
        last_error=public_error,
    )
    _emit_delivery_health_notification(
        project,
        project_id=project_id,
        request=request,
        status="failed" if status == "reconciliation_required" else status,
        error=public_error,
    )


def _skip_request(
    project: Project,
    *,
    request: Any,
    channel: Any,
    error: str,
) -> dict[str, Any]:
    result = DeliveryProviderResult(status="skipped", error=error)
    _record_attempt(project, request=request, channel=channel, result=result)
    project.terminalize_notification_delivery_request(
        int(request["id"]),
        status="skipped",
        last_error=error,
    )
    return {"request_id": int(request["id"]), "status": "skipped"}


def _record_attempt(
    project: Project,
    *,
    request: Any,
    channel: Any,
    result: DeliveryProviderResult,
) -> None:
    notification_id = request["notification_id"]
    project.record_notification_delivery_attempt(
        delivery_request_id=int(request["id"]),
        notification_id=int(notification_id) if notification_id is not None else None,
        channel_id=int(channel["id"]),
        status=result.status,
        provider_ref=result.provider_ref,
        error=result.error,
        response_meta=result.response_meta,
    )


def _emit_delivery_health_notification(
    project: Project,
    *,
    project_id: str,
    request: Any,
    status: str,
    error: str | None,
) -> None:
    candidate = delivery_health_notification_candidate(
        project_id=project_id,
        request_id=int(request["id"]),
        status=status,
        route_id=int(request["route_id"]) if request["route_id"] is not None else None,
        channel_id=(
            int(request["channel_id"]) if request["channel_id"] is not None else None
        ),
        notification_id=(
            int(request["notification_id"])
            if request["notification_id"] is not None
            else None
        ),
        job_id=int(request["job_id"]) if request["job_id"] is not None else None,
        error=error,
    )
    if candidate is None:
        return
    try:
        from frisket.server.notifications.service import emit_notification_candidate

        emit_notification_candidate(project, candidate)
    except Exception:  # noqa: BLE001 - delivery status must remain terminal
        return


def _render_message(
    project: Project,
    *,
    project_id: str,
    request: Any,
    provider_idempotency_key: str | None = None,
) -> NotificationRenderedMessage:
    if request["digest_run_id"] is not None:
        from frisket.server.notifications.digests import (
            render_notification_digest_message,
        )

        return replace(
            render_notification_digest_message(
                project,
                project_id=project_id,
                request=request,
            ),
            provider_idempotency_key=provider_idempotency_key,
        )
    item = (
        project.notification_item(int(request["notification_id"]))
        if request["notification_id"] is not None
        else None
    )
    if item is not None:
        title = str(item["title"])
        summary = str(item["summary"])
        source_kind = str(item["source_kind"])
        severity = str(item["severity"])
        deep_link = _loads_object(item["deep_link"])
    else:
        title = "Test notification"
        summary = "This is a notification route test."
        source_kind = None
        severity = None
        deep_link = {}
    return NotificationRenderedMessage(
        project_id=project_id,
        delivery_request_id=int(request["id"]),
        route_id=int(request["route_id"]) if request["route_id"] is not None else None,
        channel_id=int(request["channel_id"]),
        delivery_kind=str(request["delivery_kind"]),
        title=title,
        summary=summary,
        source_kind=source_kind,
        severity=severity,
        notification_id=(
            int(request["notification_id"])
            if request["notification_id"] is not None
            else None
        ),
        digest_run_id=(
            int(request["digest_run_id"])
            if request["digest_run_id"] is not None
            else None
        ),
        deep_link=deep_link,
        provider_idempotency_key=provider_idempotency_key,
    )


def _channel_payload(
    row: Any,
    *,
    secret_value: str | None = None,
    signing_secret_value: str | None = None,
) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "config": _loads_object(row["config_json"]),
        "secret_ref": row["secret_ref"],
        "secret_value": secret_value,
        "signing_secret_value": signing_secret_value,
    }


def _loads_object(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
