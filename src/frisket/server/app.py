"""frisket local server: FastAPI over the project store + runner.

Local tier: `frisket serve <dir>` — projects are
.frisket bundles in a workspace directory; no accounts. The hosted tier
mounts the same API under an auth layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from frisket.server.exports.sheet_csv import SheetExportLimits
from frisket.engine.jobs import JobQueue, WorkerPorts
from frisket.project_opener import ProjectOpener
from frisket.engine.executor import (
    CellEditQueryLimits,
    ExecutorDeps,
    ImportWorkloadLimits,
    UrlImportLimits,
)
from frisket.authoring.workbench.plugin_runtime_shared import (
    PluginCompositionPolicy,
    active_plugin_composition_policy,
    install_plugin_composition_policy,
)
from frisket.execution.pricing_policy import PricingPolicy, install_pricing_policy
from frisket.execution.provider import ExecutionCompositionFactory
from frisket.server.notifications.delivery import NotificationDeliveryRuntime
from frisket.server.notifications.secrets import NotificationSecretResolver
from frisket.server import workspace as server_workspace
from frisket.server.route_errors import register_route_error_handler
from frisket.server.import_admission import (
    ImportAdmission,
    import_admission_refusal,
    is_native_import_request,
)
from frisket.server.import_bulk_request_limit import BulkImportRequestLimitMiddleware
from frisket.server.static_serving import mount_spa_static, resolve_static_dir
from frisket.server.routes.actions import register_action_catalog_routes
from frisket.server.routes.action_preview_support import (
    register_action_param_validation_routes,
    register_action_preview_routes,
    register_action_preview_run_routes,
)
from frisket.server.routes.action_registry import register_action_registry_routes
from frisket.server.routes.action_run_support import (
    register_action_run_cancel_routes,
    register_action_run_rows_routes,
    register_action_run_status_routes,
    register_action_run_trace_routes,
    register_project_attempts_routes,
    register_run_attempt_routes,
)
from frisket.server.routes.action_runs import register_action_run_routes
from frisket.server.routes.embeddings import register_embedding_routes
from frisket.server.routes.exports import (
    register_project_export_routes,
    register_sheet_dataset_export_route,
    register_work_log_export_routes,
)
from frisket.server.routes.diagnose import register_diagnose_routes
from frisket.server.routes.imports import (
    register_import_bulk_routes,
    register_import_csv_routes,
    register_import_draft_routes,
    register_import_files_routes,
    register_import_followthemoney_routes,
    register_import_pdf_routes,
    register_import_urls_routes,
    register_import_xlsx_routes,
)
from frisket.server.routes.instance import (
    register_admin_pricing_routes,
    register_health_routes,
    register_product_telemetry_routes,
)
from frisket.server.routes.runtime_config import register_runtime_config_routes
from frisket.server.routes.notifications import register_notification_routes
from frisket.server.routes.project_actions import (
    register_project_action_data_routes,
    register_project_action_describe_routes,
)
from frisket.server.routes.previews import register_preview_routes
from frisket.server.routes.project_blobs import register_project_blob_routes
from frisket.server.routes.project_inspection import (
    register_project_debug_routes,
    register_project_history_routes,
    register_project_timing_routes,
    register_sample_project_routes,
)
from frisket.server.routes.project_evidence import register_project_evidence_routes
from frisket.server.routes.projects import register_project_lifecycle_routes
from frisket.server.routes.project_mcp import register_project_mcp_routes
from frisket.server.routes.providers import register_provider_config_routes
from frisket.server.routes.project_research import (
    register_project_backfill_activity_routes,
    register_project_copilot_routes,
    register_project_entity_review_routes,
    register_project_provenance_routes,
    register_project_search_routes,
)
from frisket.server.routes.projections import register_runtime_projection_routes
from frisket.server.routes.sheet_features import (
    register_column_run_history_routes,
    register_column_type_routes,
    register_graph_routes,
    register_map_points_routes,
    register_sheet_graph_routes,
)
from frisket.server.routes.sheet_grid import register_sheet_grid_routes
from frisket.server.routes.spend import register_spend_routes
from frisket.server.routes.sources import register_source_routes
from frisket.server.routes.views import register_view_lens_routes
from frisket.server.routes.watches import register_watch_routes
from frisket.server.routes.workbench import register_workbench_routes
from frisket.server.services.action_param_validation import (
    ActionParamValidationService,
)
from frisket.server.services.action_previews import ActionPreviewService
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.services.action_preview_jobs import ActionPreviewJobRegistry
from frisket.server.services.action_registry import ActionRegistryService
from frisket.server.services.action_run_rows import ActionRunRowsService
from frisket.server.services.action_run_cancel import ActionRunCancelService
from frisket.server.services.run_attempts import RunAttemptsService
from frisket.server.services.action_run_status import ActionRunStatusService
from frisket.server.services.action_run_trace import ActionRunTraceService
from frisket.server.services.action_runs import ActionRunService
from frisket.server.services.admin_pricing import AdminPricingService
from frisket.server.services.column_runs import ColumnRunHistoryService
from frisket.server.services.column_types import ColumnTypeCatalogService
from frisket.server.services.embeddings import EmbeddingRouteService
from frisket.server.services.graph import GraphNeighborhoodService
from frisket.server.services.sheet_graph import SheetGraphService
from frisket.server.services.import_csv import ImportCsvUploadService
from frisket.server.services.import_bulk import BulkImportLimits, ImportBulkService
from frisket.server.services.import_drafts import ImportDraftService
from frisket.server.services.import_files import ImportFilesUploadService
from frisket.server.services.import_followthemoney import FollowTheMoneyUploadService
from frisket.server.services.import_pdf import ImportPdfUploadService
from frisket.server.services.import_urls import ImportUrlsService
from frisket.server.services.import_xlsx import ImportXlsxUploadService
from frisket.server.services.map_points import MapPointsService
from frisket.server.services.notifications import NotificationService
from frisket.server.services.project_actions import ProjectActionUtilityService
from frisket.server.services.project_blobs import ProjectBlobService
from frisket.server.services.project_copilot import ProjectCopilotService
from frisket.server.services.project_entity_review import ProjectEntityReviewService
from frisket.server.services.previews import PreviewService
from frisket.server.services.project_backfill_activity import (
    ProjectBackfillActivityService,
)
from frisket.server.services.project_attempts import ProjectAttemptsService
from frisket.server.services.project_debug import ProjectDebugService
from frisket.server.services.project_evidence import ProjectEvidenceService
from frisket.server.services.project_exports import ProjectExportService
from frisket.server.services.projects import ProjectLifecycleService
from frisket.server.services.project_mcp import ProjectMcpService
from frisket.server.services.sample_project import SampleProjectSeedService
from frisket.server.services.project_history import ProjectHistoryService
from frisket.server.services.project_provenance import ProjectProvenanceService
from frisket.server.services.project_search import ProjectSearchService
from frisket.server.services.project_timing import ProjectTimingService
from frisket.server.services.projections import RuntimeProjectionService
from frisket.server.services.sheet_export import SheetDatasetExportService
from frisket.server.services.sheet_grid import SheetGridService
from frisket.server.services.spend import SpendService
from frisket.server.services.sources import SourceService
from frisket.server.services.views import ViewLensService
from frisket.server.services.watches import WatchService
from frisket.server.services.work_log_exports import WorkLogExportService
from frisket.server.services.workbench import WorkbenchService
from frisket.ai.llm import ModelRouter
from frisket.operability.structured_logging import (
    install_fastapi_logging,
)

DEFAULT_RUN_STATUS_GRACE_SECONDS = 120.0
# A worker heartbeats ~every lease/2 (~30s); 90s = three missed beats before we
# call it dead. Env-overridable so operators can tune to their worker cadence.
DEFAULT_WORKER_LIVENESS_WINDOW_SECONDS = 90.0
# Loud queue timeout: a job queued longer than this with no progress is failed
# rather than spinning forever. Generous by default; 0/empty disables.
DEFAULT_QUEUE_TIMEOUT_SECONDS = 1800.0
# The models gateway may spend up to five seconds probing its active remote
# worker. Preserve the old short connect budget when the gateway itself is
# unreachable, while allowing a longer read interval for that inner probe.
SIDECAR_CAPABILITIES_CONNECT_TIMEOUT_SECONDS = 2.0
SIDECAR_CAPABILITIES_TIMEOUT_SECONDS = 10.0
SIDECAR_CAPABILITIES_CACHE_TTL_SECONDS = 5.0
LOG = logging.getLogger("frisket.server")


class _NativeImportAdmissionMiddleware:
    """Acquire a hosted import slot before a native route consumes its body."""

    def __init__(self, app: ASGIApp, *, import_admission: ImportAdmission) -> None:
        self.app = app
        self.import_admission = import_admission

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not is_native_import_request(scope):
            await self.app(scope, receive, send)
            return
        permit = self.import_admission.try_acquire()
        if permit is None:
            await import_admission_refusal()(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            permit.release()


class _SidecarCapabilitiesCache:
    """Small app-scoped TTL cache that coalesces catalog discovery probes."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        clock: Callable[[], float],
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._key: tuple[str | None, str | None] | None = None
        self._value: dict[str, Any] | None = None
        self._expires_at = 0.0

    def get(
        self,
        key: tuple[str | None, str | None],
        loader: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        # Catalog routes are synchronous FastAPI endpoints and may execute in
        # parallel worker threads. Keep the load under the lock so concurrent
        # misses share one outbound probe, including when that probe fails.
        with self._lock:
            if (
                self._value is not None
                and self._key == key
                and self._clock() < self._expires_at
            ):
                return self._value
            value = loader()
            self._key = key
            self._value = value
            # Start the full TTL after a slow probe finishes, not before it.
            self._expires_at = self._clock() + self._ttl_seconds
            return value


def _positive_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _optional_timeout_env(name: str, default: float) -> float | None:
    value = _positive_env_float(name, default)
    return value if value > 0 else None


def _media_column_kind(mime: str | None) -> str:
    if mime and mime.startswith("audio/"):
        return "audio"
    if mime and mime.startswith("video/"):
        return "video"
    if mime and mime.startswith("image/"):
        return "image"
    return "file"


def create_app(
    workspace_root: str | Path,
    router: ModelRouter | None = None,
    *,
    queue: JobQueue | None = None,
    queue_clock: Callable[[], datetime] | None = None,
    queue_payload_extra: dict[str, Any] | None = None,
    control_database_url: str | None = None,
    run_status_grace_seconds: float | None = None,
    worker_liveness_window_seconds: float | None = None,
    queue_timeout_seconds: float | None = None,
    executor_deps_factory: Callable[[str, Request], ExecutorDeps] | None = None,
    sheet_export_limits: SheetExportLimits | None = None,
    url_import_limits: UrlImportLimits | None = None,
    import_workload_limits: ImportWorkloadLimits | None = None,
    cell_edit_query_limits: CellEditQueryLimits | None = None,
    project_secret_fallback_resolver: Callable[[str, str], str | None] | None = None,
    notification_secret_resolver: NotificationSecretResolver | None = None,
    notification_delivery_runtime: NotificationDeliveryRuntime | None = None,
    static_dir: str | Path | None = None,
    serve_spa: bool = True,
    enable_provider_config: bool = True,
    provider_keys_resolver: Callable[[], Mapping[str, str]] | None = None,
    worker_ports: WorkerPorts | None = None,
    project_blob_store_factory: Callable[[str], Any] | None = None,
    project_opener: ProjectOpener | None = None,
    install_queue_terminalization: bool = True,
    require_storage_identity: bool = False,
    require_explicit_provider_keys: bool = False,
    product_telemetry_destination: Any | None = None,
    edition: str = "solo",
    model_pull_enabled: bool | None = None,
    pricing_policy: PricingPolicy | None = None,
    execution_router_factory: Callable[[], ModelRouter] | None = None,
    execution_composition_factory: ExecutionCompositionFactory | None = None,
    direct_action_receipt_settlement_port: (
        server_workspace.DirectActionReceiptSettlementPort | None
    ) = None,
    plugin_composition_policy: PluginCompositionPolicy | None = None,
    auth_methods: Any | None = None,
    bulk_import_limits: BulkImportLimits | None = None,
    import_admission: ImportAdmission | None = None,
) -> FastAPI:
    # The deployment's price list (frisket.execution.pricing_policy), installed
    # process-wide because the fourteen sites that mint a money confirmation
    # share no object to carry it — see that module for why. A downstream
    # composition's ``app.py`` is the intended caller; with none, this process
    # rates at the identity policy and bills exactly provider cost. Installing
    # twice with different policies refuses by name, so a host cannot end up
    # quoting off two price lists.
    if pricing_policy is not None:
        install_pricing_policy(pricing_policy)
    # THE plugin composition policy for this process: which persisted plugins
    # this deployment may expose, serve, activate, and execute. Installed
    # process-wide (the pricing-policy seam's shape and reasoning) because the
    # surfaces that must honor it -- the routes here AND the execution-path
    # dispatch resolution in the worker -- share no object to carry one. With
    # none, the process runs the permissive default and open/Local/Team
    # behavior is unchanged.
    if plugin_composition_policy is not None:
        install_plugin_composition_policy(plugin_composition_policy)
    app = FastAPI(title="frisket", version="0.1.0")
    install_fastapi_logging(app)
    register_route_error_handler(app)
    bulk_limits = bulk_import_limits or BulkImportLimits()
    if bulk_limits.max_request_bytes is not None:
        app.add_middleware(
            BulkImportRequestLimitMiddleware,
            max_request_bytes=bulk_limits.max_request_bytes,
        )
    if import_admission is not None:
        app.add_middleware(
            _NativeImportAdmissionMiddleware,
            import_admission=import_admission,
        )
    export_limits = sheet_export_limits or SheetExportLimits()
    original_executor_deps_factory = executor_deps_factory
    # A factory's presence is itself a composition signal: the local action
    # catalog uses it to decide whether connected-account actions are
    # available.  Do not turn the unlimited policy defaults into that signal.
    # Explicit finite policies still need this request-scoped overlay so every
    # action execution path receives the deployment limit.
    needs_executor_deps_factory = (
        original_executor_deps_factory is not None
        or sheet_export_limits not in (None, SheetExportLimits())
        or url_import_limits not in (None, UrlImportLimits())
        or import_workload_limits not in (None, ImportWorkloadLimits())
        or cell_edit_query_limits not in (None, CellEditQueryLimits())
    )

    def export_executor_deps_factory(project_id: str, request: Request) -> ExecutorDeps:
        from dataclasses import replace

        base = (
            original_executor_deps_factory(project_id, request)
            if original_executor_deps_factory is not None
            else ExecutorDeps()
        )
        base = base or ExecutorDeps()
        return replace(
            base,
            sheet_export_limits=(
                sheet_export_limits
                if sheet_export_limits is not None
                else base.sheet_export_limits
            ),
            url_import_limits=(
                url_import_limits
                if url_import_limits is not None
                else base.url_import_limits
            ),
            import_workload_limits=(
                import_workload_limits
                if import_workload_limits is not None
                else base.import_workload_limits
            ),
            cell_edit_query_limits=(
                cell_edit_query_limits
                if cell_edit_query_limits is not None
                else base.cell_edit_query_limits
            ),
        )

    ws = server_workspace.Workspace(
        Path(workspace_root),
        router,
        queue=queue,
        queue_clock=queue_clock,
        queue_payload_extra=queue_payload_extra,
        control_database_url=control_database_url,
        executor_deps_factory=(
            export_executor_deps_factory if needs_executor_deps_factory else None
        ),
        project_secret_fallback_resolver=project_secret_fallback_resolver,
        notification_secret_resolver=notification_secret_resolver,
        notification_delivery_runtime=notification_delivery_runtime,
        provider_keys_resolver=provider_keys_resolver,
        worker_ports=worker_ports,
        project_blob_store_factory=project_blob_store_factory,
        project_opener=project_opener,
        install_queue_terminalization=install_queue_terminalization,
        require_storage_identity=require_storage_identity,
        require_explicit_provider_keys=require_explicit_provider_keys,
        # The model.pull worker handler rides
        # the SAME gate as the provider-config routes below -- a composition
        # with the routes disabled (team's enable_provider_config=False)
        # also structurally lacks the handler, not just the route.
        enable_local_model_pull=enable_provider_config,
        plugin_composition_policy=plugin_composition_policy,
        execution_router_factory=execution_router_factory,
        execution_composition_factory=execution_composition_factory,
        direct_action_receipt_settlement_port=(direct_action_receipt_settlement_port),
        edition=edition,
    )
    stale_run_grace_seconds = (
        DEFAULT_RUN_STATUS_GRACE_SECONDS
        if run_status_grace_seconds is None
        else max(0.0, float(run_status_grace_seconds))
    )
    liveness_window_seconds = (
        _positive_env_float(
            "FRISKET_WORKER_LIVENESS_WINDOW_SECONDS",
            DEFAULT_WORKER_LIVENESS_WINDOW_SECONDS,
        )
        if worker_liveness_window_seconds is None
        else max(0.0, float(worker_liveness_window_seconds))
    )
    if queue_timeout_seconds is None:
        queue_timeout = _optional_timeout_env(
            "FRISKET_QUEUE_TIMEOUT_SECONDS", DEFAULT_QUEUE_TIMEOUT_SECONDS
        )
    else:
        queue_timeout = (
            float(queue_timeout_seconds) if queue_timeout_seconds > 0 else None
        )
    app.state.workspace = ws
    action_preview_job_registry = ActionPreviewJobRegistry()
    app.state.action_preview_job_registry = action_preview_job_registry
    sidecar_capabilities_cache = _SidecarCapabilitiesCache(
        ttl_seconds=SIDECAR_CAPABILITIES_CACHE_TTL_SECONDS,
        clock=time.monotonic,
    )
    app.state.sidecar_capabilities_cache = sidecar_capabilities_cache

    async def _shutdown_action_preview_jobs() -> None:
        # Joining process-owning preview workers can take until their bounded
        # sandbox teardown completes; keep that wait off the ASGI event loop.
        await asyncio.to_thread(action_preview_job_registry.shutdown)

    app.router.add_event_handler("shutdown", _shutdown_action_preview_jobs)
    from frisket.operability.telemetry import build_product_telemetry_runtime

    app.state.product_telemetry = build_product_telemetry_runtime(
        edition=edition,
        destination=product_telemetry_destination,
    )
    register_product_telemetry_routes(
        app,
        workspace=ws,
        runtime=app.state.product_telemetry,
    )

    # ---------- programmatic actions ----------

    register_action_catalog_routes(
        app,
        workspace=ws,
        # Cloud's project composition already declares the gateway engines it
        # sells. Probing /capabilities here would cold-start Modal merely to
        # render the picker; diagnostics and execution retain live probes.
        sidecar_capabilities=lambda: (
            {"catalog_from_composition": True}
            if edition == "cloud"
            else _sidecar_capabilities()
        ),
        edition=edition,
    )

    # ---------- projects ----------

    register_project_lifecycle_routes(app, service=ProjectLifecycleService(ws))
    # Local stdio MCP servers execute as the Solo user's process.  Team has a
    # distinct trust/deployment model, so it must not inherit these routes by
    # virtue of mounting this otherwise shared core app.
    if edition == "solo":
        register_project_mcp_routes(app, service=ProjectMcpService(ws))
    register_sample_project_routes(app, service=SampleProjectSeedService(ws))
    # Local-tier UI key management (workspace file + validate probe). The
    # hosted tier builds per-org sub-apps through create_app but must NOT
    # expose these: tenant dispatch has no capability gate for the
    # `providers` path segment, so an exposed route is reachable by any
    # signed-in user of any org (env-key hint leak, non-owner key writes,
    # unmetered validate oracle). Hosted key management is the owner-gated
    # /api/org/keys instead.
    if enable_provider_config:
        register_provider_config_routes(app, workspace=ws)
    project_action_utility_service = ProjectActionUtilityService(ws)

    register_project_action_describe_routes(
        app,
        service=project_action_utility_service,
    )

    # Zero availability registers NO workbench plugin routes. A bundled-only
    # policy registers only the runtime index and admitted frontend modules;
    # configuration and lifecycle routes exist only when nonbundled plugins are
    # enabled. Per-plugin access still derives from the same runtime policy.
    active_policy = active_plugin_composition_policy()
    if active_policy.exposes_any_plugins:
        register_workbench_routes(
            app,
            service=WorkbenchService(ws),
            nonbundled_enabled=active_policy.nonbundled_enabled,
        )

    register_project_action_data_routes(
        app,
        service=project_action_utility_service,
    )

    action_preview_run_service = ActionPreviewRunService(
        ws, registry=action_preview_job_registry
    )
    register_preview_routes(
        app,
        service=PreviewService(ws),
        action_preview_service=action_preview_run_service,
    )

    register_embedding_routes(app, service=EmbeddingRouteService(ws))

    register_action_run_routes(
        app,
        service=ActionRunService(
            ws,
            stale_run_grace_seconds=stale_run_grace_seconds,
            worker_liveness_window_seconds=liveness_window_seconds,
            queue_timeout_seconds=queue_timeout,
        ),
        import_admission=import_admission,
    )

    # ---------- sheet data ----------

    register_sheet_grid_routes(app, service=SheetGridService(ws))

    register_graph_routes(app, service=GraphNeighborhoodService(ws))

    register_sheet_graph_routes(app, service=SheetGraphService(ws))

    register_map_points_routes(app, service=MapPointsService(ws))

    register_runtime_projection_routes(app, service=RuntimeProjectionService(ws))

    # ---------- import ----------

    register_import_draft_routes(app, service=ImportDraftService(ws))

    register_import_bulk_routes(app, service=ImportBulkService(ws, limits=bulk_limits))

    register_import_csv_routes(
        app,
        service=ImportCsvUploadService(ws),
        limits=bulk_limits,
    )

    register_import_xlsx_routes(
        app,
        service=ImportXlsxUploadService(ws),
        limits=bulk_limits,
    )

    register_import_pdf_routes(
        app,
        service=ImportPdfUploadService(ws),
        limits=bulk_limits,
    )

    register_import_files_routes(
        app,
        service=ImportFilesUploadService(ws),
        limits=bulk_limits,
    )
    register_import_followthemoney_routes(
        app,
        service=FollowTheMoneyUploadService(ws),
        limits=bulk_limits,
    )

    register_import_urls_routes(
        app,
        service=ImportUrlsService(ws),
    )

    # ---------- recipes / runs ----------

    def _sidecar_capabilities() -> dict[str, Any]:
        """Best-effort frisket-models discovery for recipe metadata.

        The action catalog is UI-critical, so a missing or unhealthy sidecar
        must surface as disabled engine metadata rather than taking /catalog
        down.
        """
        import httpx

        from frisket.ops._sidecar import (
            probe_sidecar_capabilities,
            sidecar_base_url,
            sidecar_token,
        )

        base = sidecar_base_url()
        token = sidecar_token()

        def _probe() -> dict[str, Any]:
            return probe_sidecar_capabilities(
                base=base,
                token=token,
                timeout=httpx.Timeout(
                    connect=SIDECAR_CAPABILITIES_CONNECT_TIMEOUT_SECONDS,
                    read=SIDECAR_CAPABILITIES_TIMEOUT_SECONDS,
                    write=SIDECAR_CAPABILITIES_CONNECT_TIMEOUT_SECONDS,
                    pool=SIDECAR_CAPABILITIES_CONNECT_TIMEOUT_SECONDS,
                ),
            )

        return sidecar_capabilities_cache.get((base, token), _probe)

    register_admin_pricing_routes(app, service=AdminPricingService())

    register_action_registry_routes(app, service=ActionRegistryService(ws))

    register_action_preview_routes(app, service=ActionPreviewService(ws))

    register_action_param_validation_routes(
        app, service=ActionParamValidationService(ws)
    )

    register_action_preview_run_routes(
        app,
        service=action_preview_run_service,
        import_admission=import_admission,
    )

    register_project_timing_routes(app, service=ProjectTimingService(ws))

    register_action_run_rows_routes(app, service=ActionRunRowsService(ws))

    register_action_run_cancel_routes(app, service=ActionRunCancelService(ws))

    register_action_run_status_routes(
        app,
        service=ActionRunStatusService(
            ws,
            stale_run_grace_seconds=stale_run_grace_seconds,
            worker_liveness_window_seconds=liveness_window_seconds,
            queue_timeout_seconds=queue_timeout,
        ),
    )

    register_action_run_trace_routes(app, service=ActionRunTraceService(ws))

    # ---------- columns ----------

    register_column_type_routes(app, service=ColumnTypeCatalogService(ws))

    # ---------- history / undo ----------

    register_column_run_history_routes(app, service=ColumnRunHistoryService(ws))

    register_project_history_routes(app, service=ProjectHistoryService(ws))

    register_project_debug_routes(app, service=ProjectDebugService(ws))

    # ---------- cells / blobs ----------

    register_project_evidence_routes(app, service=ProjectEvidenceService(ws))

    register_project_blob_routes(app, service=ProjectBlobService(ws))

    # ---------- Entities ----------

    register_project_entity_review_routes(
        app,
        service=ProjectEntityReviewService(ws),
    )

    register_project_copilot_routes(
        app,
        service=ProjectCopilotService(ws),
    )

    register_project_search_routes(
        app,
        service=ProjectSearchService(ws),
    )

    register_project_provenance_routes(
        app,
        service=ProjectProvenanceService(ws),
    )

    register_project_backfill_activity_routes(
        app,
        service=ProjectBackfillActivityService(ws),
    )

    register_sheet_dataset_export_route(
        app,
        service=SheetDatasetExportService(
            ws,
            limits=export_limits,
        ),
    )

    register_work_log_export_routes(app, service=WorkLogExportService(ws))

    register_project_export_routes(app, service=ProjectExportService(ws))

    register_spend_routes(app, service=SpendService(ws))

    view_lens_service = ViewLensService(ws)
    register_view_lens_routes(app, service=view_lens_service)

    register_watch_routes(
        app,
        service=WatchService(ws),
    )

    register_notification_routes(app, service=NotificationService(ws))

    # ---------- sources / scheduling / monitoring ----------
    # The Manosphere pattern: live sources, cron, new-row detection,
    # digest emails. This is the CRUD + monitoring-state surface; a scheduler
    # polls each source on its cron and lands new rows through worker actions.

    register_source_routes(app, service=SourceService(ws))

    if model_pull_enabled is None:
        # The local composition always registers the pull handler; individual
        # endpoint records own whether the route admits a pull.
        effective_model_pull_enabled = enable_provider_config
    else:
        effective_model_pull_enabled = model_pull_enabled

    register_health_routes(
        app,
        queue=ws.queue,
        liveness_window_seconds=liveness_window_seconds,
        timeout_seconds=queue_timeout,
        model_pull_enabled=effective_model_pull_enabled,
    )

    register_diagnose_routes(
        app,
        workspace=ws,
        liveness_window_seconds=liveness_window_seconds,
        queue_timeout_seconds=queue_timeout,
    )
    register_runtime_config_routes(
        app,
        workspace=ws,
        allow_updates=enable_provider_config and ws.local_cache_mode_editable,
        auth_methods=auth_methods,
        product_telemetry_available=app.state.product_telemetry.available,
    )

    # Register the attempt receipt last among the API
    # routes: the route-order pins in tests/server/ form one unbroken
    # adjacency chain over every earlier registration, and a read-only
    # addition should not force any of them to be rewritten. Its literal
    # path shadows nothing.
    register_run_attempt_routes(app, service=RunAttemptsService(ws))

    # The run-keyed reader above cannot open a
    # compaction-orphaned attempt (`run_id` NULL by ruling 7's ON DELETE SET
    # NULL), so the record the ruling preserves had no opener. Project-keyed,
    # with the run as an optional filter. Registered beside its sibling for
    # the same reason: a read-only addition should not rewrite the route-order
    # pins in tests/server/, and its literal path shadows nothing.
    register_project_attempts_routes(app, service=ProjectAttemptsService(ws))

    # static frontend (built web/ bundle) — mounted last so API routes win.
    # Resolution: explicit static_dir arg -> FRISKET_STATIC_DIR -> packaged
    # web bundle -> none (dev mode: vite serves the UI, proxying /api here).
    # serve_spa=False is for embedders that own the browser-facing mount
    # themselves (the hosted app builds per-tenant sub-apps through here and
    # must not grow an inner catch-all just because FRISKET_STATIC_DIR is set
    # process-wide).
    if serve_spa:
        resolved_static = resolve_static_dir(static_dir)
        if resolved_static is not None:
            mount_spa_static(app, resolved_static)

    return app
