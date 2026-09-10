"""V1 action execution services for local server routes."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Mapping

from frisket.authoring.action_metadata import (
    action_available_in_edition,
    action_edition_unavailable_message,
    declared_action_capabilities,
)
from frisket.actions.system import action_id_from_request
from frisket.ai.llm import ModelRouter
from frisket.contracts.action import (
    ActionError as V1ActionError,
    ActionResult as V1ActionResult,
    Receipt as V1Receipt,
)
from frisket.credentials import missing_required_credentials
from frisket.engine.executor import BoundLocalFile, ExecutorDeps, run_action_spec
from frisket.engine.executor.action_support import _new_id
from frisket.engine.jobs import (
    ACTION_RUN_KIND,
    RUN_PROJECT_KIND,
    enqueue_enclosure_downloads,
    enqueue_source_append_refreshes,
)
from frisket.engine.jobs.projection import (
    job_project_id,
    job_run_id,
    parse_status_time,
    project_action_job_payload,
    run_id_from_inline_job_id,
    run_inline_job_payload,
)
from frisket.server.action_enqueue import (
    QueuedV1ActionRunContext,
    exact_keyless_replay_covers_action,
    missing_action_credential_error,
    missing_model_key_error_for_request,
    missing_provider_key_error,
    network_disabled_error,
    queue_v1_action_run,
)
from frisket.server.workspace import Workspace
from frisket.execution.provider import ExecutionCompositionContext
from frisket.engine.store import Project
from frisket.engine.store.receipts import FINISHED_RECEIPT_STATUSES, ReceiptStore
from frisket.engine.store.sources import SourceStore
from frisket.features.watchlists.triggers import (
    source_poll_materialization_from_receipt,
    trigger_completed_source_poll_watches,
)
from frisket.server.route_errors import RouteError


LOG = logging.getLogger("frisket.server")

# Inverse of jobs/projection.py's _RUN_STATUS_TO_JOB_STATUS: a jobs-list
# `status` filter (queue vocabulary) translated to the `runs.status` value it
# would have come from. 'queued' has no runs.status equivalent — an INLINE
# run starts executing the instant its row is created — so that filter value
# matches no run rows.
_JOB_STATUS_TO_RUN_STATUS = {
    "running": "running",
    "done": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}

# How many of the project's most recent project.run queue jobs to scan when
# building the "already covered" run-id set for the inline-run union — always
# at least this many regardless of the caller's requested page size, since a
# small `limit` must not let an old covered run leak back in as a false
# "uncovered" duplicate.
_INLINE_RUN_EXCLUSION_SCAN_LIMIT = 500


def _unqueued_run_rows(
    project: Project,
    *,
    exclude_run_ids: set[int | None],
    status: str | None,
    limit: int,
) -> list[Any]:
    """Recent `runs` rows NOT already represented by a queue job, most-recent
    first, capped at `limit` (the caller merges + re-sorts against queue jobs
    and slices to the requested page size)."""

    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        run_status = _JOB_STATUS_TO_RUN_STATUS.get(status)
        if run_status is None:
            return []
        clauses.append("status=?")
        params.append(run_status)
    covered = {run_id for run_id in exclude_run_ids if run_id is not None}
    if covered:
        placeholders = ",".join("?" for _ in covered)
        clauses.append(f"id NOT IN ({placeholders})")
        params.extend(sorted(covered))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return project.db.execute(
        f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
        params,
    ).fetchall()


def _job_payload_sort_key(payload: dict[str, Any]) -> tuple[float, int]:
    timing = payload.get("timing") or {}
    raw = (
        timing.get("started_at")
        or timing.get("created_at")
        or timing.get("finished_at")
    )
    parsed = parse_status_time(raw)
    epoch = parsed.timestamp() if parsed is not None else 0.0
    return (epoch, int(payload.get("job_id") or 0))


@dataclass(frozen=True)
class ActionRunResponse:
    status_code: int
    payload: dict[str, Any]


class ActionRunRouteError(RouteError):
    pass


class ActionRunService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def _storage_org_id(self) -> int | None:
        return self._workspace.queue_storage_org_id

    def run_action(
        self,
        project_id: str,
        body: dict[str, Any],
        *,
        request_context: Any = None,
        admitted_sources: Mapping[str, BoundLocalFile] | None = None,
    ) -> ActionRunResponse:
        # Payload-only gates FIRST, before the project is resolved. These two
        # read nothing but the action envelope and this process's environment,
        # so a refusal they own is already final: opening the workspace to
        # produce a 404 instead would answer a different question than the
        # caller asked, and (for a project that DOES exist) would create the
        # project's files on the way to refusing anyway. Everything that needs
        # project state stays below `self._workspace.get`.
        edition_result = self._edition_unavailable_result(project_id, body)
        if edition_result is not None:
            return _action_run_response(edition_result)
        code_action_result = self._code_action_disabled_result(
            project_id,
            body,
        )
        if code_action_result is not None:
            return _action_run_response(code_action_result)
        project = self._workspace.get(project_id)
        replay_result = self._typed_receipt_replay_result(project, project_id, body)
        if replay_result is not None:
            return self._direct_result_response(project, project_id, replay_result)
        execution_context = self._workspace.edition_execution_composition_context_for(
            request_context
        )
        # This is the ONE choke point both the queued
        # (queue_v1_action_run) and direct (run_action_spec) dispatch paths
        # pass through below, so a required_credentials action is gated
        # regardless of its declared placement (executor/queue_policy.py).
        # status="failed" (not "needs_confirmation") keeps this OFF the
        # cost-gate's 402 envelope.
        credential_result = self._missing_credential_result(project, project_id, body)
        if credential_result is not None:
            return _action_run_response(credential_result)
        # Egress gate, always-remote class: the CATALOG's static
        # required_capabilities — never the client-supplied
        # body["capabilities"] — decides whether the kind is network-bound.
        # Engine-dependent kinds gate later at MapRunner._validate_spec.
        network_result = self._network_disabled_result(project, project_id, body)
        if network_result is not None:
            return _action_run_response(network_result)
        # Resolve the request's effective router once. The missing-key gate,
        # queued admission and direct dispatcher must all inspect/use this same
        # object; a later asynchronous worker execution deliberately resolves
        # its own fresh router from the registered factory.
        effective_router = self._workspace.action_execution_router_for(project)
        # This is the same choke point, with the same choice of
        # status="failed" over 402 — see _missing_model_key_result and
        # missing_model_key_error_for_request (server/action_enqueue.py) for
        # the gate's scope and the replay-mode/cache design decision.
        model_key_result = self._missing_model_key_result(
            project,
            project_id,
            body,
            router=effective_router,
            execution_context=execution_context,
        )
        if model_key_result is not None:
            if self._workspace.require_explicit_provider_keys and any(
                error.code == "missing_provider_key"
                or error.details.get("resumable") is True
                for error in model_key_result.errors
            ):
                # Team BYOK exhaustion is an ordinary resumable product
                # outcome, not malformed input and never the commerce 402.
                return ActionRunResponse(
                    status_code=200,
                    payload=model_key_result.model_dump(mode="json"),
                )
            return _action_run_response(model_key_result)
        # Resolve request-scoped dependencies at most once, and only if the
        # selected execution path asks for them. In particular the Google
        # Sheets action-job admission check uses the exact same request bag the
        # direct path would use; other queued actions do not acquire it merely
        # by passing through this coordinator.
        request_deps: ExecutorDeps | None = None

        def request_executor_deps() -> ExecutorDeps:
            nonlocal request_deps
            if request_deps is None:
                resolved = (
                    self._workspace.executor_deps_factory(project_id, request_context)
                    if self._workspace.executor_deps_factory
                    else None
                )
                request_deps = resolved or ExecutorDeps()
                if admitted_sources is not None:
                    request_deps = replace(
                        request_deps, local_file_sources=admitted_sources
                    )
            return request_deps

        queued_result = queue_v1_action_run(
            project,
            project_id,
            body,
            ctx=QueuedV1ActionRunContext(
                queue=self._workspace.queue,
                workspace_root=self._workspace.root,
                router_for=lambda _project: effective_router,
                execution_composition_for=self._workspace.execution_composition_for,
                active_runs=self._workspace.active_runs,
                run_jobs=self._workspace.run_jobs,
                queue_payload_extra=self._workspace.queue_payload_extra,
                logger=LOG,
                request_executor_deps=request_executor_deps,
            ),
            execution_context=execution_context,
        )
        if queued_result is not None:
            return _action_run_response(queued_result)

        execution_composition = self._workspace.execution_composition_for(
            project,
            effective_router,
            execution_context,
        )
        direct_request_deps = request_executor_deps()
        result = run_action_spec(
            project,
            body,
            project_id=project_id,
            router=effective_router,
            deps=replace(
                direct_request_deps,
                execution_composition=execution_composition,
            ),
            edition_run_context=execution_context.edition_snapshot,
        )
        return self._direct_result_response(project, project_id, result)

    def _direct_result_response(
        self,
        project: Project,
        project_id: str,
        result: V1ActionResult,
    ) -> ActionRunResponse:
        self._settle_direct_terminal_receipt(project, project_id, result)
        self._enqueue_completed_v1_source_poll_enclosures(project, project_id, result)
        self._trigger_completed_v1_source_poll_watches(project, project_id, result)
        if self._workspace.require_explicit_provider_keys and any(
            error.details.get("resumable") is True for error in result.errors
        ):
            return ActionRunResponse(
                status_code=200,
                payload=result.model_dump(mode="json"),
            )
        return _action_run_response(result)

    def _settle_direct_terminal_receipt(
        self,
        project: Project,
        project_id: str,
        result: V1ActionResult,
    ) -> None:
        """Invoke edition settlement only across a durable terminal boundary.

        Completed and failed receipt replays return the original receipt id,
        so the same callback also closes a process-death window without
        provider re-egress. Receiptless refusals and replay-validation errors
        deliberately do not authorize settlement.
        """

        port = self._workspace.direct_action_receipt_settlement_port
        if port is None or result.receipt_id is None:
            return
        stored = ReceiptStore(project).find_by_id(result.receipt_id)
        if stored is None or stored.status not in FINISHED_RECEIPT_STATUSES:
            return
        port.settle_action_receipt(
            project=project,
            project_id=project_id,
            receipt_id=stored.id,
        )

    def _edition_unavailable_result(
        self,
        project_id: str,
        body: dict[str, Any],
    ) -> V1ActionResult | None:
        kind = action_id_from_request(body) if isinstance(body, dict) else None
        if not isinstance(kind, str):
            return None
        effective_edition = self._workspace.edition
        if action_available_in_edition(kind, effective_edition):
            return None
        return V1ActionResult(
            action={"kind": kind, "action_id": _new_id("act")},
            status="failed",
            project_id=project_id,
            errors=[
                V1ActionError(
                    code="action_unavailable_in_edition",
                    message=action_edition_unavailable_message(kind, effective_edition),
                    action_kind=kind,
                    field="kind",
                )
            ],
        )

    @staticmethod
    def _typed_receipt_replay_result(
        project: Project,
        project_id: str,
        body: dict[str, Any],
    ) -> V1ActionResult | None:
        """Replay existing receipts before keys, preserving queue recovery."""

        key = body.get("idempotency_key")
        if not isinstance(key, str):
            return None
        stored = ReceiptStore(project).find_by_idempotency_key(key)
        if stored is None:
            return None

        try:
            from frisket.actions.core import MapBatch, MapRows, ModelRows
            from frisket.actions.system import typed_action_for_request
            from frisket.engine.executor.map_rows_action import (
                typed_map_rows_replay_result,
            )

            bound = typed_action_for_request(body)
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(
            bound.action.definition.run,
            (MapRows, MapBatch, ModelRows),
        ):
            return None
        return typed_map_rows_replay_result(project, project_id, bound)

    @staticmethod
    def _code_action_disabled_result(
        project_id: str,
        body: dict[str, Any],
    ) -> V1ActionResult | None:
        """Apply the trusted-local code policy at the canonical action gate."""

        kind = action_id_from_request(body) if isinstance(body, dict) else None
        if not isinstance(kind, str):
            return None
        from frisket.authoring.action_metadata import gated_capability_phrase

        gated = gated_capability_phrase(kind)
        if gated is None or os.environ.get("FRISKET_ALLOW_CODE_RECIPES", "1") == "1":
            return None
        return V1ActionResult(
            action={"kind": kind, "action_id": _new_id("act")},
            status="failed",
            project_id=project_id,
            errors=[
                V1ActionError(
                    code="code_action_disabled",
                    message=(
                        f"the '{kind}' action declares {gated}, which is disabled "
                        "on this server; enable it only on trusted/local deployments"
                    ),
                    action_kind=kind,
                    field="kind",
                )
            ],
        )

    @staticmethod
    def _network_disabled_result(
        project: Project,
        project_id: str,
        body: dict[str, Any],
    ) -> V1ActionResult | None:
        """None unless the kind's static catalog ``required_capabilities``
        carries an ``external:*`` tag AND the project's effective network
        policy is ``off`` — then the terminal ``network_disabled`` result.
        Reads only server-owned facts: the catalog entry (Python code) and
        the project's stored policy. The request's ``capabilities`` list is
        client-supplied and deliberately never consulted (bypassable in both
        directions)."""
        kind = action_id_from_request(body) if isinstance(body, dict) else None
        if not isinstance(kind, str):
            return None
        external = next(
            (
                str(capability)
                for capability in declared_action_capabilities(kind)
                if str(capability).startswith("external:")
            ),
            None,
        )
        if external is None:
            return None
        if project.effective_network_policy() != "off":
            return None
        return V1ActionResult(
            action={"kind": kind, "action_id": _new_id("act")},
            status="failed",
            project_id=project_id,
            errors=[network_disabled_error(kind, external)],
        )

    @staticmethod
    def _missing_credential_result(
        project: Project,
        project_id: str,
        body: dict[str, Any],
    ) -> V1ActionResult | None:
        """None when the action either has no `required_credentials`
        (the overwhelming majority) or all of them resolve; otherwise the
        terminal `missing_action_credential` result (manifest
        action-api-key-gate-v1) — census enrichment is the pinned concrete
        case (actions/census.py)."""
        kind = action_id_from_request(body) if isinstance(body, dict) else None
        if not isinstance(kind, str):
            return None
        from frisket.actions.registry import ACTION_REGISTRY

        try:
            required = (
                ACTION_REGISTRY.get(kind)
                .catalog_entry()
                .get("required_credentials", [])
            )
        except KeyError:
            return None
        if not required:
            return None
        missing = missing_required_credentials(project, required)
        if not missing:
            return None
        return V1ActionResult(
            action={"kind": kind, "action_id": _new_id("act")},
            status="failed",
            project_id=project_id,
            errors=[missing_action_credential_error(kind, missing)],
        )

    def _missing_model_key_result(
        self,
        project: Project,
        project_id: str,
        body: dict[str, Any],
        *,
        router: ModelRouter,
        execution_context: ExecutionCompositionContext,
    ) -> V1ActionResult | None:
        """None when the action's params carry no chat-completion `model`
        field, the field's provider needs no key (local/keyless, e.g.
        `ollama`), the router already has an adapter configured for it, or
        the cache/replay-mode combination could still serve the run keyless
        — otherwise the terminal `missing_provider_key` result (manifest
        llm-model-key-request-gate-v1). See
        `missing_model_key_error_for_request` (server/action_enqueue.py) for
        the full gate logic and its documented replay-mode design decision;
        this method just adapts it to the same request/response shape
        `_missing_credential_result` above uses."""
        kind = action_id_from_request(body) if isinstance(body, dict) else None
        if not isinstance(kind, str):
            return None
        params = body.get("params") if isinstance(body, dict) else None
        if not isinstance(params, dict):
            return None
        model = params.get("model")
        if (
            self._workspace.require_explicit_provider_keys
            and isinstance(model, str)
            and "/" in model
        ):
            provider = model.split("/", 1)[0]
            exact_replay = exact_keyless_replay_covers_action(
                project,
                body,
                router,
                execution_composition=self._workspace.execution_composition_for(
                    project,
                    router,
                    execution_context,
                ),
            )
            if router.adapter_for(provider) is None and not exact_replay:
                return V1ActionResult(
                    action={
                        "kind": kind,
                        "action_id": _new_id("act"),
                    },
                    status="failed",
                    project_id=project_id,
                    errors=[missing_provider_key_error(kind, provider)],
                )
        error = missing_model_key_error_for_request(kind, params, router)
        if error is None:
            return None
        return V1ActionResult(
            action={"kind": kind, "action_id": _new_id("act")},
            status="failed",
            project_id=project_id,
            errors=[error],
        )

    def receipt_lookup(self, project_id: str, receipt_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        body = ReceiptStore(project).body_by_id(receipt_id)
        if body is None:
            raise ActionRunRouteError(
                404,
                _v1_action_error(
                    code="receipt_not_found",
                    message="No receipt exists with that id in this project.",
                    field="receipt_id",
                    details={"receipt_id": receipt_id},
                ),
            )
        try:
            receipt = V1Receipt.model_validate(json.loads(body))
        except Exception as exc:  # noqa: BLE001 - corrupt project receipt body
            raise ActionRunRouteError(
                500,
                _v1_action_error(
                    code="invalid_receipt_body",
                    message="The stored receipt body is not a valid v1 receipt.",
                    field="receipt_id",
                    details={"receipt_id": receipt_id, "error": str(exc)},
                ),
            ) from exc
        return receipt.model_dump(mode="json")

    def list_jobs(
        self,
        project_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        now = datetime.now(UTC)
        jobs = []
        for kind in (RUN_PROJECT_KIND, ACTION_RUN_KIND):
            jobs.extend(
                self._workspace.queue.list_project_jobs(
                    project_id,
                    storage_org_id=self._storage_org_id(),
                    status=status,
                    limit=limit,
                    kind=kind,
                )
            )
        payloads = [
            project_action_job_payload(job, project_id=project_id, now=now)
            for job in jobs
        ]
        # Union in runs whose action placement is
        # INLINE (no project.run queue job was ever written — see
        # jobs/projection.py's RUN_INLINE_KIND docstring) so every run surfaces
        # here regardless of execution path. A run already represented by a
        # RUN_PROJECT_KIND queue job is never double-projected. The exclusion
        # set is deliberately UNFILTERED by `status` (a run's `runs.status`
        # defaults to 'running' the instant it is reserved, before its queue
        # job is even claimed — filtering this lookup by the caller's queue
        # -job status would "unmask" an already-queued run as if uncovered).
        covering_jobs = self._workspace.queue.list_project_jobs(
            project_id,
            storage_org_id=self._storage_org_id(),
            status=None,
            limit=max(limit, _INLINE_RUN_EXCLUSION_SCAN_LIMIT),
            kind=RUN_PROJECT_KIND,
        )
        queued_run_ids = {
            job_run_id(job) for job in covering_jobs if job_run_id(job) is not None
        }
        payloads.extend(
            run_inline_job_payload(row, project_id=project_id, now=now)
            for row in _unqueued_run_rows(
                project, exclude_run_ids=queued_run_ids, status=status, limit=limit
            )
        )
        payloads.sort(key=_job_payload_sort_key, reverse=True)
        return {
            "schema_version": "frisket.job_list.v1",
            "project_id": project_id,
            "jobs": payloads[:limit],
        }

    def job_detail(self, project_id: str, job_id: int) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        inline_run_id = run_id_from_inline_job_id(job_id)
        if inline_run_id is not None:
            row = project.db.execute(
                "SELECT * FROM runs WHERE id=?", (inline_run_id,)
            ).fetchone()
            if row is None:
                raise ActionRunRouteError(404, "no such action job")
            return run_inline_job_payload(
                row, project_id=project_id, now=datetime.now(UTC)
            )
        job = self._workspace.queue.get(job_id)
        if (
            job is None
            or job.kind not in {RUN_PROJECT_KIND, ACTION_RUN_KIND}
            or job_project_id(job) != project_id
            or (
                self._storage_org_id() is not None
                and job.storage_org_id != self._storage_org_id()
            )
        ):
            raise ActionRunRouteError(404, "no such action job")
        return project_action_job_payload(
            job,
            project_id=project_id,
            now=datetime.now(UTC),
        )

    def cancel_job(self, project_id: str, job_id: int) -> dict[str, Any]:
        self._workspace.get(project_id)
        job = self._workspace.queue.get(job_id)
        if (
            job is None
            or job.kind != ACTION_RUN_KIND
            or job_project_id(job) != project_id
            or (
                self._storage_org_id() is not None
                and job.storage_org_id != self._storage_org_id()
            )
        ):
            raise ActionRunRouteError(404, "no such cancellable action job")
        if job.status in {"done", "failed", "cancelled"}:
            raise ActionRunRouteError(
                409,
                f"job is already {job.status} — nothing to cancel",
            )
        # Runless actions own deterministic Project transactions:
        # once claimed, they finish rather than leaving a cancelled queue row
        # beside committed Project state. Row actions use project.run's ordinary
        # cancellation and claim machinery, including installed actions.
        cancelled = self._workspace.queue.cancel_queued(job_id)
        if not cancelled:
            refreshed = self._workspace.queue.get(job_id)
            if refreshed is not None and refreshed.status == "running":
                raise ActionRunRouteError(
                    409,
                    "job is already running — its deterministic transaction "
                    "will finish",
                )
            status = refreshed.status if refreshed is not None else "unavailable"
            raise ActionRunRouteError(
                409, f"job is already {status} — nothing to cancel"
            )
        refreshed = self._workspace.queue.get(job_id)
        return {
            "schema_version": "frisket.action_job_cancel.v1",
            "project_id": project_id,
            "job_id": job_id,
            "status": "cancelled",
            "kind": ACTION_RUN_KIND,
            "action_kind": job.action_kind,
            "receipt_id": job.receipt_id,
            "queue_cancelled": True,
            "job_status": refreshed.status if refreshed is not None else "cancelled",
        }

    def _enqueue_completed_v1_source_poll_enclosures(
        self,
        project: Project,
        project_id: str,
        result: V1ActionResult,
    ) -> int:
        if (
            result.status != "completed"
            or result.action.kind != "source.poll"
            or not result.receipt_id
        ):
            return 0
        body = ReceiptStore(project).body_by_id(result.receipt_id)
        if body is None:
            return 0
        try:
            receipt = json.loads(body)
        except (TypeError, ValueError):
            return 0
        refs = [
            item.get("ref") or {}
            for item in [*receipt.get("outputs", []), *receipt.get("evidence", [])]
            if isinstance(item, dict)
        ]
        source_run = next(
            (ref for ref in refs if ref.get("kind") == "source_poll_run"),
            {},
        )
        enclosures = next(
            (
                ref
                for ref in refs
                if ref.get("kind") == "source_poll_enclosure_pointers"
            ),
            {},
        )
        row_ids = [int(row_id) for row_id in enclosures.get("row_ids") or []]
        if not row_ids:
            return 0
        try:
            source_id = int(source_run.get("source_id") or enclosures["source_id"])
            sheet_id = int(enclosures.get("sheet_id") or source_run["sheet_id"])
        except (KeyError, TypeError, ValueError):
            return 0
        source = SourceStore(project).get_source(source_id)
        if source is None:
            return 0
        source_data = dict(source)
        try:
            source_data["config"] = json.loads(source_data.get("config") or "{}")
        except (TypeError, ValueError):
            source_data["config"] = {}
        return len(
            enqueue_enclosure_downloads(
                self._workspace.queue,
                project=project,
                project_id=project_id,
                workspace_root=self._workspace.root,
                storage_org_id=self._workspace.queue_storage_org_id,
                source=source_data,
                sheet_id=sheet_id,
                row_ids=row_ids,
            )
        )

    def _trigger_completed_v1_source_poll_watches(
        self,
        project: Project,
        project_id: str,
        result: V1ActionResult,
    ) -> int:
        if result.status != "completed" or result.action.kind != "source.poll":
            return 0
        materialization = source_poll_materialization_from_receipt(
            project,
            project_id=project_id,
            receipt_id=result.receipt_id or "",
        )
        if materialization and materialization.get("sheet_id") is not None:
            enqueue_source_append_refreshes(
                self._workspace.queue,
                project=project,
                project_id=project_id,
                workspace_root=self._workspace.root,
                storage_org_id=self._workspace.queue_storage_org_id,
                sheet_id=int(materialization["sheet_id"]),
                row_ids=[int(r) for r in materialization.get("row_ids") or []],
                trigger_ref={
                    "trigger_kind": "source_run_completed",
                    "source_id": materialization.get("source_id"),
                    "source_run_id": materialization.get("source_run_id"),
                    "receipt_id": result.receipt_id,
                    "sheet_id": materialization["sheet_id"],
                },
            )
        return len(
            trigger_completed_source_poll_watches(
                project,
                project_id=project_id,
                receipt_id=result.receipt_id,
            )
        )


def _action_run_response(result: V1ActionResult) -> ActionRunResponse:
    return ActionRunResponse(
        status_code=_v1_action_result_http_status(result),
        payload=result.model_dump(mode="json"),
    )


def v1_action_result_http_status(result: V1ActionResult) -> int:
    return _v1_action_result_http_status(result)


def _v1_action_result_http_status(result: V1ActionResult) -> int:
    # Provider-key-refusal seats: run (this fallback) -> 400; preview -> 400;
    # copilot -> deliberate 409 (server/routes/project_copilot.py:33).
    if result.status in {"completed", "queued", "running", "partial", "cancelled"}:
        return 200
    if result.status == "needs_confirmation":
        return 402
    codes = {error.code for error in result.errors}
    if codes & {
        "idempotency_conflict",
        "output_claim_conflict",
        "output_column_exists",
        "duplicate_sheet_name",
        "stale_replay",
        "plugin_code_integrity_mismatch",
    }:
        return 409
    if codes & {"project_write_failed"}:
        return 500
    return 400


def _v1_action_error(
    *,
    code: str,
    message: str,
    action_kind: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return V1ActionError(
        code=code,
        message=message,
        action_kind=action_kind,
        field=field,
        details=dict(details or {}),
    ).model_dump(mode="json")
