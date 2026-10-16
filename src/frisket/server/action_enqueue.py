"""Server-side enqueue orchestration for canonical v1 action runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from frisket.contracts.action import (
    ActionError,
    ActionResult,
)
from frisket.engine.executor.action_jobs import launch_queued_action_job
from frisket.engine.executor.action_inventory import (
    ExecutorDeps,
)
from frisket.engine.executor.action_support import (
    _failed_result,
)
from frisket.engine.executor.queued_actions import (
    QueuedV1ActionRequest,
    queued_v1_action_request,
    queued_v1_job_payload,
)
from frisket.engine.jobs.queue import ACTION_RUN_KIND, JobQueue
from frisket.engine.jobs.runs import RUN_PROJECT_KIND, build_attempt_authority
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.remediation import missing_provider_key_message
from frisket.engine.runner import (
    CostGate,
    EmptyInputColumns,
    InvalidTargetRows,
    InvalidTargetSheet,
    MapRunner,
    NetworkDisabled,
    OutputColumnExists,
    ProviderKeyRefusal,
    RunProgress,
)
from frisket.server.provider_config import KEY_PROVIDERS
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.attempt import STALE_DISPATCHING_AGE
from frisket.engine.executor.action_jobs import merge_queue_payload
from frisket.operability.structured_logging import correlation_log_payload
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.provider import (
    ExecutionComposition,
    ExecutionCompositionContext,
)


# Shared error shape for the action-credential gate. The actual gate check
# lives in
# ActionRunService.run_action (server/services/action_runs.py, the ONE choke
# point both the queued path below and the direct-dispatch path pass
# through) — this module just owns the error-building helper so both that
# gate and server/action_catalog_hints.py's project-aware
# ui_hints.missing_credentials (ActionPanel's proactive Settings-deep-link
# banner) describe the same shape. status="failed" keeps this OFF the
# cost-gate's needs_confirmation/402 envelope, mirroring the
# missing_provider_key precedent below.
MISSING_ACTION_CREDENTIAL_ERROR_CODE = "missing_action_credential"


def missing_action_credential_error(
    action_kind: str,
    missing: list[str],
) -> ActionError:
    joined = ", ".join(missing)
    return ActionError(
        code=MISSING_ACTION_CREDENTIAL_ERROR_CODE,
        message=(
            f"'{action_kind}' requires {joined} to run. Add it in "
            "Settings → Secrets, then try again."
        ),
        action_kind=action_kind,
        details={
            "missing_credentials": missing,
            "settings_scope": "project",
            "settings_section": "secrets",
        },
    )


# Shared shape for a keyless-model-provider refusal — code + message match
# the pre-existing
# MissingProviderKey exception handlers below and in
# frisket.executor.action_lifecycle / frisket.server.services.action_preview_runs
# verbatim (missing_provider_key_message is the one place the message text is
# built), so a request-time 400 reads identically to the row-level failure it
# replaces.
MISSING_PROVIDER_KEY_ERROR_CODE = "missing_provider_key"


def missing_provider_key_error(action_kind: str, provider: str) -> ActionError:
    return ActionError(
        code=MISSING_PROVIDER_KEY_ERROR_CODE,
        message=missing_provider_key_message(provider),
        action_kind=action_kind,
        field="params.model",
        details={"provider": provider, "retryable": True, "resumable": True},
    )


def provider_key_refusal_error(
    action_kind: str, exc: ProviderKeyRefusal
) -> ActionError:
    """Turn ANY provider-key refusal into its ActionError.

    Handlers call this instead of matching on the concrete refusal class, so
    a new refusal (e.g. the spend cap) surfaces through every existing path
    without touching a single catch site. The exception owns its code, field,
    message, and details; this function owns none of them.
    """
    return ActionError(
        code=exc.error_code,
        message=exc.action_message(),
        action_kind=action_kind,
        field=exc.field,
        details=dict(exc.details),
    )


# Egress gate: the project's network policy is off and the action resolves a remote
# capability. status="failed", never "needs_confirmation" — off is off, not a
# confirm-and-proceed gate. The classification is server-derived only.
NETWORK_DISABLED_ERROR_CODE = "network_disabled"


def network_disabled_error(action_kind: str, capability: str) -> ActionError:
    return ActionError(
        code=NETWORK_DISABLED_ERROR_CODE,
        message=(
            f"this project's network setting is off; it blocks {capability}. "
            "Turn network on in the project settings to run this."
        ),
        action_kind=action_kind,
        details={"capability": capability},
    )


def missing_model_key_error_for_request(
    action_kind: str,
    params: Mapping[str, Any],
    router: ModelRouter,
) -> ActionError | None:
    """Request-time model-key gate (manifest llm-model-key-request-gate-v1).

    Generalizes the missing_action_credential genre (a STATIC
    required_credentials list, census enrichment's pinned case) to actions
    whose params carry a chat-completion `model` field ("provider/model-id")
    instead — every `map.*`/`reduce.group_summary`/`research.answer` kind
    (the typed registry's `cost_policy.kind="model_metered"` /
    `model:complete` capability set).
    None (no gate) when: `params` carries no string `model` field shaped
    "provider/model-id" (nothing to resolve — e.g. supported `sheet.refresh`
    rebuilds stored deterministic intent without a model call); the provider isn't one of
    KEY_PROVIDERS (`server/provider_config.py`) — local/keyless providers
    (`ollama`) and unrecognized provider ids never gate here; or the
    router already has an adapter for the provider (a key IS configured).

    Replay-mode nuance (LIVE run 15: map.extract w/ gemini/flash-lite and no
    gemini key burned 11 rows each failing this same message — "knowable at
    LAUNCH", the incident that minted this task): a `'replay'`-mode cache HIT
    can complete a row with NO adapter at all (`ModelRouter.complete()`
    answers from cache before any adapter lookup), so a blanket key-absence
    block would ALSO refuse the "keyless dev default" pattern
    (committed-cache replay tests, and any real re-run of
    fully-cached rows) that the whole test suite depends on. Cheaply proving
    every TARGET ROW of THIS specific request is a cache hit before
    reservation would mean duplicating each recipe's request-rendering logic
    at this generic, recipe-agnostic seam — not a cheap, honest thing to do
    here. Mirroring MapRunner._validate_spec's own MissingProviderKey
    pre-flight in ``runner/map_runner.py``, this gate uses the explicit
    fallback: gate on key absence, with cache-can-
    serve-replay as the one exemption. Strict replay WITH A CACHE never falls
    through to a live call at all (a miss raises typed CacheMiss, not an
    adapter call) so it is exempt too — but only with the cache, on the same
    two-axis proof (`model_call_cannot_go_live`) the money path uses:
    `_complete_transport`'s strict branch is guarded on `self.cache is not
    None`, so a CACHELESS `'replay_strict'` router really does call the
    adapter and really does need a key, exactly like the cacheless `'replay'`
    case. A genuine miss under either exemption
    still surfaces the SAME `missing_provider_key` message, just at the row
    level (frisket.llm.remediation.classify_llm_error) — this gate does not
    change that pre-existing, already-tested outcome for map.* actions; it
    ADDS the same protection to the OTHER model-bearing kinds
    (research.answer, reduce.group_summary) that
    today have NO pre-launch key check at all and would otherwise fail every
    row individually in `'fresh'`/`'off'`/cacheless-`'replay'` mode.
    """
    model = params.get("model")
    if not isinstance(model, str) or "/" not in model:
        return None
    provider = model.split("/", 1)[0]
    if not provider or provider not in KEY_PROVIDERS:
        return None
    from frisket.ai.llm.router import model_call_cannot_go_live

    cache_can_serve_replay = router.cache_mode == "replay" and router.cache is not None
    if model_call_cannot_go_live(router) or cache_can_serve_replay:
        return None
    if router.adapter_for(provider) is not None:
        return None
    return missing_provider_key_error(action_kind, provider)


def exact_keyless_replay_covers_action(
    project: Project,
    action_body: Mapping[str, Any],
    router: ModelRouter,
    *,
    execution_composition: ExecutionComposition,
) -> bool:
    """Pure proof that a canonical queued map action can replay every row."""
    if isinstance(action_body.get("action_id"), str):
        try:
            from frisket.actions.system import typed_action_for_request
            from frisket.engine.executor.map_rows_action import (
                build_typed_map_rows_plan,
            )

            plan = build_typed_map_rows_plan(
                project,
                typed_action_for_request(dict(action_body)),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return MapRunner(
            project,
            router,
            authority=UnroutedOnlyAuthority(project),
            execution_composition=execution_composition,
        ).exact_replay_available(plan.spec_dict(), program=plan.program)

    return False


@dataclass(frozen=True)
class QueuedV1ActionRunContext:
    queue: JobQueue
    workspace_root: Path
    router_for: Callable[[Project], ModelRouter]
    execution_composition_for: Callable[
        [Project, ModelRouter, ExecutionCompositionContext], ExecutionComposition
    ]
    active_runs: MutableMapping[tuple[str, int], RunProgress]
    run_jobs: MutableMapping[tuple[str, int], int]
    queue_payload_extra: Mapping[str, Any]
    logger: logging.Logger
    request_executor_deps: Callable[[], ExecutorDeps] = ExecutorDeps


def attach_run_edition_run_context(
    project: Project,
    run_id: int,
    snapshot: Mapping[str, Any] | None,
    *,
    commit: bool = True,
) -> None:
    if not snapshot:
        return
    # The edition context lands on its own column, not
    # inside `params`. It was always write-only there — no src reader, one
    # identity carve-out, and a pop in the preview path — so relocating it
    # costs nothing and takes `start_run`'s INSERT one step closer to being
    # the only writer of the spec blob.
    row = project.db.execute(
        "SELECT edition_run_context FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if row is None or row["edition_run_context"] is not None:
        return
    project.db.execute(
        "UPDATE runs SET edition_run_context=? WHERE id=?",
        (json.dumps(dict(snapshot), sort_keys=True), run_id),
    )
    if commit:
        project.db.commit()


def _prepared_run_progress(project: Project, run_id: int) -> RunProgress:
    row = project.db.execute(
        "SELECT total_rows FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    total = int(row["total_rows"] or 0) if row is not None else 0
    return RunProgress(run_id=run_id, total=total)


def _queue_v1_project_run_request(
    project: Project,
    project_id: str,
    action_body: Mapping[str, Any],
    *,
    ctx: QueuedV1ActionRunContext,
    execution_context: ExecutionCompositionContext,
    request: QueuedV1ActionRequest,
    payload_extra: Mapping[str, Any] | None = None,
) -> ActionResult:
    # Fail before reserving a receipt/run: queue metadata is allowed to add
    # hosted routing and tracing fields, never to replace the identity that the
    # worker, queue indexes, and terminal receipt hooks must share.
    protected_payload_keys = {
        "project_id",
        "run_id",
        "spec",
        "action_kind",
        "v1_action",
        "v1_action_id",
        "v1_receipt_id",
        "v1_params_hash",
        "workspace_root",
        "v1_cache_mode",
        "v1_local_endpoint_bindings",
        "dedupe_key",
    }
    protected_payload_keys.update(
        f"v1_{codec.key}" for codec in request.entry.payload_codecs
    )
    merge_queue_payload(
        {},
        payload_extra,
        ctx.queue_payload_extra,
        protected_keys=protected_payload_keys,
    )
    # The credential gate itself lives one layer up, in
    # ActionRunService.run_action (server/services/action_runs.py) — the ONE
    # choke point both this queued path and the direct-dispatch path
    # (run_action_spec) pass through, so a future required_credentials action
    # is covered regardless of its queued/direct placement (executor/
    # queue_policy.py). See missing_action_credential_error below for the
    # shared error shape.
    #
    # The router is built ONCE and reused
    # for both the reservation (join.semantic's resolve seam needs it to
    # resolve the embedding backend; every other queued kind's resolve_fn
    # ignores it, spec.resolve_needs_router=False) and the MapRunner
    # instantiation below.
    router = ctx.router_for(project)
    execution_composition = ctx.execution_composition_for(
        project, router, execution_context
    )
    consent_coverage = ctx.request_executor_deps().consent_coverage
    atomic_publication = request.entry.requires_atomic_publication(
        request.params, program=request.program
    )
    if atomic_publication:
        # Mint/cache the deployment principal before the publication lock.
        # Route persistence may read it, but no participant may commit the
        # caller-owned transaction.
        from frisket.engine.store.execution_routes import instance_principal

        instance_principal(project)
        project.db.execute("BEGIN IMMEDIATE")
    try:
        reservation = request.entry.reserve_action(
            project,
            request.action,
            request.params,
            project_id=project_id,
            recover_unobservable=True,
            router=router,
            program=request.program,
            commit=not atomic_publication,
        )
    except Exception:
        if atomic_publication:
            project.db.rollback()
        raise
    if isinstance(reservation, ActionResult):
        if atomic_publication:
            project.db.rollback()
        return reservation
    runner_spec = request.entry.mark_runner_spec(
        reservation["runner_spec"],
        receipt_id=reservation["receipt_id"],
        action_id=reservation["action_id"],
        params_hash=reservation["params_hash"],
    )
    runner_spec.pop("deferred_publication", None)
    if getattr(request.program, "defer_generation_seal", False) is True:
        runner_spec["deferred_publication"] = True
    reservation_snapshot = (
        {codec.key: reservation[codec.key] for codec in request.entry.payload_codecs}
        if request.program is not None
        else None
    )

    def abort_prepare(
        error: ActionError,
        *,
        status: str = "failed",
    ) -> ActionResult:
        """Close a failed pre-enqueue preparation without changing ownership.

        Atomic publication owns an open caller transaction and rolls it back as
        one unit.  The ordinary path owns a committed reservation and deletes
        it through the entry's lifecycle adapter, which transactionally
        discards its output claim before deleting the reservation.
        """

        if atomic_publication:
            project.db.rollback()
        else:
            request.entry.cleanup_reservation(
                project,
                receipt_id=reservation["receipt_id"],
            )
        return ActionResult(
            action={
                "kind": request.action.kind,
                "action_id": reservation["action_id"],
            },
            status=status,
            project_id=project_id,
            errors=[error],
        )

    prepared_run_id = reservation.get("prepared_run_id")
    if isinstance(prepared_run_id, int) and not isinstance(prepared_run_id, bool):
        progress = _prepared_run_progress(project, prepared_run_id)
    else:
        runner = MapRunner(
            project,
            router,
            consent_coverage=consent_coverage,
            allow_action_lifecycle_only_recipes=True,
            authority=(
                build_attempt_authority(
                    project,
                    composition=execution_composition,
                )
                if atomic_publication
                else UnroutedOnlyAuthority(project)
            ),
            execution_composition=execution_composition,
        )
        try:
            # A reservation payload without a "confirmed" key is NOT consent:
            # default False, matching the sibling
            # needs_confirmation checks. Kinds that thread confirmation put
            # the real value in the payload (_queued_action_reservation_
            # payload); free kinds never trip the gate at $0.
            attempt_id: str | None = None
            program_kwargs = (
                {} if request.program is None else {"program": request.program}
            )
            if atomic_publication:
                prepared, admitted_attempt = runner.prepare_admitted_run(
                    runner_spec,
                    **program_kwargs,
                    confirmed=bool(reservation.get("confirmed", False)),
                    output_fields=reservation["output_fields"],
                )
                progress = RunProgress(
                    run_id=prepared.run_id,
                    total=prepared.row_count,
                )
                attempt_id = admitted_attempt.attempt_id
            else:
                progress = runner.prepare_run(
                    runner_spec,
                    **program_kwargs,
                    confirmed=bool(reservation.get("confirmed", False)),
                )
            request.entry.mark_prepared(
                project,
                receipt_id=reservation["receipt_id"],
                run_id=progress.run_id,
                attempt_id=attempt_id,
                output_fields=reservation["output_fields"],
                program=request.program,
                reservation_snapshot=reservation_snapshot,
                commit=not atomic_publication,
            )
            attach_run_edition_run_context(
                project,
                progress.run_id,
                execution_context.edition_snapshot,
                commit=not atomic_publication,
            )
            if atomic_publication:
                project.db.commit()
        except CostGate as exc:
            error = request.entry.prepare_cost_gate_error(request.action, exc)
            return abort_prepare(
                error,
                status=(
                    "needs_confirmation"
                    if error.needs_confirmation
                    or error.code == "model_cost_requires_confirmation"
                    else "failed"
                ),
            )
        except ProviderKeyRefusal as exc:
            # Every refusal about the provider key this run would spend
            # through (no key configured; the key is already past its spend
            # cap). A typed error at run-confirm time (before any row work is
            # queued), named and settings-deep-linkable, instead of a run
            # that queues and then fails every row with a raw string.
            error = provider_key_refusal_error(request.action.kind, exc)
            return abort_prepare(error)
        except NetworkDisabled as exc:
            # Egress gate: same failed shape as
            # MissingProviderKey, never the needs_confirmation envelope.
            error = network_disabled_error(request.action.kind, exc.capability)
            return abort_prepare(error)
        except OutputColumnExists as exc:
            error = ActionError(
                code="output_column_exists",
                message=str(exc),
                action_kind=request.action.kind,
                field=request.entry.output_claim_error_field,
                details={"columns": exc.columns},
            )
            return abort_prepare(error)
        except InvalidTargetSheet as exc:
            error = ActionError(
                code="invalid_input_ref",
                message=str(exc),
                action_kind=request.action.kind,
                field=(
                    "row_scope.sheet_id"
                    if request.action.row_scope is not None
                    else "params.sheet_id"
                ),
                details={"sheet_id": exc.sheet_id},
            )
            return abort_prepare(error)
        except InvalidTargetRows as exc:
            error = ActionError(
                code="invalid_input_ref",
                message=str(exc),
                action_kind=request.action.kind,
                field=(
                    "row_scope.selector.membership.row_ids"
                    if request.action.row_scope is not None
                    else "params.row_ids"
                ),
                details={"missing": exc.missing},
            )
            return abort_prepare(error)
        except EmptyInputColumns as exc:
            # Refuse before
            # any job is queued/`runs` row created — see the matching catch
            # in executor.action_lifecycle for the direct-dispatch twin.
            error = ActionError(
                code="empty_input_column",
                message=str(exc),
                action_kind=request.action.kind,
                field=(
                    request.program.empty_input_error_field
                    if request.program is not None
                    else "params.input_columns"
                ),
                details={"columns": exc.columns},
            )
            return abort_prepare(error)
        except (ValueError, RuntimeError) as exc:
            error = request.entry.prepare_run_error(request.action, exc)
            return abort_prepare(error)
        except BaseException:
            if atomic_publication and project.db.in_transaction:
                project.db.rollback()
            raise
    if isinstance(prepared_run_id, int) and not isinstance(prepared_run_id, bool):
        try:
            if atomic_publication and reservation.get("replace_abandoned_attempt"):
                runner = MapRunner(
                    project,
                    router,
                    consent_coverage=consent_coverage,
                    allow_action_lifecycle_only_recipes=True,
                    authority=build_attempt_authority(
                        project,
                        composition=execution_composition,
                    ),
                    execution_composition=execution_composition,
                )
                claim_token = f"output-claim:{reservation['receipt_id']}"
                output_column_ids = list(
                    OutputColumnClaimStore(project).active_column_ids(
                        claim_token=claim_token,
                        run_id=progress.run_id,
                    )
                )
                if len(output_column_ids) != len(reservation["output_fields"]):
                    raise RuntimeError(
                        "the resumed output claim group no longer matches "
                        "the durable output plan"
                    )
                from frisket.engine.runner.validation import recipe_for_spec

                attempt = runner.authority.mint(
                    recipe=request.program or recipe_for_spec(runner_spec),
                    spec=runner_spec,
                    run_id=progress.run_id,
                    scope=tuple(
                        runner._pending_run_scope_row_ids(
                            progress.run_id,
                            output_column_ids,
                        )
                    ),
                    commit=False,
                )
                renewed = OutputColumnClaimStore(project).renew(
                    claim_token=claim_token,
                    run_id=progress.run_id,
                    lease_seconds=int(STALE_DISPATCHING_AGE.total_seconds()),
                    commit=False,
                )
                if renewed != len(output_column_ids):
                    raise RuntimeError(
                        "the resumed output claim group could not be renewed"
                    )
                request.entry.mark_prepared(
                    project,
                    receipt_id=reservation["receipt_id"],
                    run_id=progress.run_id,
                    attempt_id=attempt.attempt_id,
                    output_fields=reservation["output_fields"],
                    program=request.program,
                    reservation_snapshot=reservation_snapshot,
                    commit=False,
                )
            if atomic_publication:
                attach_run_edition_run_context(
                    project,
                    progress.run_id,
                    execution_context.edition_snapshot,
                    commit=False,
                )
                project.db.commit()
            else:
                attach_run_edition_run_context(
                    project,
                    progress.run_id,
                    execution_context.edition_snapshot,
                )
        except BaseException:
            if atomic_publication and project.db.in_transaction:
                project.db.rollback()
            raise
    authoritative_payload = {
        "project_id": project_id,
        "run_id": progress.run_id,
        "spec": runner_spec,
        # Freeze the execution posture that admitted and cost-gated this run.
        # A later Preferences change applies to later launches; it may not turn
        # an already-queued strict-replay run into a live provider call.
        "v1_cache_mode": router.cache_mode,
        # Stable ids make cache identity deterministic; the origin snapshot is
        # the retry/claim fence for env or hand-edited configuration.
        "v1_local_endpoint_bindings": reservation["local_endpoint_bindings"],
        **queued_v1_job_payload(
            request,
            action_body,
            reservation,
            run_id=progress.run_id,
        ),
        "workspace_root": str(ctx.workspace_root),
    }
    job_id = ctx.queue.enqueue(
        RUN_PROJECT_KIND,
        merge_queue_payload(
            authoritative_payload,
            payload_extra,
            correlation_log_payload(),
            ctx.queue_payload_extra,
        ),
        max_attempts=1,
    )
    request.entry.mark_enqueued(
        project,
        receipt_id=reservation["receipt_id"],
        run_id=progress.run_id,
        job_id=job_id,
    )
    ctx.active_runs[(project_id, progress.run_id)] = progress
    ctx.run_jobs[(project_id, progress.run_id)] = job_id
    ctx.logger.info(
        "v1_action_run_queued",
        extra={
            "event": "v1_action_run_queued",
            "project_id": project_id,
            "run_id": progress.run_id,
            "job_id": job_id,
            "action_kind": request.action.kind,
        },
    )
    return ActionResult(
        action={
            "kind": request.action.kind,
            "action_id": reservation["action_id"],
        },
        status="queued",
        project_id=project_id,
        run_id=progress.run_id,
        job_id=job_id,
        receipt_id=reservation["receipt_id"],
    )


def queue_v1_action_run(
    project: Project,
    project_id: str,
    action_body: Mapping[str, Any],
    *,
    ctx: QueuedV1ActionRunContext,
    execution_context: ExecutionCompositionContext,
) -> ActionResult | None:
    from frisket.engine.executor.action_dispatch import placement_for_kind
    from frisket.engine.executor.action_specs import PlacementPolicy, execution_spec_for

    from frisket.actions.system import action_id_from_request

    kind = (
        action_id_from_request(dict(action_body))
        if isinstance(action_body, Mapping)
        else None
    )
    if not isinstance(kind, str):
        return None
    from frisket.actions.types import ActionRequest
    from frisket.authoring.workbench.installed_actions import bind_installed_action

    try:
        installed = bind_installed_action(
            project, ActionRequest.model_validate(action_body)
        )
    except (KeyError, TypeError, ValueError) as exc:
        from frisket.engine.executor.map_rows_action import _typed_plan_error

        return _failed_result(
            project_id=project_id,
            action_kind=kind,
            error=_typed_plan_error(kind, exc),
        )
    if installed is not None:
        from frisket.actions.core import CreateSheet

        if installed.action.catalog_entry()["async_mode"] != "queued":
            return None
        placement = (
            PlacementPolicy.QUEUED_ACTION_JOB
            if isinstance(installed.action.definition.run, CreateSheet)
            else PlacementPolicy.QUEUED_PROJECT_RUN
        )
    if installed is None:
        try:
            placement = placement_for_kind(kind)
        except KeyError:
            return None
    if placement is PlacementPolicy.QUEUED_ACTION_JOB:
        execution_spec = execution_spec_for(kind)
        max_attempts = (
            2
            if execution_spec is not None
            and execution_spec.lifecycle.external_claim.claimed
            else 1
        )
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.action_jobs import reserve_typed_action_job

        try:
            bound = installed or typed_action_for_request(dict(action_body))
            from frisket.actions.core import GoogleSheetsExport
            from frisket.actions.find_types import FindScanner

            if getattr(bound.action.definition.run, "capabilities", ()) == (
                FindScanner,
            ):
                from frisket.engine.executor.find_action import (
                    reserve_typed_find_action_job,
                )

                envelope = reserve_typed_find_action_job(
                    project,
                    project_id,
                    bound,
                    edition_run_context=execution_context.edition_snapshot,
                    consent_coverage=ctx.request_executor_deps().consent_coverage,
                )
            elif isinstance(bound.action.definition.run, GoogleSheetsExport):
                from frisket.engine.executor.google_sheets_action import (
                    prepare_google_sheets_action_job,
                )

                envelope = prepare_google_sheets_action_job(
                    project,
                    project_id,
                    bound,
                    deps=ctx.request_executor_deps(),
                    edition_run_context=execution_context.edition_snapshot,
                )
            else:
                envelope = reserve_typed_action_job(
                    project,
                    project_id,
                    bound,
                    edition_run_context=execution_context.edition_snapshot,
                )
        except (KeyError, TypeError, ValueError) as exc:
            return _failed_result(
                project_id=project_id,
                action_kind=kind,
                error=ActionError(
                    code="invalid_action_request",
                    message=str(exc),
                    action_kind=kind,
                ),
            )
        if isinstance(envelope, ActionResult):
            return envelope
        return launch_queued_action_job(
            envelope=envelope,
            queue=ctx.queue,
            job_kind=ACTION_RUN_KIND,
            max_attempts=max_attempts,
            payload_extra={
                "workspace_root": str(ctx.workspace_root),
                **correlation_log_payload(),
                **ctx.queue_payload_extra,
            },
            project=project,
        )

    # Dispatch is driven by the declared placement policy: the queued registry is
    # the execution table behind queued_project_run, not the source of truth for
    # WHETHER an action queues. (Registry == declared placement is an invariant,
    # proven in tests/test_action_runtime_restructure_contract.py.)
    #
    # Placement is resolved FIRST: the old order asked
    # `queued_v1_action_request` before consulting placement, so any failure to
    # build the request — a canonical-params re-validation ValueError from an
    # overlay/projector divergence being the live bug class —
    # returned None and silently degraded a DECLARED-QUEUED kind to inline
    # execution. For a queued placement, a request that cannot be built is now
    # a terminal failed ActionResult, never a silent inline fallback.
    if placement is not PlacementPolicy.QUEUED_PROJECT_RUN:
        return None
    try:
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.map_rows_action import (
            _typed_plan_error,
            build_typed_map_rows_plan,
            typed_map_rows_replay_result,
        )

        bound = installed or typed_action_for_request(dict(action_body))
        replay = typed_map_rows_replay_result(project, project_id, bound)
        if replay is not None:
            return replay
        from frisket.actions.cluster_types import ValueClusterer

        if getattr(bound.action.definition.run, "capabilities", ()) == (
            ValueClusterer,
        ):
            from frisket.engine.executor.cluster_action import prepare_cluster_action

            initial_typed_plan = prepare_cluster_action(
                project, bound, router=ctx.router_for(project)
            )
        else:
            initial_typed_plan = build_typed_map_rows_plan(
                project,
                bound,
                _allow_existing_outputs=(
                    ReceiptStore(project).find_by_idempotency_key(
                        bound.request.idempotency_key
                    )
                    is not None
                ),
            )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=kind,
            error=_typed_plan_error(kind, exc),
        )
    request = queued_v1_action_request(
        action_body,
        project=project,
        initial_typed_plan=initial_typed_plan,
    )
    if request is None:
        from frisket.actions.system import validate_root_action

        validation = validate_root_action(dict(action_body))
        error = validation.error if not validation.ok else None
        if error is None:
            # The spec validated but the queued registry could not rebuild
            # canonical params (or the kind is missing from the registry
            # despite its queued placement): a seam divergence, reported
            # honestly instead of executed inline.
            error = ActionError(
                code="invalid_params",
                message=(
                    f"'{kind}' is declared queued but its canonical params "
                    "could not be re-validated for the queued path; refusing "
                    "to run it inline"
                ),
                action_kind=kind,
            )
        return _failed_result(
            project_id=project_id,
            action_kind=kind,
            error=error,
        )
    return _queue_v1_project_run_request(
        project,
        project_id,
        action_body,
        ctx=ctx,
        execution_context=execution_context,
        request=request,
    )
