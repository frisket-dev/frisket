"""Workspace ownership for the local HTTP server."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import HTTPException, Request
from filelock import FileLock

from frisket.engine.executor import ExecutorDeps
from frisket.execution.provider import (
    ExecutionComposition,
    ExecutionCompositionContext,
    ExecutionCompositionFactory,
    open_execution_composition,
)
from frisket.engine.jobs import (
    HandlerRegistry,
    JobQueue,
    WorkerPorts,
    default_registry,
    open_queue,
    register_production_handlers,
)
from frisket.project_identity import ProjectStorageKey
from frisket.project_opener import (
    ProjectOpener,
    require_opener_storage_org_id,
)
from frisket.ai.llm import CacheMode, ModelRouter, ResponseCache
from frisket.server.runtime_settings import (
    resolve_workspace_cache_mode,
    resolve_workspace_cost_preapproval_usd,
    save_workspace_cache_mode,
    save_workspace_cost_preapproval_usd,
)
from frisket.server.notifications.delivery import (
    NotificationDeliveryRuntime,
    default_delivery_runtime,
)
from frisket.server.notifications.secrets import NotificationSecretResolver
from frisket.project_identity import validate_project_slug
from frisket.authoring.recipe_registry import ActionRegistryStore
from frisket.authoring.action_metadata import PRODUCT_EDITION_SNAPSHOT_KEY
from frisket.engine.runner import RunProgress
from frisket.server.route_errors import RouteError
from frisket.engine.store import BundleSchemaMismatch, Project, delete_bundle
from frisket.engine.store.blob_backend import ProjectBlobStore
from frisket.authoring.workbench.plugin_runtime import (
    bootstrap_project_bundled_plugins,
)
from frisket.authoring.workbench.plugin_package_catalog import (
    plugin_package_lifecycle_lock,
    plugin_package_catalog_for_root,
)
from frisket.authoring.workbench.plugin_runtime_shared import PluginCompositionPolicy
from frisket.authoring.workbench.plugin_subprocess import (
    register_project_secret_fallback,
)


_log = logging.getLogger(__name__)


@runtime_checkable
class DirectActionReceiptSettlementPort(Protocol):
    """Edition-owned settlement after a direct receipt is durable and terminal.

    Unlike the worker ``SettlementPort``, this boundary has no queue row and
    therefore no ``job.org_id`` authority.  The composing edition validates
    its own opaque context from the receipt sidecar; public code supplies only
    the already-open project and the durable receipt identity.
    """

    def settle_action_receipt(
        self,
        *,
        project: Any,
        project_id: str,
        receipt_id: str,
    ) -> None: ...


class DataRootUnwritable(RuntimeError):
    """The configured data root exists but this process cannot write to it."""


def ensure_writable_data_root(root: Path) -> None:
    """Fail loudly, with the fix, when the data root is not writable.

    Not a security boundary — no adversary. This catches one deployment
    misconfiguration: release images run as non-root uid/gid 10001:10001, so
    a /data volume populated by an older, root-running image keeps root
    ownership and every later write dies with a bare EACCES deep inside first
    use. One probe at boot turns that into an actionable message instead.
    """
    probe = root / f".frisket-write-probe-{os.getpid()}"
    try:
        probe.touch()
    except OSError as exc:
        euid = getattr(os, "geteuid", lambda: "unknown")()
        raise DataRootUnwritable(
            f"data root {root} is not writable by this process (uid {euid}): "
            f"{exc}.\n"
            "Frisket release images run as the non-root user frisket "
            "(uid/gid 10001:10001). A data volume created by an older, "
            "root-running image keeps root ownership. Fix ownership once from "
            "the host, then restart:\n"
            "  named volume: docker run --rm -v <frisket-data-volume>:/data "
            "alpine chown -R 10001:10001 /data\n"
            "  bind mount:   sudo chown -R 10001:10001 ./data\n"
            "After fixing ownership, restart Frisket."
        ) from exc
    else:
        try:
            probe.unlink()
        except OSError:
            pass


class Workspace:
    """Holds open projects + per-project runner state."""

    def __init__(
        self,
        root: Path,
        router: ModelRouter | None = None,
        *,
        queue: JobQueue | None = None,
        queue_clock: Callable[[], datetime] | None = None,
        registry: HandlerRegistry | None = None,
        queue_payload_extra: dict[str, Any] | None = None,
        control_database_url: str | None = None,
        executor_deps_factory: Callable[[str, Request], ExecutorDeps] | None = None,
        project_secret_fallback_resolver: Callable[[str, str], str | None]
        | None = None,
        notification_secret_resolver: NotificationSecretResolver | None = None,
        notification_delivery_runtime: NotificationDeliveryRuntime | None = None,
        project_blob_store_factory: Callable[[str], ProjectBlobStore] | None = None,
        project_opener: ProjectOpener | None = None,
        worker_ports: WorkerPorts | None = None,
        install_queue_terminalization: bool = True,
        require_storage_identity: bool = False,
        provider_keys_resolver: Callable[[], Mapping[str, str]] | None = None,
        require_explicit_provider_keys: bool = False,
        enable_local_model_pull: bool = True,
        execution_router_factory: Callable[[], ModelRouter] | None = None,
        execution_composition_factory: ExecutionCompositionFactory | None = None,
        direct_action_receipt_settlement_port: DirectActionReceiptSettlementPort
        | None = None,
        plugin_composition_policy: PluginCompositionPolicy | None = None,
        edition: str = "solo",
    ):
        self.root = root
        self.edition = str(edition).strip().lower()
        self.root.mkdir(parents=True, exist_ok=True)
        ensure_writable_data_root(self.root)
        self._projects: dict[str, Project] = {}
        self._router = router
        # An edition may construct a fresh base router for each execution
        # while deliberately sharing its cache/config. Project-key overlays
        # and the execution-composition factory consume that fresh object;
        # Local/Team omit the factory and retain the existing router paths.
        self._execution_router_factory = execution_router_factory
        # Request-scoped money/consent composition.  Hosted supplies a
        # factory; open builds facts + provider from the exact effective
        # router chosen for this request.  Store the factory, never a result,
        # so a cached Workspace cannot bleed one request's funding/provider
        # view into the next.
        self._execution_composition_factory = (
            execution_composition_factory or open_execution_composition
        )
        # The open/local server's cache-mode authority. FRISKET_CACHE_MODE
        # seeds a fresh workspace; a deliberate Preferences change persists in
        # the workspace and wins on later starts. Injected routers retain their
        # composition-owned mode and are never mutated through this local seam.
        self._local_cache_mode = resolve_workspace_cache_mode(self.root)
        self._local_cost_preapproval_usd = resolve_workspace_cost_preapproval_usd(
            self.root
        )
        self._runtime_settings_lock = threading.RLock()
        self._provider_keys_resolver = provider_keys_resolver
        self.require_explicit_provider_keys = require_explicit_provider_keys
        # ``queue_clock`` reaches the lease-expiry/fencing/liveness lines of a
        # workspace whose queue only exists via this construction (the
        # deterministic-time contract's injected-clock idiom); None keeps real
        # time and an explicitly injected ``queue`` always wins.
        self.queue = queue or open_queue(workspace=self.root, clock=queue_clock)
        self.registry = registry or default_registry()
        self.queue_payload_extra = dict(queue_payload_extra or {})
        self.executor_deps_factory = executor_deps_factory
        if direct_action_receipt_settlement_port is not None and not isinstance(
            direct_action_receipt_settlement_port,
            DirectActionReceiptSettlementPort,
        ):
            raise TypeError(
                "direct action receipt settlement port must satisfy "
                "DirectActionReceiptSettlementPort"
            )
        self.direct_action_receipt_settlement_port = (
            direct_action_receipt_settlement_port
        )
        self.project_secret_fallback_resolver = project_secret_fallback_resolver
        self.project_blob_store_factory = project_blob_store_factory
        self.project_opener = project_opener or self._derived_project_opener()
        register_project_secret_fallback(self.root, project_secret_fallback_resolver)
        self.notification_delivery_runtime = (
            notification_delivery_runtime
            or default_delivery_runtime(secret_resolver=notification_secret_resolver)
        )
        register_production_handlers(
            self.registry,
            workspace_root=self.root,
            queue=self.queue,
            router=self._router,
            control_database_url=control_database_url,
            executor_deps_factory=self.executor_deps_factory,
            notification_delivery_runtime=self.notification_delivery_runtime,
            # Edition ports for this workspace's in-process worker. None for the
            # open/local server (open defaults: no settlement, no admission).
            # An external managed composition injects its settlement +
            # spend-capped credential ports here — the funding
            # reconcile used to be hardwired into the shared handler, so this is
            # where it stays wired for that composition.
            worker_ports=worker_ports,
            require_storage_identity=require_storage_identity,
            # Declared storage posture, not path inference: an external managed
            # composition constructs this Workspace with
            # queue_payload_extra["storage_org_id"]
            # equal to the org whose directory self.root IS
            # (FRISKET_DATA_DIR/projects/<org_id>). Declaring it here lets a
            # claimed ProjectStorageKey resolve against this org-scoped root
            # without re-appending the org id (double-nesting), while claims
            # for other orgs fail closed. Local workspaces have no
            # queue_payload_extra, so this stays None and the registration
            # root keeps its global/unscoped resolution.
            workspace_root_storage_org_id=self.queue_storage_org_id,
            # THE open seam for this workspace's in-process worker: the same
            # callable that Workspace.get() uses for route opens, so a server
            # route, a job handler and a background derivation in this process
            # all open projects exactly one way.
            project_opener=self.project_opener,
            # A multi-tenant composition may own one global terminalization
            # router for the shared queue. In that case no lazily-created
            # per-org Workspace may replace it.
            install_queue_terminalization=install_queue_terminalization,
            plugin_composition_policy=plugin_composition_policy,
            execution_router_factory=self._execution_router_factory,
            execution_composition_factory=self._execution_composition_factory,
        )
        if enable_local_model_pull:
            # Local-tier-only: deliberately
            # NOT part of register_production_handlers (the one function
            # every edition, including an external managed composition,
            # calls) -- gated by the SAME flag that gates the provider-config
            # routes (enable_provider_config, threaded down from create_app),
            # so a composition with the routes structurally absent (team)
            # also structurally lacks the worker handler, not just the route.
            from frisket.engine.jobs.model_pull import register_model_pull_handler

            register_model_pull_handler(
                self.registry, workspace_root=self.root, queue=self.queue
            )
        # This workspace OWNS the plugin package catalog
        # (<root>/.frisket/plugin_packages.db). Seed shipped bundled packages
        # into it and register every catalog package (bundled + previously
        # installed non-bundled) into the process-global registry exactly ONCE,
        # here, instead of re-registering per project on every restart. The
        # The installed composition policy directly governs both catalog
        # seeding and registry rehydration at this authority.
        self.plugin_package_catalog = plugin_package_catalog_for_root(self.root)
        self._saved_recipes_lock = threading.RLock()
        self.action_registry = ActionRegistryStore(self.root)
        # The saved-actions store accepts only the post-reset v1 dialect.
        # keyed (project_id, run_id): run ids are per-project sqlite
        # autoincrements, so bare run_id COLLIDES across projects: cancel on
        # project A could kill project B's run.
        self.active_runs: dict[tuple[str, int], RunProgress] = {}
        # Internal only: public APIs stay run-id centric, but status polling
        # needs an O(1) way to reconcile terminal queue failures with run rows.
        self.run_jobs: dict[tuple[str, int], int] = {}

    def _derived_project_opener(self) -> ProjectOpener | None:
        """The opener implied by this workspace's own declared configuration.

        A workspace that was given a per-project blob-store factory AND a
        declared storage org already fully determines HOW its projects open
        (bundle path + that org's blob store). Deriving the opener here means
        the in-process worker's handlers open exactly the way the route path
        does, without every composition root having to hand-write and inject
        the same closure. An explicitly injected ``project_opener`` always
        wins — a composition with extra policy (deletion tombstones) needs to
        wrap, not merely reproduce, this.
        """
        factory = self.project_blob_store_factory
        declared_org_id = self.queue_storage_org_id
        if factory is None or declared_org_id is None:
            return None

        def open_declared_project(key: ProjectStorageKey, path: Path) -> Project:
            if key.storage_org_id != declared_org_id:
                raise ValueError(
                    f"claimed storage identity (org {key.storage_org_id}) does "
                    f"not belong to this workspace (org {declared_org_id})"
                )
            return Project(path, blob_store=factory(key.project_slug))

        return open_declared_project

    def _open_project(self, project_id: str, path: Path) -> Project:
        """Open one of THIS workspace's projects, through the seam if present.

        A bundle this build's schema does not match is refused by
        ``Project.__init__``; that refusal has to reach the user as its own
        message. The app-level fallback handler answers any unmapped
        exception with a sanitized ``{"detail": "Internal Server Error"}``
        and logs the rest, so without this mapping the one sentence telling
        a journalist what to do about their project would never leave the
        server. Wrapping here rather than in ``get`` covers the hosted
        ``project_opener`` seam too -- it opens the same ``Project``.
        """
        try:
            if self.project_opener is None:
                blob_store = (
                    self.project_blob_store_factory(project_id)
                    if self.project_blob_store_factory is not None
                    else None
                )
                return Project(path, blob_store=blob_store)
            return self.project_opener(
                ProjectStorageKey(
                    storage_org_id=require_opener_storage_org_id(
                        self.queue_storage_org_id
                    ),
                    project_slug=project_id,
                ),
                path,
            )
        except BundleSchemaMismatch as exc:
            # 409, not 404: the project is really there and really the
            # user's. Saying "no such project" about data sitting on their
            # disk is the mangling, in message form.
            raise RouteError(409, str(exc)) from exc

    @property
    def queue_storage_org_id(self) -> int | None:
        value = self.queue_payload_extra.get("storage_org_id")
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    def _local_provider_keys(self) -> dict[str, str]:
        """UI-entered local-tier keys from the gitignored workspace file, with
        env vars already winning any conflict (resolve_effective_keys drops
        providers whose env var is set so the router falls back to it)."""
        from frisket.server import provider_config

        return provider_config.resolve_effective_keys(self.root)

    def _local_endpoints(self):
        """The complete explicit local-model endpoint authority."""

        from frisket.server import provider_config

        configs, _notes = provider_config.resolve_local_endpoints(self.root)
        return configs

    def router_for(self, project: Project) -> ModelRouter:
        """Return the ordinary workspace router for non-action consumers."""

        return self._router_for_base(project, self._router)

    def action_execution_router_for(self, project: Project) -> ModelRouter:
        """Return one fresh edition router for an action execution launch."""

        base_router = (
            self._execution_router_factory()
            if self._execution_router_factory is not None
            else self._router
        )
        return self._router_for_base(project, base_router)

    def _router_for_base(
        self,
        project: Project,
        base_router: ModelRouter | None,
    ) -> ModelRouter:
        # Per-project provider keys decrypt through the store seam
        # (Project.provider_model_keys) — the router never decrypts inline.
        project_keys = project.provider_model_keys()
        # Hosted / injected base router: layer project keys over org secrets.
        # The team edition resolves its org keys for each request so a key
        # written through /api/org/keys becomes effective immediately without
        # restarting the process.  The resolver is deliberately key-only: its
        # composition boundary fixes the non-secret provenance to org_byok.
        resolved_org_keys = (
            dict(self._provider_keys_resolver())
            if self._provider_keys_resolver is not None
            else None
        )
        # With no request-time org layer or project override, the injected
        # router is already the effective router. Preserve its adapters and
        # any other injected runtime state rather than reconstructing it.
        if base_router is not None and resolved_org_keys is None and not project_keys:
            return base_router
        if base_router is not None or resolved_org_keys is not None:
            base_keys = (
                resolved_org_keys
                if resolved_org_keys is not None
                else base_router.configured_keys()
            )
            base_sources = (
                dict.fromkeys(base_keys, "org_byok")
                if resolved_org_keys is not None
                else base_router.configured_key_sources()
            )
            keys = {**base_keys, **project_keys}
            # A request-time org resolver or project key overlay requires a
            # composed router. The composition boundary fixes resolver keys'
            # non-secret provenance to org_byok.
            sources = {
                **{
                    provider: (
                        source
                        if (
                            resolved_org_keys is not None
                            or base_router.has_explicit_key_source(provider)
                        )
                        else "org_byok"
                    )
                    for provider, source in base_sources.items()
                    if provider in base_keys
                },
                **{
                    provider: "org_byok"
                    for provider in base_keys
                    if provider not in base_sources
                },
                **dict.fromkeys(project_keys, "project_key"),
            }
            return ModelRouter(
                keys=keys,
                key_sources=sources,
                cache=(
                    base_router.cache
                    if base_router is not None
                    else ResponseCache(project.path / "project.cache.db")
                ),
                cache_mode=base_router.cache_mode
                if base_router is not None
                else self._local_cache_mode,
                chaos=(
                    base_router.chaos.config
                    if base_router is not None and base_router.chaos is not None
                    else None
                ),
                max_retries=(base_router.max_retries if base_router is not None else 3),
                use_env_keys=resolved_org_keys is None,
                local_endpoints=(
                    base_router.local_endpoints
                    if base_router is not None
                    else self._local_endpoints()
                ),
                model_call_policy=(
                    base_router.model_call_policy if base_router is not None else None
                ),
            )
        # Pure local tier: workspace file keys underneath any per-project keys,
        # plus the resolved atomic local-model endpoint bundle (origin/tokens/
        # edge-auth) threaded in explicitly rather than left to the
        # router's env-only default.
        keys = {**self._local_provider_keys(), **project_keys}
        cache = ResponseCache(project.path / "project.cache.db")
        local_endpoints = self._local_endpoints()
        if keys:
            return ModelRouter(
                keys=keys,
                key_sources={
                    **dict.fromkeys(self._local_provider_keys(), "local"),
                    **dict.fromkeys(project_keys, "project_key"),
                },
                env_key_source="local",
                cache=cache,
                cache_mode=self._local_cache_mode,
                local_endpoints=local_endpoints,
            )
        return ModelRouter(
            cache=cache,
            cache_mode=self._local_cache_mode,
            env_key_source="local",
            local_endpoints=local_endpoints,
        )

    def execution_composition_for(
        self,
        project: Project,
        router: ModelRouter | None,
        context: ExecutionCompositionContext,
    ) -> ExecutionComposition:
        """Build one request's composition from its exact effective router."""
        effective_router = router if router is not None else self.router_for(project)
        return self._execution_composition_factory(project, effective_router, context)

    @staticmethod
    def execution_composition_context_for(
        request_context: Any = None,
    ) -> ExecutionCompositionContext:
        """Read the typed request fact, or construct the Local/Team direct arm."""
        state = getattr(request_context, "state", None)
        value = getattr(state, "execution_composition_context", None)
        if value is None:
            return ExecutionCompositionContext.direct()
        if not isinstance(value, ExecutionCompositionContext):
            raise TypeError(
                "request.state.execution_composition_context must be an "
                "ExecutionCompositionContext"
            )
        return value

    def edition_execution_composition_context_for(
        self,
        request_context: Any = None,
    ) -> ExecutionCompositionContext:
        """Stamp the deployment edition into the durable execution context."""
        context = self.execution_composition_context_for(request_context)
        if self.edition == "solo":
            return context
        edition_snapshot = dict(context.edition_snapshot or {})
        edition_snapshot[PRODUCT_EDITION_SNAPSHOT_KEY] = self.edition
        return replace(context, edition_snapshot=edition_snapshot)

    def org_provider_keys(self) -> dict[str, str]:
        """The team/org-injected LLM provider keys this workspace resolves
        RIGHT NOW, i.e. the same
        ``self._provider_keys_resolver()`` call ``router_for``/
        ``diagnostic_router`` already use at execution time. Public so the
        project action catalog route (server/routes/actions.py) can thread
        it into the catalog's availability hints (server/
        action_catalog_hints.py's ``_configured_llm_providers``) — without
        this, a team-edition org key (``frisket.team.app``'s
        ``org_provider_keys`` control-plane resolver, injected via
        ``provider_keys_resolver=`` at Workspace construction) made an LLM
        remote engine (map.translate's ``llm``, media.ocr's openai/gemini
        tiers) usable at RUN time but reported "unavailable" at CATALOG time,
        since the catalog previously only read the on-disk workspace
        AI-Providers file (``provider_config.resolve_effective_keys``), never
        this resolver. Empty dict when no resolver is configured (the open/
        local-server posture)."""
        return (
            dict(self._provider_keys_resolver())
            if self._provider_keys_resolver is not None
            else {}
        )

    def diagnostic_router(self) -> ModelRouter:
        """Workspace-level effective router for GET /api/diagnose.

        No ``Project`` is in scope at this route, so unlike ``router_for``
        there is no per-project key
        layer to add. In an external managed composition, the injected router
        is already the caller's own org router: its composition root builds it
        from that org's stored secrets only, and every org gets its own
        ``create_app``/``Workspace`` -- so
        returning it here can never leak another org's or the platform's
        env-only presence. Local tier: layer the workspace's own
        ``.frisket/provider_keys.json`` file keys (same precedence as
        ``router_for``'s pure-local branch: env wins per
        ``resolve_effective_keys``) instead of a bare env-only
        ``ModelRouter()``.

        CACHE POSTURE (deliberate, and what ``/api/diagnose``'s replay-mode
        probe reports): every reconstructed branch below carries the SAME
        ``cache_mode`` the effective execution router runs under -- the
        injected base router's mode when one exists, else this workspace's
        ``FRISKET_CACHE_MODE``-resolved ``_local_cache_mode``. Leaving it to
        ``ModelRouter``'s ``"replay"`` default made ``/api/diagnose``
        contradict ``/api/config`` (which reads ``self.cache_mode``) on the
        one operator surface whose job is describing the runtime honestly.
        The CACHE OBJECT is only threaded where a real one exists: an
        injected base router's cache is that org's actual execution cache, so
        it is passed through. The pure-local branch attaches NONE and says so
        -- every ResponseCache in this codebase is per-project
        (``project.path / "project.cache.db"``; see ``router_for``), no
        ``Project`` is in scope at this route, and minting a workspace-level
        cache file purely so the probe could report
        ``cache_configured=True`` would make the report claim a cache no
        execution path ever reads. This router is a probe, never a transport:
        the diagnostics that consume it read keys/endpoints
        (``provider_report``, ``ollama_report``) or run their own httpx probe
        (``provider_key_validation_report``), so nothing here ever reaches
        ``_complete_transport`` and no cache is needed to make the probe
        work."""
        if self._provider_keys_resolver is not None:
            keys = dict(self._provider_keys_resolver())
            return ModelRouter(
                keys=keys,
                key_sources=dict.fromkeys(keys, "org_byok"),
                use_env_keys=False,
                cache=self._router.cache if self._router is not None else None,
                cache_mode=(
                    self._router.cache_mode
                    if self._router is not None
                    else self._local_cache_mode
                ),
                local_endpoints=(
                    self._router.local_endpoints
                    if self._router is not None
                    else self._local_endpoints()
                ),
            )
        if self._router is not None:
            return self._router
        keys = self._local_provider_keys()
        local_endpoints = self._local_endpoints()
        return ModelRouter(
            keys=keys,
            key_sources=dict.fromkeys(keys, "local"),
            env_key_source="local",
            cache_mode=self._local_cache_mode,
            local_endpoints=local_endpoints,
        )

    @property
    def cache_mode(self) -> CacheMode:
        """The effective router cache mode for this workspace (onboard-replay-
        banner-v1): the injected router's mode when one was supplied (hosted
        per-org routers, test fixtures), else the persisted local preference
        (seeded by FRISKET_CACHE_MODE, defaulting to "replay") every lazily-
        built per-project router above uses. Read by
        server/routes/runtime_config.py so the
        frontend can report the actual active mode and the replay-mode banner
        without a project already open."""
        if self._router is not None:
            return self._router.cache_mode
        return self._local_cache_mode

    @property
    def local_cache_mode_editable(self) -> bool:
        """Whether this workspace, rather than its composition, owns the mode."""

        return self._router is None

    @property
    def cost_preapproval_usd(self) -> str:
        """The local installation's cost-gate amount, defaulting to $2."""

        return self._local_cost_preapproval_usd

    def set_local_cache_mode(self, mode: CacheMode) -> CacheMode:
        """Persist and activate a local instance's mode for future launches."""

        if self._router is not None:
            raise ValueError("this deployment owns AI call mode outside the local UI")
        with self._runtime_settings_lock:
            save_workspace_cache_mode(self.root, mode)
            self._local_cache_mode = mode
            return self._local_cache_mode

    def set_local_cost_preapproval_usd(self, amount: str) -> str:
        """Persist and activate this unauthenticated installation's threshold."""

        if self._router is not None:
            raise ValueError(
                "this deployment owns cost pre-approval outside the local UI"
            )
        with self._runtime_settings_lock:
            self._local_cost_preapproval_usd = save_workspace_cost_preapproval_usd(
                self.root, amount
            )
            return self._local_cost_preapproval_usd

    def list(self) -> list[dict]:
        """Every project row, cheaply: a manifest.json read (name +
        pending_review_count — a summary Project.refresh_pending_review_summary
        keeps current, src/frisket/engine/store/project.py) plus a project.db stat()
        for last-modified. Never opens a project's sqlite db here — at
        thousand-project scale that's the difference between a list call
        answering instantly and one that takes tens of seconds (measured
        ~5-44ms per Project() open depending on OS page-cache state)."""
        out = []
        for p in self.root.glob("*.frisket"):
            manifest_path = p / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
            except (ValueError, OSError):
                # One unreadable manifest costs ONE row, never the listing.
                # Manifest writes are atomic (engine/store/project_meta.py), so
                # this is no longer an in-flight write — it is a manifest
                # corrupted some other way: truncated by a crash before that
                # atomicity landed, half-restored, hand-edited. Raising here
                # made every project in the workspace disappear behind a 500.
                _log.warning(
                    "skipping project %s: manifest.json does not parse", p.name
                )
                continue
            if not isinstance(manifest, dict):
                _log.warning(
                    "skipping project %s: manifest.json is not an object", p.name
                )
                continue
            db_path = p / "project.db"
            updated_at = None
            if db_path.exists():
                updated_at = datetime.fromtimestamp(
                    db_path.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            out.append(
                {
                    "id": p.stem,
                    "name": manifest.get("name", p.stem),
                    "description": manifest.get("description", ""),
                    "sensitive": bool(manifest.get("sensitive", False)),
                    "updated_at": updated_at,
                    "pending_review_count": manifest.get("pending_review_count", 0),
                    # Shell/Home project flags: read
                    # straight from the manifest, no sqlite open. Default False.
                    "starred": bool(manifest.get("starred", False)),
                    "archived": bool(manifest.get("archived", False)),
                }
            )
        out.sort(key=lambda entry: entry["id"])  # stable, deterministic tie-break
        out.sort(key=lambda entry: entry["updated_at"] or "", reverse=True)
        return out

    def get(self, project_id: str) -> Project:
        try:
            validate_project_slug(project_id)
        except ValueError as exc:
            # A malformed route ID (reserved device stem, bad character, bad
            # length) must be indistinguishable from a missing project at
            # the HTTP boundary -- RouteError subclasses ValueError, so a
            # direct Python caller still observes the contractually
            # required ValueError, while the app-level RouteError handler
            # (registered once, app-wide, in create_app) turns this into a
            # 404 for every route that calls Workspace.get(), not only the
            # lifecycle routes that pre-check via
            # ProjectLifecycleService._manifest_exists. This is NOT a
            # blanket ValueError handler: only this RouteError subclass is
            # registered.
            raise RouteError(404, f"no project '{project_id}'") from exc
        if project_id not in self._projects:
            path = self.root / f"{project_id}.frisket"
            if not path.exists():
                raise HTTPException(404, f"no project '{project_id}'")
            self._projects[project_id] = self._open_project(project_id, path)
            setattr(self._projects[project_id], "_frisket_run_queue", self.queue)
        return self._projects[project_id]

    def create(
        self,
        name: str,
        *,
        project_id: str | None = None,
        sensitive: bool = False,
    ) -> dict:
        with self.plugin_package_lifecycle():
            return self._create_locked(name, project_id=project_id, sensitive=sensitive)

    def _create_locked(
        self,
        name: str,
        *,
        project_id: str | None,
        sensitive: bool,
    ) -> dict:
        if project_id is not None:
            # An explicit caller-supplied project_id must fail validation
            # outright -- no silent rewrite.
            slug = validate_project_slug(project_id)
        else:
            slug = (
                "".join(
                    c if (c.isalnum() and c.isascii()) or c in "-_" else "-"
                    for c in name.lower().strip()
                )[:64]
                or "project"
            )
            try:
                slug = validate_project_slug(slug)
            except ValueError:
                # The only way a name-derived slug (already ASCII
                # letters/digits/-/_ and <=64 chars) fails validation is a
                # reserved Windows device stem (e.g. "con"); disambiguate
                # before the collision-number loop below.
                slug = validate_project_slug(f"{slug}-project")
        path = self.root / f"{slug}.frisket"
        if project_id is not None and path.exists():
            raise FileExistsError(f"project bundle already exists: {slug}")
        if project_id is None:
            i = 1
            while path.exists():
                i += 1
                path = self.root / f"{slug}-{i}.frisket"
        blob_store = (
            self.project_blob_store_factory(path.stem)
            if self.project_blob_store_factory is not None
            else None
        )
        project = Project.create(
            path, name=name, sensitive=sensitive, blob_store=blob_store
        )
        setattr(project, "_frisket_run_queue", self.queue)
        try:
            # Bundled plugins (src/frisket/authoring/bundled_plugins/) are seeded
            # installed+enabled+activated here, through the exact same
            # public install/load/activate functions any other trusted-local
            # plugin uses. Failure policy
            # lives in bootstrap_project_bundled_plugins's docstring: a
            # broken bundled package logs loudly and is skipped, it never
            # aborts project creation.
            bootstrap_project_bundled_plugins(project, project_id=path.stem)
        finally:
            project.close()
        return {
            "id": path.stem,
            "name": name,
            "description": "",
            "sensitive": sensitive,
        }

    def delete(self, project_id: str) -> None:
        """Tear a project down completely: close the cached handle (so sqlite
        files release cleanly), drop this project's live-run handles
        (cancelling any still running), and remove the bundle directory.
        Disk and the listing stay consistent because list() globs the same
        directory the bundle is removed from — there is no separate local
        registry to desynchronize."""
        with self.plugin_package_lifecycle():
            self._delete_locked(project_id)

    def _delete_locked(self, project_id: str) -> None:
        validate_project_slug(project_id)
        path = self.root / f"{project_id}.frisket"
        if not (path / "manifest.json").exists():
            raise HTTPException(404, f"no project '{project_id}'")
        proj = self._projects.pop(project_id, None)
        if proj is not None:
            proj.close()
        for key, prog in list(self.active_runs.items()):
            if key[0] == project_id:
                if not prog.done:
                    prog.cancel()
                self.active_runs.pop(key, None)
        for key in list(self.run_jobs):
            if key[0] == project_id:
                self.run_jobs.pop(key, None)
        delete_bundle(path)

    def plugin_package_lifecycle(self) -> AbstractContextManager[None]:
        return plugin_package_lifecycle_lock(self.root)

    @property
    def _saved_recipes_path(self) -> Path:
        return self.root / "saved_recipes.json"

    @property
    def _saved_recipes_lock_path(self) -> Path:
        return self.root / ".saved_recipes.lock"

    def _read_saved_recipes(self) -> list[dict]:
        p = self._saved_recipes_path
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text())
        except (ValueError, OSError):
            return []
        return data if isinstance(data, list) else []

    def saved_recipes(self) -> list[dict]:
        with self._saved_recipes_lock:
            return self._read_saved_recipes()

    def saved_recipe_by_id(self, recipe_id: int) -> dict | None:
        with self._saved_recipes_lock:
            for recipe in self._read_saved_recipes():
                if recipe.get("id") == recipe_id:
                    return recipe
        return None

    def save_recipe(
        self,
        name: str,
        spec: dict,
        *,
        registry_artifact: dict[str, Any] | None = None,
        eval_receipts: list[dict[str, Any]] | None = None,
    ) -> dict:
        from frisket.server.services.saved_actions import require_saved_action_spec

        with self._saved_recipes_lock:
            lock_path = self._saved_recipes_lock_path
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=-1):
                recipes = self._read_saved_recipes()
                rid = max((r.get("id", 0) for r in recipes), default=0) + 1
                entry = {
                    "id": rid,
                    "name": name,
                    "spec": require_saved_action_spec(spec),
                }
                if registry_artifact is not None:
                    entry["registry_artifact"] = registry_artifact
                if eval_receipts is not None:
                    entry["eval_receipts"] = eval_receipts
                recipes.append(entry)
                tmp = self._saved_recipes_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(recipes, indent=2))
                tmp.replace(self._saved_recipes_path)
                return entry
