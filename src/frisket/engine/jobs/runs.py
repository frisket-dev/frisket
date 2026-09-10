"""Queue job handlers for project recipe runs.

The public API remains run-id centric. Queue job ids are operational only: a
``project.run`` job carries the project id, the already-created run id, and the
spec, then resumes that run inside a worker process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, cast

from frisket.contracts.action import ActionError
from frisket.authoring.action_metadata import (
    action_available_in_edition,
    action_edition_unavailable_message,
    product_edition_from_snapshot,
)
from frisket.engine.executor.queued_actions import (
    queued_v1_identity_error,
    queued_v1_finalize_action_result,
    queued_v1_pre_run_error,
    queued_v1_prepared_attempt_id,
    queued_v1_payload_envelope,
    queued_v1_run_authorizes_action_lifecycle,
    queued_v1_terminal_failure_result,
    queued_v1_terminal_receipt_result,
    queued_v1_worker_exception_failure_result,
)
from frisket.ai.llm import (
    CACHE_MODES,
    RESUMABLE_PROVIDER_ERROR_DETAILS,
    CacheMode,
    ModelRouter,
    ResponseCache,
)
from frisket.ai.llm.endpoint_config import (
    LocalModelEndpointConfig,
)
from frisket.ops.base import persisted_recipe_invocation_halt
from frisket.engine.runner import MapRunner
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.store import Project
from frisket.engine.store.output_claims import ClaimLeaseRenewalFailed
from frisket.engine.store.receipts import FINISHED_RECEIPT_STATUSES, ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.worker_version import code_version
from frisket.execution.attempt_authority import AttemptAuthority
from frisket.execution.consent_coverage import ConsentCoverage
from decimal import Decimal
from frisket.execution.attempt import StaleAttemptWriter
from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed
from frisket.execution.provider import (
    ExecutionComposition,
    ExecutionCompositionContext,
    ExecutionCompositionFactory,
    open_execution_composition,
)
from frisket.project_identity import ProjectStorageKey

from .ports import JobHandlerContext, WorkerPorts, coerce_worker_ports
from .queue import (
    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
    claimed_project_location,
)
from .project_opener import ProjectOpener, open_claimed_project
from .worker import HandlerRegistration, HandlerRegistry

RUN_PROJECT_KIND = "project.run"
_RESPONSE_CACHE_UNSET = object()


def _queued_action_edition_error(
    action_kind: str,
    raw_edition_snapshot: object,
) -> ActionError | None:
    edition_snapshot = (
        None if raw_edition_snapshot is None else json.loads(str(raw_edition_snapshot))
    )
    if edition_snapshot is not None and not isinstance(edition_snapshot, Mapping):
        raise ValueError("run edition context must be a JSON object")
    product_edition = product_edition_from_snapshot(edition_snapshot)
    if product_edition is None or action_available_in_edition(
        action_kind, product_edition
    ):
        return None
    return ActionError(
        code="action_unavailable_in_edition",
        message=action_edition_unavailable_message(action_kind, product_edition),
        action_kind=action_kind,
        field="kind",
    )


def _has_live_managed_project_run_writer(project: Project, run_id: int) -> bool:
    """Whether queued cleanup must leave the reconciliation tuple intact."""

    open_generation = project.db.execute(
        "SELECT 1 FROM run_output_generations "
        "WHERE run_id=? AND state IN ('active','staged') LIMIT 1",
        (int(run_id),),
    ).fetchone()
    if open_generation is None:
        return False
    return (
        project.db.execute(
            "SELECT 1 FROM execution_attempts "
            "WHERE run_id=? AND state='dispatching' LIMIT 1",
            (int(run_id),),
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Worker-side route verification. Workers evaluate persisted
# artifacts only — ``confirmed=True`` on a spec is merely the retry-flow
# flag and authorizes nothing.
# ---------------------------------------------------------------------------


def build_attempt_authority(
    project: Project,
    *,
    composition: ExecutionComposition,
    consent_coverage: ConsentCoverage | None = None,
) -> AttemptAuthority:
    """The one construction of ``MapRunner.authority``: every
    dispatch path that can execute a routed run — the queued ``project.run``
    handler below and every direct v1 action's MapRunner factory
    (``engine/executor/actions.py``, ``server/mcp/backends.py``) — builds this
    SAME authority, closed over ``project``, so a routed run is admitted
    identically (exact-consent match, then ``evaluate_set`` promise-violation
    halt) regardless of whether it was queued or dispatched directly (e.g.
    ``run.backfill``).

    It inherits ``build_route_verification_check``'s seat unchanged — the
    difference is that it RETURNS the artifact instead of raising-or-returning
    ``None``, so the head it verified is the head dispatch binds. That is what
    lets ``bound_route_id`` and the second head read go (B2)."""

    return AttemptAuthority(
        project, composition=composition, consent_coverage=consent_coverage
    )


def verify_execution_route(
    project: Project,
    run_id: int,
    spec: dict[str, Any],
    *,
    composition: ExecutionComposition,
) -> None:
    """Claim-time admission for one routed project run, as a bare assertion.

    A thin delegation to :func:`frisket.execution.attempt_authority.
    admit_routed` — the ONE implementation — for callers that want the
    refusal without the artifact. Raises ``ExecutionRouteVerificationFailed``
    (``consent_missing`` / ``no_live_target``) or
    ``RecipeInvocationHalt('promise_violation')`` exactly as dispatch would.
    """
    from frisket.engine.runner import validation
    from frisket.engine.executor.map_rows_action import typed_program_from_runner_spec
    from frisket.execution.attempt_authority import admit_routed

    recipe = typed_program_from_runner_spec(project, spec)
    if recipe is None:
        try:
            recipe = validation.recipe_for_spec(spec)
        except Exception:  # noqa: BLE001 — unknown recipes fail in the runner's own path
            return
    if not recipe.consumes_resolution:
        return
    run_store = RunResultStore(project)
    scope = (
        tuple(run_store.run_row_scope(run_id))
        if run_store.has_run_row_scope(run_id)
        else None
    )
    admit_routed(
        project,
        run_id,
        spec,
        composition=composition,
        recipe=recipe,
        scope=scope,
    )


# The typed resumable provider vocabulary is declared ONCE, in
# frisket.llm.remediation.RESUMABLE_PROVIDER_ERROR_DETAILS, next to the
# classifier that produces these row-level error_codes — this handler only
# promotes the first matching row to the run-level ActionError, reusing that
# mapping's per-code retry/resume metadata (provider_rate_limited retries;
# provider_key_exhausted / invalid_provider_key need a funded or fixed key
# first, so they are resumable but not retryable).
_RESUMABLE_PROVIDER_ERROR_CODES = tuple(sorted(RESUMABLE_PROVIDER_ERROR_DETAILS))


def _resumable_provider_error(
    project: Project, *, run_id: int, action_kind: str, spec: dict[str, Any]
) -> ActionError | None:
    placeholders = ",".join("?" for _ in _RESUMABLE_PROVIDER_ERROR_CODES)
    row = project.db.execute(
        "SELECT error, error_code FROM results "
        f"WHERE run_id=? AND error_code IN ({placeholders}) "
        "ORDER BY row_id, column_id LIMIT 1",
        (run_id, *_RESUMABLE_PROVIDER_ERROR_CODES),
    ).fetchone()
    if row is None:
        return None
    code = str(row["error_code"])
    model = str(spec.get("model") or "")
    provider = model.split("/", 1)[0] if "/" in model else None
    details: dict[str, Any] = dict(RESUMABLE_PROVIDER_ERROR_DETAILS[code])
    if provider:
        details["provider"] = provider
    return ActionError(
        code=code,
        message=str(row["error"] or "provider capacity is temporarily unavailable"),
        action_kind=action_kind,
        field="params.model",
        details=details,
    )


def _org_local_server_env_bundle(
    env: dict[str, str] | None = None,
) -> LocalModelEndpointConfig | None:
    """The local-server endpoint is operator DEPLOYMENT INFRASTRUCTURE —
    same category as ``FRISKET_MODELS_URL``, which every org run already
    reads straight from the process env regardless of ``use_env_keys`` — not
    a tenant credential. Org runs get the whole validated env endpoint
    — origin AND tokens AND edge_auth — atomically: a bundle
    with tokens but an invalid/missing origin is rejected as a unit (an env
    token may never ride a non-env origin), same rule as the local tier's
    own env-bundle resolution. Returns None when no env bundle is a
    candidate at all or it was rejected — callers receive an empty endpoint
    authority. API keys are untouched by this function and
    remain excluded for org runs exactly as before; this is ONLY the
    local-model endpoint bundle crossing the ``use_env_keys=False`` gate.
    """
    # Config loading only — no HTTP machinery rides in through this import.
    from frisket.server.provider_config import resolve_env_local_endpoint

    config, _notes = resolve_env_local_endpoint(os.environ if env is None else env)
    return config


def resolve_run_local_endpoints(
    root: str | Path,
    *,
    org_id: int | str | None,
    env: dict[str, str] | None = None,
) -> tuple[LocalModelEndpointConfig, ...]:
    """Plural execution authority; org runs receive only operator endpoints."""

    if org_id is not None:
        config = _org_local_server_env_bundle(env)
        return (config,) if config is not None else ()
    from frisket.server.provider_config import resolve_local_endpoints

    configs, _notes = resolve_local_endpoints(root, env)
    return configs


def resolve_run_keys(
    workspace_root: str | Path,
    *,
    org_id: int | str | None,
    credentials: Any,
    control_database_url: str | None,
) -> tuple[dict[str, str] | None, str]:
    """The provider keys a queued run's router starts from, plus their
    non-secret provenance label (worker-local-provider-keys-v1).

    Hosted (org_id + control DB): org BYOK rows through the credentials port,
    resolved at claim time — plaintext never rides the queue payload.
    Local tier: the UI-entered workspace provider-key file, with env still
    winning per ``resolve_effective_keys`` — the same layer the
    server process already gives the copilot/previews, without which a run
    fails per-row with the missing-key remediation while the copilot works.
    """
    if org_id is not None and control_database_url:
        keys = dict(
            credentials.provider_keys(
                org_id=int(org_id),
                control_database_url=control_database_url,
            )
        )
        return keys, "org_byok"
    if org_id is not None:
        # No control-plane read happened, so no returned key can honestly be
        # labeled org BYOK. The only possible value is operator deployment
        # infrastructure from the environment. Calling it ``org_byok`` made a
        # paid operator credential look like the zero-rated lane when a
        # malformed hosted worker omitted its control DB.
        return None, "local"
    # Config loading only — no HTTP machinery rides in through this import.
    from frisket.server.provider_config import resolve_effective_keys

    keys = resolve_effective_keys(workspace_root)
    return (keys or None), "local"


def _router_for(
    project: Project,
    router: ModelRouter | None,
    *,
    keys: dict[str, str] | None = None,
    keys_source: str = "org_byok",
    use_env_keys: bool = True,
    cache_mode: CacheMode | None = None,
    local_endpoints: tuple[LocalModelEndpointConfig, ...] | None = None,
    response_cache: ResponseCache | None | object = _RESPONSE_CACHE_UNSET,
) -> ModelRouter:
    """Compose the exact router authority for one queued execution.

    An injected router owns its cache posture. The queued handler always passes
    the enqueue-frozen ``cache_mode`` when it constructs a local router; direct
    non-queued callers retain the system's explicit replay default.
    """
    project_keys = _project_model_keys(project)
    # Endpoint resolution is an override only when it changes the injected
    # router's endpoint authority.  The queued handler always supplies the
    # freshly resolved tuple, including ``()`` for the overwhelmingly common
    # no-local-model case.  Treating that equal empty tuple as a composition
    # layer rebuilt ``ModelRouter`` and silently discarded injected adapters,
    # keys, subclass behaviour, cache posture, and other runtime state.
    #
    # Equality, rather than truthiness, is the important fence: an explicit
    # empty tuple MUST still rebuild a router that currently carries local
    # endpoints, so removed endpoint authority cannot survive into execution.
    endpoint_authority_unchanged = local_endpoints is None or (
        router is not None and local_endpoints == router.local_endpoints
    )
    if (
        router is not None
        and not project_keys
        and not keys
        and endpoint_authority_unchanged
        and response_cache is _RESPONSE_CACHE_UNSET
    ):
        return router
    if project_keys or keys or router is not None:
        base_keys = router.configured_keys() if router is not None else {}
        base_sources = router.configured_key_sources() if router is not None else {}
        merged_keys = {
            **base_keys,
            **(keys or {}),
            **project_keys,
        }
        # The injected router's own keys keep the provenance ITS composer
        # declared. This used to default to ``platform_key`` for anything the
        # router had not declared, which is a fact this function cannot know:
        # in a self-hosted deployment the injected router's keys are the
        # operator's OWN env material ("local"), and stamping "platform_key"
        # on them put a false credential_source into every provider fact the
        # run persists. It read as correct only because the hosted
        # composition happens to be the only injector today. So the source is
        # REQUIRED of the injector rather than guessed here (bug pattern #1).
        undeclared = (
            sorted(p for p in base_keys if not router.has_explicit_key_source(p))
            if router is not None
            else []
        )
        if undeclared:
            raise ValueError(
                "an injected router must declare the provenance of every "
                "provider key it carries; none was declared for "
                f"{', '.join(undeclared)}. Construct it with `key_sources=` "
                "(per provider) or `env_key_source=` (for env-held keys) — "
                "this run stamps that token onto its durable provider facts "
                "and must not invent it."
            )
        sources = {
            **{provider: base_sources[provider] for provider in base_keys},
            # keys_source is the caller's provenance for the injected layer:
            # org BYOK rows in hosted, the workspace key file ("local") in the
            # local tier (worker-local-provider-keys-v1).
            **dict.fromkeys(keys or {}, keys_source),
            **dict.fromkeys(project_keys, "project_key"),
        }
        cache = (
            cast(ResponseCache | None, response_cache)
            if response_cache is not _RESPONSE_CACHE_UNSET
            else (
                router.cache
                if router is not None
                else ResponseCache(project.path / "project.cache.db")
            )
        )
        effective_cache_mode = (
            router.cache_mode if router is not None else cache_mode or "replay"
        )
        return ModelRouter(
            keys=merged_keys,
            key_sources=sources,
            cache=cache,
            cache_mode=effective_cache_mode,
            chaos=(
                router.chaos.config
                if router is not None and router.chaos is not None
                else None
            ),
            max_retries=router.max_retries if router is not None else 3,
            use_env_keys=use_env_keys,
            local_endpoints=(
                router.local_endpoints
                if local_endpoints is None and router is not None
                else local_endpoints or ()
            ),
            model_call_policy=(
                router.model_call_policy if router is not None else None
            ),
        )
    cache = (
        cast(ResponseCache | None, response_cache)
        if response_cache is not _RESPONSE_CACHE_UNSET
        else ResponseCache(project.path / "project.cache.db")
    )
    return ModelRouter(
        keys=keys,
        key_sources=dict.fromkeys(keys or {}, "local"),
        env_key_source="local",
        cache=cache,
        cache_mode=cache_mode or "replay",
        use_env_keys=use_env_keys,
        local_endpoints=local_endpoints or (),
    )


def project_scoped_router(project: Project, router: ModelRouter | None) -> ModelRouter:
    """THE way an executor composes a router for one project's work.

    Every map-runner factory used to spell this ``router or ModelRouter()``.
    A bare ``ModelRouter`` reads the process env and nothing else, so a
    project whose provider key was configured in Settings > AI Providers had
    that key silently bypassed on every router-less entry point (``frisket
    action run``, the plan runner, previews): the call ran on the
    deployment's env key instead, spending outside the per-key spend cap the
    journalist set and stamping ``credential_source="local"`` on the durable
    fact. The stamp was HONEST — that really is the key that paid — which is
    exactly why the defect survived: nothing was lying, the work was just
    routed to the wrong credential.

    Layering through :func:`_router_for` makes ``project.provider_model_keys``
    the one credential authority for both entry points, and routes the
    composition through the provenance guard below (a bare ``ModelRouter``
    never passed through it). ``env_key_source="local"`` is what the fallback
    router DECLARES about its own env keys — the same token it already
    reported by default, now stated rather than assumed.

    A router the caller supplied is returned untouched only when no explicit
    project/key/endpoint authority must be layered onto it. Queued execution
    always supplies its claim-time endpoint collection, including an explicit
    empty tuple, so it is reconstructed and cannot retain stale endpoints.

    Caching is untouched: the fallback carries no ``ResponseCache``, exactly
    as the bare ``ModelRouter`` did not.
    """
    if router is not None:
        return router
    return _router_for(project, ModelRouter(env_key_source="local"))


def _project_model_keys(project: Project) -> dict[str, str]:
    # Per-project provider keys decrypt through the store seam
    # (Project.provider_model_keys) — the run worker never decrypts inline.
    return project.provider_model_keys()


def _run_params(project: Project, run_id: int) -> dict[str, Any]:
    row = RunResultStore(project).get_run(run_id)
    if row is None:
        return {}
    try:
        params = json.loads(row["params"] or "{}")
    except json.JSONDecodeError:
        return {}
    return params if isinstance(params, dict) else {}


def _deferred_publication(project: Project, run_id: int) -> bool:
    """Read the admitted preparation decision, not the mutable job payload."""
    row = project.db.execute(
        "SELECT ops.spec FROM runs JOIN ops ON ops.id=runs.op_id WHERE runs.id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError("queued run has no durable operation")
    return json.loads(row["spec"]).get("deferred_publication") is True


def register_project_run_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    router: ModelRouter | None = None,
    control_database_url: str | None = None,
    worker_ports: WorkerPorts | None = None,
    require_storage_identity: bool = False,
    workspace_root_storage_org_id: int | None = None,
    executor_deps_factory: Any | None = None,
    project_opener: ProjectOpener | None = None,
    execution_router_factory: Callable[[], ModelRouter] | None = None,
    response_cache_factory: Callable[[JobHandlerContext], ResponseCache | None]
    | None = None,
    execution_composition_factory: ExecutionCompositionFactory | None = None,
) -> HandlerRegistration:
    """Register the project-run handler.

    Edition policy arrives as ports (`worker_ports`), never as an import: the
    open default admits every run, settles nothing, and resolves org BYOK keys
    from the open identity control plane. An external composition injects its
    own admission + settlement ports.
    """

    ports = coerce_worker_ports(worker_ports)
    credentials = ports.credentials()
    admission = ports.admission_port
    settlement = ports.settlement_port
    root = Path(workspace_root)
    composition_factory = execution_composition_factory or open_execution_composition

    def handle(payload: dict, handler_context: JobHandlerContext) -> dict:
        project_id, _project_root, project_path = claimed_project_location(
            payload,
            workspace_root=root,
            require_storage_identity=require_storage_identity,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
        )
        run_id = int(payload["run_id"])
        project = (
            Project(project_path)
            if project_opener is None
            else open_claimed_project(payload, project_path, project_opener)
        )
        org_id = payload.get("org_id")
        raw_cache_mode = payload["v1_cache_mode"]
        if raw_cache_mode not in CACHE_MODES:
            raise ValueError(f"queued run has invalid cache mode {raw_cache_mode!r}")
        queued_cache_mode: CacheMode = raw_cache_mode
        db_url = control_database_url or os.environ.get("FRISKET_DATABASE_URL")
        keys, keys_source = resolve_run_keys(
            root,
            org_id=org_id,
            credentials=credentials,
            control_database_url=db_url,
        )
        key_providers = set(keys or {})

        def settle() -> None:
            """Hand the run's outcome to the settlement port, if any. The open
            editions have none — they meter nothing and charge nobody."""
            if settlement is None:
                return
            terminal_row = run_store.get_run(run_id)
            terminal_status = (
                None if terminal_row is None else str(terminal_row["status"])
            )
            if terminal_status not in {"completed", "failed", "cancelled"}:
                raise RuntimeError(
                    "settlement requires the run's durable terminal posture; "
                    f"run {run_id} is {terminal_status!r}"
                )
            settlement.settle_run(
                project=project,
                project_id=project_id,
                run_id=run_id,
                trusted_job_org_id=handler_context.trusted_job_org_id,
                control_database_url=db_url,
                key_providers=key_providers,
                terminal_status=terminal_status,
            )

        run_store = RunResultStore(project)
        # Stamp the claiming process's code identity onto the run record itself
        # so the run-detail panel can show it — independent of the queue-level
        # enqueue/claim mismatch
        # check in jobs/worker.py, which only compares job rows.
        run_store.set_worker_version(run_id, code_version())

        def cancelled(rid: int) -> bool:
            return run_store.cancellation_requested(rid)

        defer_attempt_close = False
        progress = None
        claim_token: str | None = None
        handler_owned_cache: ResponseCache | None = None
        action_envelope = None
        try:
            row = run_store.get_run(run_id)
            if row is not None and row["status"] != "running":
                settle()
                result = {
                    "project_id": project_id,
                    "run_id": run_id,
                    "status": row["status"],
                    "total": row["total_rows"],
                    "completed": row["completed_rows"],
                    "failed": row["failed_rows"],
                    "skipped": True,
                }
                action_result = queued_v1_finalize_action_result(
                    project,
                    payload,
                    project_id=project_id,
                    run_id=run_id,
                )
                if action_result is not None:
                    result["action_result"] = action_result.model_dump(mode="json")
                return result
            raw_endpoint_bindings = payload["v1_local_endpoint_bindings"]
            if not isinstance(raw_endpoint_bindings, list):
                raise ValueError("queued local endpoint bindings must be a list")
            run_local_endpoints = resolve_run_local_endpoints(root, org_id=org_id)
            endpoints_by_id = {
                endpoint.endpoint_id: endpoint for endpoint in run_local_endpoints
            }
            # The outer payload, v1 action, runner spec, and private marker
            # must name one canonical action before edition policy or runner
            # lookup sees the job.
            pre_run_error = queued_v1_identity_error(payload, project=project)
            action_kind = str(payload.get("action_kind") or "")
            raw_edition_snapshot = None if row is None else row["edition_run_context"]
            if pre_run_error is None:
                pre_run_error = _queued_action_edition_error(
                    action_kind,
                    raw_edition_snapshot,
                )
            for binding in raw_endpoint_bindings if pre_run_error is None else ():
                if not isinstance(binding, dict) or set(binding) != {
                    "endpoint_id",
                    "origin",
                }:
                    raise ValueError("queued local endpoint binding is malformed")
                endpoint = endpoints_by_id.get(binding["endpoint_id"])
                if endpoint is None:
                    pre_run_error = ActionError(
                        code="local_endpoint_unavailable",
                        message=(
                            "The queued local model endpoint no longer exists: "
                            f"{binding['endpoint_id']}"
                        ),
                        action_kind=str(payload.get("action_kind") or RUN_PROJECT_KIND),
                        details={"endpoint_id": binding["endpoint_id"]},
                    )
                    break
                if endpoint.origin != binding["origin"]:
                    pre_run_error = ActionError(
                        code="local_endpoint_changed",
                        message=(
                            "The queued local model endpoint changed before execution: "
                            f"{binding['endpoint_id']}"
                        ),
                        action_kind=str(payload.get("action_kind") or RUN_PROJECT_KIND),
                        details={
                            "endpoint_id": binding["endpoint_id"],
                            "queued_origin": binding["origin"],
                            "current_origin": endpoint.origin,
                        },
                    )
                    break
            if (
                pre_run_error is None
                and admission is not None
                and admission.execution_denied(body=payload)
            ):
                pre_run_error = ActionError(
                    code="code_action_disabled",
                    message=(
                        "code-execution actions are disabled on the hosted tier; "
                        "run frisket locally to use them"
                    ),
                    action_kind=str(payload.get("action_kind") or "project.run"),
                )
            if pre_run_error is None:
                pre_run_error = queued_v1_pre_run_error(project, payload)
            if pre_run_error is not None:
                # Preparation can cooperatively stop its child before dispatch.
                # Its transport error must not override durable cancellation.
                cancel_requested = cancelled(run_id)
                terminal_status = "cancelled" if cancel_requested else "failed"
                action_result = queued_v1_terminal_receipt_result(
                    project,
                    payload,
                    project_id=project_id,
                    run_id=run_id,
                    status=terminal_status,
                    error=None if cancel_requested else pre_run_error,
                    never_dispatched=True,
                )
                settle()
                return {
                    "project_id": project_id,
                    "run_id": run_id,
                    "status": terminal_status,
                    "total": row["total_rows"] if row is not None else 0,
                    "completed": row["completed_rows"] if row is not None else 0,
                    "failed": row["failed_rows"] if row is not None else 0,
                    "action_result": action_result.model_dump(mode="json"),
                }
            spec = dict(payload["spec"])
            from frisket.engine.runner import validation

            action_envelope = queued_v1_payload_envelope(payload, project=project)
            program = None if action_envelope is None else action_envelope.program
            bind_project = getattr(program, "bind_project", None)
            if callable(bind_project):
                bind_project(project)
            recipe = program or validation.recipe_for_spec(spec)
            # The queued handler no longer preloads the route
            # binding. That pre-load (plus its own RouteBindingUnavailable ->
            # no_live_target mapping) was the origin of the second head read:
            # the handler bound one head, the fence verified another, and
            # `bound_route_id` was threaded through three signatures to
            # reconcile them. The authority now reads the head once, derefs
            # once, and owns the typed mapping; the binding rides ctx.extras
            # as part of the attempt.
            admitted_attempt_id = None
            claim_token = (
                f"output-claim:{payload['v1_receipt_id']}"
                if isinstance(payload.get("v1_receipt_id"), str)
                and payload["v1_receipt_id"]
                else None
            )
            durable_output_fields = None
            if claim_token is not None:
                from frisket.engine.store.output_claims import OutputColumnClaimStore

                durable_output_fields = OutputColumnClaimStore(
                    project
                ).active_output_fields(
                    claim_token=claim_token,
                    run_id=run_id,
                    require_frozen_descriptors=True,
                )
            if recipe.consumes_resolution:
                admitted_attempt_id = queued_v1_prepared_attempt_id(
                    project,
                    payload,
                    run_id=run_id,
                )
                if admitted_attempt_id is None:
                    raise ExecutionRouteVerificationFailed(
                        "stale_head",
                        "the queued receipt does not name one exact admitted "
                        "attempt for this run",
                    )
                if claim_token is None:
                    raise ExecutionRouteVerificationFailed(
                        "stale_head",
                        "the queued receipt does not name its output claim token",
                    )
            base_router = (
                execution_router_factory()
                if execution_router_factory is not None
                else router
            )
            cache_override: ResponseCache | None | object = _RESPONSE_CACHE_UNSET
            if response_cache_factory is not None:
                handler_owned_cache = response_cache_factory(handler_context)
                cache_override = handler_owned_cache
            effective_router = _router_for(
                project,
                base_router,
                keys=keys,
                keys_source=keys_source,
                use_env_keys=org_id is None,
                local_endpoints=run_local_endpoints,
                cache_mode=queued_cache_mode,
                response_cache=cache_override,
            )
            claimed_storage_key = payload.get(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
            edition_snapshot = (
                None
                if raw_edition_snapshot is None
                else json.loads(raw_edition_snapshot)
            )
            execution_context = ExecutionCompositionContext(
                storage_key=(
                    claimed_storage_key
                    if isinstance(claimed_storage_key, ProjectStorageKey)
                    else None
                ),
                run_id=run_id,
                trusted_job_org_id=handler_context.trusted_job_org_id,
                edition_snapshot=edition_snapshot,
            )
            execution_composition = composition_factory(
                project, effective_router, execution_context
            )
            actor_row = project.db.execute(
                "SELECT consent_principal FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            principal = (
                actor_row["consent_principal"] if actor_row is not None else None
            )
            if principal is None and isinstance(
                handler_context.trusted_job_org_id, int
            ):
                raise ExecutionRouteVerificationFailed(
                    "consent_missing", "queued run has no admitted user identity"
                )
            if principal is not None:
                from frisket.engine.store.execution_routes import instance_principal

                installation = instance_principal(project)
                # Portable run/consent rows can agree on a foreign actor.
                # Only this installation's admitted identities authorize egress.
                if principal != installation and not principal.startswith(
                    f"{installation}:user:"
                ):
                    raise ExecutionRouteVerificationFailed(
                        "consent_missing",
                        "queued run consent belongs to another installation",
                    )
            consent_coverage = (
                ConsentCoverage(principal, Decimal("0")) if principal else None
            )
            authority = build_attempt_authority(
                project,
                composition=execution_composition,
                consent_coverage=consent_coverage,
            )
            executor_deps = (
                executor_deps_factory(project_id, None)
                if executor_deps_factory is not None
                else None
            )
            runner = MapRunner(
                project,
                effective_router,
                consent_coverage=consent_coverage,
                should_cancel=cancelled,
                defer_cancel_terminal_close=True,
                op_context_extras={
                    "url_capture_browser": (
                        executor_deps.url_capture_browser
                        if executor_deps is not None
                        else None
                    )
                },
                allow_action_lifecycle_only_recipes=(
                    queued_v1_run_authorizes_action_lifecycle(
                        project,
                        payload,
                        run_id=run_id,
                    )
                ),
                authority=authority,
                execution_composition=execution_composition,
            )
            action_kind = str(payload.get("action_kind") or "")
            deferred_publication = _deferred_publication(project, run_id)
            defer_attempt_close = bool(
                action_kind == "join.semantic" or deferred_publication
            )
            progress = asyncio.run(
                runner.run(
                    spec,
                    program=program,
                    confirmed=True,
                    resume_run_id=run_id,
                    reopen_operator_cancelled=False,
                    claim_token=claim_token,
                    admitted_attempt_id=admitted_attempt_id,
                    precomputed_output_fields=durable_output_fields,
                    defer_attempt_close=defer_attempt_close,
                    defer_generation_seal=deferred_publication,
                )
            )
            payload["spec"] = dict(spec)
            if action_envelope is not None:
                action_envelope = replace(
                    action_envelope, runner_spec=dict(payload["spec"])
                )
            provider_error = _resumable_provider_error(
                project,
                run_id=progress.run_id,
                action_kind=str(payload.get("action_kind") or RUN_PROJECT_KIND),
                spec=spec,
            )
            if provider_error is not None:
                if not defer_attempt_close:
                    run_store.finish_run(progress.run_id, "failed")
                action_result = queued_v1_terminal_failure_result(
                    project,
                    payload,
                    project_id=project_id,
                    run_id=progress.run_id,
                    error=provider_error,
                    writer_attempt_id=(
                        progress.writer_attempt_id if defer_attempt_close else None
                    ),
                    claim_token=(claim_token if defer_attempt_close else None),
                )
                settle()
                return {
                    "project_id": project_id,
                    "run_id": progress.run_id,
                    "status": "failed",
                    "completed": progress.completed,
                    "failed": progress.failed,
                    "action_result": action_result.model_dump(mode="json"),
                }
            row = run_store.get_run(run_id)
            cooperative_cancel = bool(
                progress.cancel_requested
                and progress.cancelled
                and progress.completed < progress.total
            )
            if cooperative_cancel:
                action_result = queued_v1_terminal_receipt_result(
                    project,
                    payload,
                    project_id=project_id,
                    run_id=progress.run_id,
                    status="cancelled",
                    writer_attempt_id=(
                        progress.writer_attempt_id
                        if progress.writer_attempt_claimed
                        else None
                    ),
                    claim_token=(
                        claim_token if progress.writer_attempt_claimed else None
                    ),
                )
                row = run_store.get_run(run_id)
                settle()
            else:
                action_result = queued_v1_finalize_action_result(
                    project,
                    payload,
                    project_id=project_id,
                    run_id=progress.run_id,
                    envelope=action_envelope,
                    writer_attempt_id=(
                        progress.writer_attempt_id if defer_attempt_close else None
                    ),
                    claim_token=(claim_token if defer_attempt_close else None),
                )
                settle()
            result = {
                "project_id": project_id,
                "run_id": progress.run_id,
                "status": row["status"] if row else "unknown",
                "completed": progress.completed,
                "failed": progress.failed,
            }
            if action_result is not None:
                result["action_result"] = action_result.model_dump(mode="json")
            return result
        except StaleAttemptWriter as exc:
            # The provider may already have charged this losing invocation.
            # Complete the queue invocation without retrying or touching the
            # replacement attempt, run, receipt, or active claim.
            row = run_store.get_run(run_id)
            return {
                "project_id": project_id,
                "run_id": run_id,
                "status": exc.code,
                "total": row["total_rows"] if row is not None else 0,
                "completed": row["completed_rows"] if row is not None else 0,
                "failed": row["failed_rows"] if row is not None else 0,
                "error": {"code": exc.code, "message": str(exc)},
            }
        except ClaimLeaseRenewalFailed:
            # The effect batch rolled back with its failed lease renewal.
            # Authority may still be live, so leave the shared tuple untouched
            # and let the queue retry this invocation.
            raise
        except ExecutionRouteVerificationFailed as exc:
            # Worker verification: consent_missing /
            # no_live_target are DURABLE, honest failures — nothing executed,
            # the receipt records the remedy, and the terminal receipt path
            # releases any output-column claims. The queue job itself
            # completes with a failed result (mirroring the pre_run_error
            # path), so nothing retries into the same refusal.
            row = run_store.get_run(run_id)
            action_result = queued_v1_terminal_failure_result(
                project,
                payload,
                project_id=project_id,
                run_id=run_id,
                error=ActionError(
                    code=exc.code,
                    message=str(exc),
                    action_kind=str(payload.get("action_kind") or RUN_PROJECT_KIND),
                ),
                never_dispatched=True,
            )
            settle()
            row = run_store.get_run(run_id)
            return {
                "project_id": project_id,
                "run_id": run_id,
                "status": "failed",
                "total": row["total_rows"] if row is not None else 0,
                "completed": row["completed_rows"] if row is not None else 0,
                "failed": row["failed_rows"] if row is not None else 0,
                "action_result": action_result.model_dump(mode="json"),
            }
        except Exception as exc:
            if (
                defer_attempt_close
                and progress is not None
                and progress.writer_attempt_id is not None
            ):
                # The paid terminal materializer/checkpoint transaction
                # failed and rolled back. Keep its writer/claim/receipt tuple
                # live for reconciliation; generic cleanup would release the
                # authority that prevents the external effect being bought
                # again.
                raise
            if _has_live_managed_project_run_writer(project, run_id):
                # An unexpected failure after the dispatch CAS may have
                # followed a provider/result effect. Do not mark the run
                # failed first and then ask the non-owner terminalizer to
                # release its claim: preserve run + dispatching attempt + open
                # generation + claim for the existing reconciliation path.
                raise
            try:
                row = run_store.get_run(run_id)
                terminalized_teardown = bool(
                    isinstance(exc, SandboxTeardownError)
                    and getattr(exc, "run_id", None) == run_id
                    and row
                    and row["status"] == "cancelled"
                    and persisted_recipe_invocation_halt(row["params"]) is not None
                )
                if terminalized_teardown:
                    # MapRunner already committed the allowlisted resumable
                    # halt atomically. Preserve the action-specific typed
                    # receipt, then re-raise so the queue job still records
                    # the strict infrastructure failure and never auto-retries.
                    settle()
                    queued_v1_finalize_action_result(
                        project,
                        payload,
                        project_id=project_id,
                        run_id=run_id,
                        envelope=action_envelope,
                    )
                else:
                    if row and row["status"] == "running":
                        run_store.finish_run(run_id, "failed")
                    settle()
                    queued_v1_worker_exception_failure_result(
                        project,
                        payload,
                        project_id=project_id,
                        run_id=run_id,
                        exc=exc,
                    )
            except Exception as cleanup_exc:  # noqa: BLE001
                exc.add_note(
                    "failed to mark run as failed after handler error: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
            raise
        finally:
            if handler_owned_cache is not None:
                # The factory transfers ownership to this one invocation. A
                # close failure must not turn a completed provider effect into
                # a retried queue job.
                with contextlib.suppress(Exception):
                    handler_owned_cache.close()
            project.close()

    return registry.add(
        RUN_PROJECT_KIND,
        handle,
        origin="frisket.production.project_run",
    )


def register_action_run_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    router: ModelRouter | None = None,
    control_database_url: str | None = None,
    worker_ports: WorkerPorts | None = None,
    require_storage_identity: bool = False,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    """Register the generic ``action.run`` worker handler (queued_action_job).

    Unlike ``project.run`` there is no run id to resume: the handler decodes the
    ActionJobEnvelope, dispatches to the action's registered executor, and
    terminalizes the reserved (run_id=None) receipt.

    The admission port (if the composition injected one) decides whether an
    action may execute at all; the open editions admit everything. A terminal
    runless receipt is handed to the same edition settlement port used for
    project runs.
    """

    from frisket.engine.executor.action_jobs import (
        action_job_failure_result,
        action_job_result_payload,
        action_run_envelope_from_payload,
        reconcile_exhausted_action_run_jobs,
        run_action_run_job,
    )
    from frisket.engine.jobs.queue import ACTION_RUN_KIND

    root = Path(workspace_root)
    _ = router  # executors that need a router pull it from their own ctx/gateway
    ports = coerce_worker_ports(worker_ports)
    admission = ports.admission_port
    settlement = ports.settlement_port

    def handle(payload: dict, handler_context: JobHandlerContext) -> dict:
        project_id, _project_root, project_path = claimed_project_location(
            payload,
            workspace_root=root,
            require_storage_identity=require_storage_identity,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
        )
        project = (
            Project(project_path)
            if project_opener is None
            else open_claimed_project(payload, project_path, project_opener)
        )
        try:
            envelope = action_run_envelope_from_payload(payload)
            if envelope is not None and admission is not None:
                decision_body = dict(envelope.action)
                decision_body.setdefault("action_kind", envelope.action_kind)
                action_capabilities = decision_body.get("capabilities")
                capabilities = {
                    str(capability)
                    for capability in (
                        action_capabilities
                        if isinstance(action_capabilities, list)
                        else ()
                    )
                }
                capabilities.update(str(item) for item in envelope.capabilities)
                decision_body["capabilities"] = sorted(capabilities)
                decision_body["resolved_snapshot"] = envelope.resolved_snapshot
                if admission.execution_denied(body=decision_body):
                    result = action_job_failure_result(
                        project,
                        project_id=envelope.project_id,
                        receipt_id=envelope.receipt_id,
                        action_kind=envelope.action_kind,
                        error=ActionError(
                            code="code_action_disabled",
                            message=(
                                "code-execution actions are disabled on the hosted "
                                "tier; run frisket locally to use them"
                            ),
                            action_kind=envelope.action_kind,
                        ),
                        job_id=(
                            int(payload["job_id"])
                            if isinstance(payload.get("job_id"), int)
                            else None
                        ),
                    )
                else:
                    result = run_action_run_job(
                        project,
                        dict(payload),
                        executor_lookup=registry.action_executor,
                    )
            else:
                result = run_action_run_job(
                    project,
                    dict(payload),
                    executor_lookup=registry.action_executor,
                )
            if envelope is not None and settlement is not None:
                stored = ReceiptStore(project).find_by_id(envelope.receipt_id)
                if (
                    stored is not None
                    and stored.parsed().status in FINISHED_RECEIPT_STATUSES
                ):
                    settlement.settle_action_receipt(
                        project=project,
                        project_id=project_id,
                        receipt_id=envelope.receipt_id,
                        trusted_job_org_id=handler_context.trusted_job_org_id,
                        control_database_url=(
                            control_database_url
                            or os.environ.get("FRISKET_DATABASE_URL")
                        ),
                    )
        finally:
            project.close()
        return action_job_result_payload(result)

    registration = registry.add(
        ACTION_RUN_KIND,
        handle,
        origin="frisket.production.action_run",
    )
    registry.register_recovery_hook(
        lambda queue: reconcile_exhausted_action_run_jobs(
            queue,
            workspace_root=root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
            project_opener=project_opener,
        )
    )
    return registration
