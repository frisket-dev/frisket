"""Neutral base endpoint catalog and the single per-edition policy compiler.

This module is the public seam every edition shares: it declares the identity
(owner, route name, method) and the generic auth/RBAC expectations of every
open/shared endpoint, plus the pure compiler that joins per-edition
declarations to a registered route inventory and resolvers as one fail-closed
decision table. Registered routes alone own their wire paths.

It carries NO commerce effects: no ``paid.account``/``funding.reservation``
resolvers, no funding reservation flag, and no managed-only endpoint
declarations (commerce checkout/webhook, account funding, signup requests,
fleet org creation, tenant dispatch). Those are an external managed
composition's CONTRIBUTION, declared in its own route-policy module, which
enriches these base entries rather than redeclaring them.

The module is intentionally a stdlib-only import leaf so any edition (local,
open team, external managed composition) can consume it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from types import MappingProxyType
from typing import Any, Literal

AuthMode = Literal["public", "session_or_pat", "browser_session", "admin"]
ProjectRole = Literal["viewer", "reviewer", "editor", "owner"]
RouteOwner = Literal["outer", "tenant"]
RouteSpec = tuple[str, str]
RouteIdentity = tuple[str, str, str]

_SUPPORTED_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
_AUTH_MODES = frozenset({"public", "session_or_pat", "browser_session", "admin"})
_PROJECT_ROLES = frozenset({"viewer", "reviewer", "editor", "owner"})


@dataclass(frozen=True)
class EndpointPolicy:
    """One endpoint declaration: identity, auth/RBAC, and effects.

    ``resolvers``/``reserves_funding``/``pat_forbidden_detail`` are effect
    fields an edition may enrich; the base catalog leaves the commerce ones
    unset.
    """

    id: str
    route_owner: RouteOwner
    route_name: str
    method: str
    auth: AuthMode
    browser_client: bool = False
    forwards_to_tenant: bool = False
    project_role: ProjectRole | None = None
    resolvers: tuple[str, ...] = ()
    reserves_funding: bool = False
    pat_forbidden_detail: str | None = None


@dataclass(frozen=True)
class RoutePolicyDecision:
    id: str
    route_owner: str
    route_name: str
    method: str
    auth: str
    forwards_to_tenant: bool
    project_role: str | None
    resolvers: tuple[str, ...]
    reserves_funding: bool
    pat_forbidden_detail: str | None


@dataclass(frozen=True)
class CompiledRoutePolicy:
    _decisions: Mapping[RouteIdentity, RoutePolicyDecision]
    _resolvers: Mapping[str, Callable[..., Any]]

    @property
    def resolvers(self) -> Mapping[str, Callable[..., Any]]:
        return self._resolvers

    def resolve(
        self,
        route_owner: str,
        route_name: str,
        method: str,
    ) -> RoutePolicyDecision:
        identity = _identity(route_owner, route_name, method)
        try:
            return self._decisions[identity]
        except KeyError as exc:
            raise LookupError(
                f"unknown hosted route policy identity: {identity!r}"
            ) from exc


def declare_endpoints(
    owner: RouteOwner,
    auth: AuthMode,
    routes: Sequence[RouteSpec],
    *,
    project_role: ProjectRole | None = None,
    resolvers: tuple[str, ...] = (),
    forwards_to_tenant: bool = False,
    reserves_funding: bool = False,
    pat_forbidden_detail: str | None = None,
) -> tuple[EndpointPolicy, ...]:
    """Expand one policy group into explicit per-method declarations."""

    declarations: list[EndpointPolicy] = []
    for raw in routes:
        if isinstance(raw, Mapping):
            legacy_keys = sorted({"path", "route_template", "template"} & set(raw))
            if legacy_keys:
                raise ValueError(
                    f"endpoint RouteSpec cannot contain legacy path keys: {legacy_keys!r}"
                )
        if not isinstance(raw, (tuple, list)) or len(raw) != 2:
            raise ValueError("endpoint RouteSpec must be exactly (route_name, method)")
        name, method = raw
        if not isinstance(name, str) or not isinstance(method, str):
            raise ValueError("endpoint RouteSpec names and methods must be strings")
        declarations.append(
            EndpointPolicy(
                id=f"{owner}.{name}.{method.lower()}",
                route_owner=owner,
                route_name=name,
                method=method,
                auth=auth,
                forwards_to_tenant=forwards_to_tenant,
                project_role=project_role,
                resolvers=resolvers,
                reserves_funding=reserves_funding,
                pat_forbidden_detail=pat_forbidden_detail,
            )
        )
    return tuple(declarations)


def order_group(entries: Sequence[EndpointPolicy]) -> tuple[EndpointPolicy, ...]:
    """Canonical within-group order: route name, then method."""

    return tuple(sorted(entries, key=lambda entry: (entry.route_name, entry.method)))


# Each method identity below is explicit. Grouping reduces syntax only; no
# runtime path/method default expands this catalog. Group keys are the
# composition unit an edition contributes into.
_OUTER_PUBLIC: tuple[RouteSpec, ...] = (
    ("client_errors", "POST"),
    ("health", "GET"),
    ("instance_info", "GET"),
    ("runtime_config", "GET"),
)

_OUTER_ADMIN: tuple[RouteSpec, ...] = (
    ("admin_audit", "GET"),
    ("admin_errors", "GET"),
    ("admin_health", "GET"),
    ("admin_invite_user", "POST"),
    ("admin_job_cancel", "POST"),
    ("admin_jobs", "GET"),
    ("admin_list_users", "GET"),
    ("admin_overview", "GET"),
    # {user_ref} is a numeric user id (SPA) or an email (remote operator CLI).
    ("admin_remove_user", "DELETE"),
    ("admin_revoke_invite", "DELETE"),
    ("admin_update_user_role", "PATCH"),
    # Browser administration is a public contract surface shared by the
    # open/team client. The team composition is its sole registered edition;
    # exporter projection records that from the live route inventory.
    ("admin_browser_audit", "GET"),
    ("admin_browser_cancel_job", "POST"),
    ("admin_browser_errors", "GET"),
    ("admin_browser_health", "GET"),
    ("admin_browser_invite_user", "POST"),
    ("admin_browser_jobs", "GET"),
    ("admin_browser_remove_user", "DELETE"),
    ("admin_browser_revoke_invite", "DELETE"),
    ("admin_browser_update_user_role", "PATCH"),
    ("admin_browser_users", "GET"),
    # Org-wide network default: owner-only, like the other org-shaping admin
    # actions.
    ("update_org_network", "PATCH"),
)

_OUTER_OAUTH_BROWSER: tuple[RouteSpec, ...] = (
    ("google_connection_callback", "GET"),
    ("google_connection_start", "GET"),
    ("list_oauth_connections", "GET"),
    (
        "revoke_oauth_connection",
        "DELETE",
    ),
)

_OUTER_PROFILE_BROWSER: tuple[RouteSpec, ...] = (("update_profile", "PATCH"),)

_OUTER_SESSION: tuple[RouteSpec, ...] = (
    ("create_project_invite", "POST"),
    ("create_token", "POST"),
    ("delete_org_env_var", "DELETE"),
    ("delete_org_key", "DELETE"),
    ("diagnostic_bundle", "POST"),
    ("get_org_network", "GET"),
    ("get_project_blob", "GET"),
    ("list_members", "GET"),
    ("list_org_env_vars", "GET"),
    ("list_org_keys", "GET"),
    # Boolean-only media egress proxy status for run-failure remediation UI;
    # the proxy URL itself stays on the operator admin surface.
    ("org_media_proxy_status", "GET"),
    ("list_project_invites", "GET"),
    ("list_tokens", "GET"),
    ("me", "GET"),
    ("remove_member", "DELETE"),
    ("revoke_project_invite", "DELETE"),
    ("revoke_token", "DELETE"),
    ("set_member", "POST"),
    ("set_org_env_var", "POST"),
    ("set_org_key", "POST"),
    ("validate_org_key", "POST"),
)

_TENANT_PUBLIC: tuple[RouteSpec, ...] = (
    ("health", "GET"),
    ("runtime_config", "GET"),
)

_TENANT_ADMIN: tuple[RouteSpec, ...] = (("external_pricing", "GET"),)

_TENANT_SESSION: tuple[RouteSpec, ...] = (
    ("actions_schema", "GET"),
    ("create_project", "POST"),
    ("diagnose", "GET"),
    (
        "get_action_registry_artifact",
        "GET",
    ),
    ("import_action_artifact", "POST"),
    ("list_action_registry", "GET"),
    ("list_column_types", "GET"),
    ("list_projects", "GET"),
    ("list_walkthroughs", "GET"),
    ("provider_catalog", "GET"),
    ("product_telemetry_installation", "POST"),
    ("publish_action_artifact", "POST"),
    ("v1_action_catalog", "GET"),
)

_TENANT_LOCAL_PROVIDERS: tuple[RouteSpec, ...] = (
    ("cancel_model_pull", "POST"),
    ("create_local_endpoint", "POST"),
    ("discover_local_endpoints", "POST"),
    ("delete_local_endpoint", "DELETE"),
    ("delete_provider_key", "DELETE"),
    ("get_model_pull", "GET"),
    ("list_model_pulls", "GET"),
    ("list_providers", "GET"),
    ("provider_status", "GET"),
    ("pull_artifact", "POST"),
    ("set_provider_key", "PUT"),
    ("uninstall_artifact", "POST"),
    ("update_local_endpoint", "PATCH"),
    ("validate_provider_key", "POST"),
)

_TENANT_LOCAL_RUNTIME_SETTINGS: tuple[RouteSpec, ...] = (
    ("update_runtime_config", "PATCH"),
)

_TENANT_LOCAL_MCP: tuple[RouteSpec, ...] = (
    ("create_mcp_server", "POST"),
    ("delete_mcp_server", "DELETE"),
    ("import_mcp_servers", "POST"),
    ("list_mcp_servers", "GET"),
    ("test_mcp_server", "POST"),
    ("update_mcp_server", "PATCH"),
)

# This is the sole edition-local policy identity owner. The routes remain in
# tenant.session for local composition, while team deliberately removes only
# these declarations before compiling its provider-config-disabled core.
LOCAL_PROVIDER_ENDPOINT_IDS = frozenset(
    f"tenant.{route_name}.{method.lower()}"
    for route_name, method in _TENANT_LOCAL_PROVIDERS
)

# Runtime setting mutations are local-instance authority, just like the
# workspace provider-config routes. Team/managed compositions keep their
# deployment-owned cache posture and structurally omit this route.
LOCAL_RUNTIME_SETTINGS_ENDPOINT_IDS = frozenset(
    f"tenant.{route_name}.{method.lower()}"
    for route_name, method in _TENANT_LOCAL_RUNTIME_SETTINGS
)
LOCAL_MCP_ENDPOINT_IDS = frozenset(
    f"tenant.{route_name}.{method.lower()}" for route_name, method in _TENANT_LOCAL_MCP
)
LOCAL_CONFIGURATION_ENDPOINT_IDS = (
    LOCAL_PROVIDER_ENDPOINT_IDS
    | LOCAL_RUNTIME_SETTINGS_ENDPOINT_IDS
    | LOCAL_MCP_ENDPOINT_IDS
)

_TENANT_SPEND: tuple[RouteSpec, ...] = (("spend", "GET"),)

_TENANT_VIEWER: tuple[RouteSpec, ...] = (
    ("action_describe_project", "GET"),
    ("action_export", "GET"),
    ("action_job_detail", "GET"),
    ("action_jobs", "GET"),
    ("action_list_sheets", "GET"),
    # The attempt receipt answers "did I say yes, what did it cost, where
    # did it go" for one run. A READ, so viewer-tier like its sibling
    # action_run_status. Omitting it made the team app refuse to start
    # (`unclassified /api route(s) on the protected team app`), which is the
    # fence working: an unclassified route is a route nobody decided the
    # policy for.
    (
        "run_attempts",
        "GET",
    ),
    # The same receipt, keyed by PROJECT instead of run — the reader that can
    # open a compaction-orphaned attempt (ruling 7 NULLs `run_id`, so the
    # entry above structurally cannot return one). Same viewer tier: it is
    # the same read, asked without the run key.
    ("project_attempts", "GET"),
    ("action_run_status", "GET"),
    ("action_run_trace", "GET"),
    (
        "action_run_trace_row",
        "GET",
    ),
    ("backfill_activity", "GET"),
    ("cell_evidence", "GET"),
    (
        "cell_text_annotations",
        "GET",
    ),
    (
        "column_evidence",
        "GET",
    ),
    ("column_runs", "GET"),
    (
        "column_stats",
        "GET",
    ),
    ("embedding_indexes", "GET"),
    (
        "embedding_provider_catalog",
        "GET",
    ),
    ("entities_ep", "GET"),
    (
        "evidence_link_run_clip",
        "GET",
    ),
    (
        "evidence_span_clip",
        "GET",
    ),
    (
        "evidence_viewer",
        "GET",
    ),
    ("get_blob", "GET"),
    ("get_lens_ep", "GET"),
    ("get_project", "GET"),
    ("get_project_network", "GET"),
    ("get_project_retention", "GET"),
    ("get_source_ep", "GET"),
    ("get_source_health_ep", "GET"),
    ("get_view_ep", "GET"),
    ("graph_neighborhood", "GET"),
    ("history", "GET"),
    ("list_lenses", "GET"),
    ("list_notification_channels", "GET"),
    (
        "list_notification_delivery_requests",
        "GET",
    ),
    ("list_notification_routes", "GET"),
    ("list_notifications", "GET"),
    ("list_project_column_types", "GET"),
    ("list_sheets", "GET"),
    ("list_sources", "GET"),
    ("list_views", "GET"),
    (
        "list_watch_run_events",
        "GET",
    ),
    ("list_watch_runs", "GET"),
    ("list_watches", "GET"),
    (
        "locate_sheet_row",
        "GET",
    ),
    ("notifications_summary", "GET"),
    ("project_debug", "GET"),
    # The project-scoped half of Diagnose. It lives on a {pid} path, not on
    # /api/diagnose with a project_id query parameter, because the role ladder
    # resolves a project ONLY from path_params["pid"]: a project named anywhere
    # else is invisible to a gate that is otherwise exhaustive by construction.
    # Viewer matches its nearest sibling, workbench_plugins, which is the other
    # read that runs the idempotent bundled-plugin bootstrap.
    ("project_diagnose", "GET"),
    ("project_lineage", "GET"),
    ("project_timing", "GET"),
    ("product_telemetry_project", "POST"),
    ("project_v1_action_catalog", "GET"),
    ("provenance", "GET"),
    ("resolve_lens", "GET"),
    ("review_bundles_ep", "GET"),
    ("review_count_ep", "GET"),
    ("review_queue_ep", "GET"),
    ("run_rows", "GET"),
    ("search_ep", "GET"),
    ("sheet_data", "GET"),
    ("sheet_graph", "GET"),
    ("sheet_map_points", "GET"),
    ("v1_action_preview_status", "GET"),
    ("v1_action_preview_artifact", "GET"),
    (
        "v1_receipt_lookup",
        "GET",
    ),
    ("workbench_marketplace", "GET"),
    (
        "workbench_plugin_frontend_component_module_route",
        "GET",
    ),
    ("workbench_plugins", "GET"),
)

# Bulk data-takeout routes: the whole project (incl. raw SQLite via mode=db),
# a full sheet, the work log (which carries prompt text and before/after
# example cell values), or a whole embedding index. These read the same
# facts a viewer can already see one cell/row at a time, but bundle them into
# a single walk-off-with-everything download -- a newsroom granting read
# access does not mean to grant a full data takeout. One rung above the
# ordinary per-cell/per-row reads in ``_TENANT_VIEWER``.
_TENANT_REVIEWER: tuple[RouteSpec, ...] = (
    ("export_project", "GET"),
    ("export_sheet_dataset", "GET"),
    ("export_work_log", "GET"),
    ("export_work_log_html", "GET"),
    ("export_work_log_pdf", "GET"),
    (
        "embedding_index_export_download",
        "GET",
    ),
    (
        "embedding_index_export_manifest",
        "GET",
    ),
)

_TENANT_OWNER: tuple[RouteSpec, ...] = (
    (
        "update_project_sensitivity",
        "PATCH",
    ),
    # The network off-switch is a safety policy, owner-level like sensitivity;
    # the team app overrides it with a control-plane-syncing twin, same as
    # sensitivity.
    (
        "update_project_network",
        "PATCH",
    ),
    (
        "activate_workbench_plugin",
        "POST",
    ),
    (
        "activate_workbench_plugin_backend",
        "POST",
    ),
    ("delete_project", "DELETE"),
    (
        "delete_project_provider_key",
        "DELETE",
    ),
    ("delete_project_secret", "DELETE"),
    (
        "delete_workbench_plugin_env_var_route",
        "DELETE",
    ),
    (
        "disable_workbench_plugin_route",
        "POST",
    ),
    ("get_project_provider_keys", "GET"),
    ("get_project_secrets", "GET"),
    ("get_project_settings", "GET"),
    (
        "patch_workbench_plugin_settings",
        "PATCH",
    ),
    ("set_project_provider_key", "POST"),
    ("set_project_secret", "POST"),
    ("update_project_settings", "PATCH"),
    (
        "set_workbench_plugin_env_var_route",
        "POST",
    ),
    (
        "uninstall_workbench_plugin_route",
        "POST",
    ),
    (
        "validate_project_provider_key",
        "POST",
    ),
    (
        "workbench_plugin_env_vars",
        "GET",
    ),
    (
        "workbench_plugin_local_install_route",
        "POST",
    ),
    (
        "workbench_plugin_settings",
        "GET",
    ),
)

_TENANT_NOTIFICATION_OWNER: tuple[RouteSpec, ...] = (
    (
        "create_notification_channel",
        "POST",
    ),
    ("create_notification_route", "POST"),
    (
        "patch_notification_channel",
        "PATCH",
    ),
    (
        "patch_notification_route",
        "PATCH",
    ),
)

_TENANT_NOTIFICATION_DELIVER: tuple[RouteSpec, ...] = (
    (
        "deliver_notification",
        "POST",
    ),
)

_TENANT_NOTIFICATION_TEST: tuple[RouteSpec, ...] = (
    (
        "test_notification_route",
        "POST",
    ),
)

_TENANT_ACTION_RUN: tuple[RouteSpec, ...] = (("v1_action_run", "POST"),)

_TENANT_ACTION_PREVIEW: tuple[RouteSpec, ...] = (("v1_action_preview_start", "POST"),)

_TENANT_COPILOT: tuple[RouteSpec, ...] = (("copilot_ep", "POST"),)

_TENANT_EDITOR: tuple[RouteSpec, ...] = (
    (
        "ack_notification",
        "POST",
    ),
    ("action_job_cancel", "POST"),
    ("action_read_range", "POST"),
    ("action_v1_estimate", "POST"),
    (
        "action_v1_validate_params",
        "POST",
    ),
    ("backfill_metadata", "POST"),
    ("bulk_ack_notifications", "POST"),
    ("cancel_run", "POST"),
    ("cluster_preview", "POST"),
    (
        "column_values_preview",
        "POST",
    ),
    ("compact_project", "POST"),
    ("create_lens", "POST"),
    ("create_view", "POST"),
    ("create_watch", "POST"),
    ("delete_lens_ep", "DELETE"),
    ("delete_view_ep", "DELETE"),
    ("delete_watch", "DELETE"),
    (
        "embedding_hybrid_preview",
        "POST",
    ),
    (
        "embedding_index_export",
        "POST",
    ),
    (
        "embedding_similarity_preview",
        "POST",
    ),
    ("emit_notification", "POST"),
    (
        "entity_mention_documents",
        "POST",
    ),
    (
        "entity_mention_occurrences",
        "POST",
    ),
    (
        "entity_mentions_preview",
        "POST",
    ),
    ("import_csv", "POST"),
    ("append_csv", "POST"),
    ("import_csv_preview", "POST"),
    ("import_csv_update_preview", "POST"),
    ("update_csv", "POST"),
    ("import_update_preview", "POST"),
    ("import_xlsx_preview", "POST"),
    ("import_xlsx_update_preview", "POST"),
    ("update_xlsx", "POST"),
    ("append_xlsx", "POST"),
    ("import_bulk_execute", "POST"),
    ("import_bulk_plan", "POST"),
    ("import_files", "POST"),
    ("import_followthemoney", "POST"),
    ("import_paste_draft", "POST"),
    ("import_paste_confirm", "POST"),
    ("import_pdf", "POST"),
    ("import_urls", "POST"),
    ("import_xlsx", "POST"),
    (
        "mark_notification_read",
        "POST",
    ),
    ("mark_notifications_seen", "POST"),
    ("ocr_compare_preview", "POST"),
    ("ocr_compare_scratch", "POST"),
    ("ocr_compare_scratch_estimate", "POST"),
    ("patch_lens", "PATCH"),
    ("patch_view", "PATCH"),
    ("replace_view_definition", "PUT"),
    ("patch_watch", "PATCH"),
    ("query_preview", "POST"),
    (
        "replace_rules_preview",
        "POST",
    ),
    ("run_watch", "POST"),
    (
        "runtime_projection_artifact",
        "POST",
    ),
    (
        "runtime_projection_build",
        "POST",
    ),
    (
        "runtime_projection_status",
        "POST",
    ),
    ("seed_sample_project", "POST"),
    (
        "topic_segmentation_compare_scratch",
        "POST",
    ),
    (
        "transcribe_compare_scratch",
        "POST",
    ),
    ("transcribe_compare_scratch_estimate", "POST"),
    (
        "translate_compare_scratch",
        "POST",
    ),
    (
        "unack_notification",
        "POST",
    ),
    ("update_project", "PATCH"),
    ("update_project_retention", "PATCH"),
    ("delete_sheet", "DELETE"),
    ("update_sheet", "PATCH"),
    (
        "v1_action_preview_cancel",
        "DELETE",
    ),
    (
        "workbench_marketplace_install_attempt_route",
        "POST",
    ),
)

# Browser-projection membership lives on EndpointPolicy rather than in the
# exporter. FastAPI owns each operation's live path/wire shape.
# ADR-001 Amendment 2026-08-03-a fixes alias classes D/E: tenant.create_project.post
# and outer.create_project.post are distinct canonical operations, as are
# tenant.list_projects.get and outer.list_projects.get; neither pair has an alias.
_BROWSER_CLIENT_IDS = (
    frozenset(
        {
            "tenant.ack_notification.post",
            "tenant.action_job_detail.get",
            "tenant.action_jobs.get",
            "tenant.action_v1_estimate.post",
            "tenant.action_v1_validate_params.post",
            "tenant.v1_action_run.post",
            "tenant.v1_action_preview_cancel.delete",
            "tenant.v1_action_preview_start.post",
            "tenant.v1_action_preview_status.get",
            "tenant.action_run_status.get",
            "tenant.action_run_trace_row.get",
            "tenant.cancel_run.post",
            "tenant.cluster_preview.post",
            "tenant.compact_project.post",
            "tenant.cell_evidence.get",
            "tenant.cell_text_annotations.get",
            "tenant.column_runs.get",
            "tenant.column_evidence.get",
            "tenant.column_values_preview.post",
            "tenant.column_stats.get",
            "tenant.copilot_ep.post",
            "tenant.create_lens.post",
            "tenant.create_notification_channel.post",
            "tenant.create_notification_route.post",
            "tenant.create_project.post",
            "tenant.create_view.post",
            "tenant.create_watch.post",
            "tenant.delete_watch.delete",
            "tenant.delete_project.delete",
            "tenant.delete_project_provider_key.delete",
            "tenant.delete_project_secret.delete",
            "tenant.delete_view_ep.delete",
            "tenant.diagnose.get",
            "tenant.embedding_hybrid_preview.post",
            "tenant.embedding_index_export.post",
            "tenant.embedding_indexes.get",
            "tenant.embedding_provider_catalog.get",
            "tenant.embedding_similarity_preview.post",
            "tenant.entity_mention_documents.post",
            "tenant.entity_mention_occurrences.post",
            "tenant.entity_mentions_preview.post",
            "tenant.import_csv.post",
            "tenant.append_csv.post",
            "tenant.import_csv_preview.post",
            "tenant.import_csv_update_preview.post",
            "tenant.update_csv.post",
            "tenant.import_update_preview.post",
            "tenant.import_xlsx_preview.post",
            "tenant.import_xlsx_update_preview.post",
            "tenant.update_xlsx.post",
            "tenant.append_xlsx.post",
            "tenant.import_bulk_execute.post",
            "tenant.import_bulk_plan.post",
            "tenant.import_files.post",
            "tenant.import_followthemoney.post",
            "tenant.import_paste_draft.post",
            "tenant.import_paste_confirm.post",
            "tenant.import_pdf.post",
            "tenant.import_urls.post",
            "tenant.import_xlsx.post",
            "tenant.ocr_compare_scratch.post",
            "tenant.ocr_compare_scratch_estimate.post",
            "tenant.evidence_viewer.get",
            "tenant.get_project.get",
            "tenant.get_project_network.get",
            "tenant.get_project_provider_keys.get",
            "tenant.get_project_retention.get",
            "tenant.get_project_secrets.get",
            "tenant.get_project_settings.get",
            "tenant.project_lineage.get",
            "tenant.history.get",
            "tenant.health.get",
            "tenant.get_source_ep.get",
            "tenant.get_source_health_ep.get",
            "tenant.list_column_types.get",
            "tenant.list_walkthroughs.get",
            "tenant.list_lenses.get",
            "tenant.list_notification_channels.get",
            "tenant.list_notification_delivery_requests.get",
            "tenant.list_notifications.get",
            "tenant.list_notification_routes.get",
            "tenant.list_projects.get",
            "tenant.list_project_column_types.get",
            "tenant.list_sheets.get",
            "tenant.list_sources.get",
            "tenant.list_views.get",
            "tenant.list_watch_run_events.get",
            "tenant.list_watch_runs.get",
            "tenant.list_watches.get",
            "tenant.locate_sheet_row.get",
            "tenant.mark_notification_read.post",
            "tenant.mark_notifications_seen.post",
            "tenant.notifications_summary.get",
            "tenant.patch_view.patch",
            "tenant.replace_view_definition.put",
            "tenant.patch_watch.patch",
            "tenant.patch_notification_channel.patch",
            "tenant.patch_notification_route.patch",
            "tenant.project_attempts.get",
            "tenant.product_telemetry_installation.post",
            "tenant.product_telemetry_project.post",
            "tenant.resolve_lens.get",
            "tenant.replace_rules_preview.post",
            "tenant.review_bundles_ep.get",
            "tenant.review_count_ep.get",
            "tenant.runtime_config.get",
            "tenant.runtime_projection_artifact.post",
            "tenant.runtime_projection_build.post",
            "tenant.runtime_projection_status.post",
            "tenant.seed_sample_project.post",
            "tenant.topic_segmentation_compare_scratch.post",
            "tenant.transcribe_compare_scratch.post",
            "tenant.transcribe_compare_scratch_estimate.post",
            "tenant.run_watch.post",
            "tenant.test_notification_route.post",
            "tenant.translate_compare_scratch.post",
            "tenant.unack_notification.post",
            "tenant.project_v1_action_catalog.get",
            "tenant.project_diagnose.get",
            "tenant.provenance.get",
            "tenant.run_rows.get",
            "tenant.search_ep.get",
            "tenant.sheet_data.get",
            "tenant.sheet_graph.get",
            "tenant.delete_sheet.delete",
            "tenant.update_project.patch",
            # The local policy owns browser membership; projection maps this
            # Team/local twin to outer.update_project_network.patch.
            "tenant.update_project_network.patch",
            "tenant.update_project_retention.patch",
            "tenant.set_project_provider_key.post",
            "tenant.set_project_secret.post",
            "tenant.spend.get",
            "tenant.update_project_settings.patch",
            "tenant.validate_project_provider_key.post",
            "tenant.update_sheet.patch",
            "tenant.v1_action_catalog.get",
            "tenant.v1_receipt_lookup.get",
            "tenant.workbench_plugin_settings.get",
            "tenant.patch_workbench_plugin_settings.patch",
            "tenant.workbench_plugin_local_install_route.post",
            "tenant.activate_workbench_plugin.post",
            "tenant.activate_workbench_plugin_backend.post",
            "tenant.disable_workbench_plugin_route.post",
            "tenant.uninstall_workbench_plugin_route.post",
            "tenant.workbench_plugins.get",
            "tenant.provider_catalog.get",
            "outer.create_token.post",
            "outer.admin_overview.get",
            "outer.admin_browser_audit.get",
            "outer.admin_browser_cancel_job.post",
            "outer.admin_browser_errors.get",
            "outer.admin_browser_health.get",
            "outer.admin_browser_invite_user.post",
            "outer.admin_browser_jobs.get",
            "outer.admin_browser_remove_user.delete",
            "outer.admin_browser_revoke_invite.delete",
            "outer.admin_browser_update_user_role.patch",
            "outer.admin_browser_users.get",
            "outer.client_errors.post",
            "outer.diagnostic_bundle.post",
            "outer.instance_info.get",
            "outer.list_oauth_connections.get",
            "outer.me.get",
            "outer.update_profile.patch",
            "outer.list_org_keys.get",
            "outer.list_org_env_vars.get",
            "outer.list_tokens.get",
            "outer.revoke_token.delete",
            "outer.set_org_key.post",
            "outer.set_org_env_var.post",
            "outer.validate_org_key.post",
            "outer.delete_org_key.delete",
            "outer.delete_org_env_var.delete",
            "outer.org_media_proxy_status.get",
            "outer.list_members.get",
            "outer.set_member.post",
            "outer.remove_member.delete",
            "outer.list_project_invites.get",
            "outer.create_project_invite.post",
            "outer.revoke_project_invite.delete",
        }
    )
    | LOCAL_CONFIGURATION_ENDPOINT_IDS
)

# Group key -> base declarations. The key is the contribution unit: an edition
# adds its own endpoints to an existing group (they merge in canonical order)
# or declares a group of its own.
_BASE_GROUPS: dict[str, tuple[EndpointPolicy, ...]] = {
    "outer.public": declare_endpoints("outer", "public", _OUTER_PUBLIC),
    "outer.admin": declare_endpoints(
        "outer",
        "admin",
        _OUTER_ADMIN,
        pat_forbidden_detail="admin routes require a browser session",
    ),
    "outer.oauth_browser": declare_endpoints(
        "outer",
        "browser_session",
        _OUTER_OAUTH_BROWSER,
        pat_forbidden_detail="OAuth connections require a browser session",
    ),
    "outer.profile_browser": declare_endpoints(
        "outer",
        "browser_session",
        _OUTER_PROFILE_BROWSER,
        pat_forbidden_detail="profile changes require a browser session",
    ),
    "outer.session": declare_endpoints("outer", "session_or_pat", _OUTER_SESSION),
    "tenant.public": declare_endpoints("tenant", "public", _TENANT_PUBLIC),
    "tenant.admin": declare_endpoints("tenant", "admin", _TENANT_ADMIN),
    "tenant.session": declare_endpoints(
        "tenant",
        "session_or_pat",
        _TENANT_SESSION
        + _TENANT_LOCAL_PROVIDERS
        + _TENANT_LOCAL_RUNTIME_SETTINGS
        + _TENANT_LOCAL_MCP,
    ),
    "tenant.spend": declare_endpoints(
        "tenant", "session_or_pat", _TENANT_SPEND, resolvers=("org.spend",)
    ),
    # Mixed-role group: viewer-tier per-cell/per-row reads plus the bulk
    # data-takeout routes one rung up, at reviewer. Kept under the SAME
    # "tenant.viewer" composition key (rather than a new "tenant.reviewer"
    # key) so the hosted edition's fixed _HOSTED_GROUP_ORDER, which already
    # places "tenant.viewer", does not need a matching cross-repo change --
    # per-route project_role still comes from each entry, not the group.
    "tenant.viewer": (
        declare_endpoints(
            "tenant", "session_or_pat", _TENANT_VIEWER, project_role="viewer"
        )
        + declare_endpoints(
            "tenant", "session_or_pat", _TENANT_REVIEWER, project_role="reviewer"
        )
    ),
    "tenant.owner": declare_endpoints(
        "tenant", "session_or_pat", _TENANT_OWNER, project_role="owner"
    ),
    "tenant.notification_deliver": declare_endpoints(
        "tenant",
        "session_or_pat",
        _TENANT_NOTIFICATION_DELIVER,
        project_role="owner",
    ),
    "tenant.notification_owner": declare_endpoints(
        "tenant",
        "session_or_pat",
        _TENANT_NOTIFICATION_OWNER,
        project_role="editor",
        resolvers=("notification.owner",),
    ),
    "tenant.notification_test": declare_endpoints(
        "tenant",
        "session_or_pat",
        _TENANT_NOTIFICATION_TEST,
        project_role="editor",
        resolvers=("notification.route_test",),
    ),
    "tenant.action_run": declare_endpoints(
        "tenant",
        "session_or_pat",
        _TENANT_ACTION_RUN,
        project_role="editor",
        resolvers=("review.decision", "action.code_execution"),
    ),
    "tenant.action_preview": declare_endpoints(
        "tenant",
        "session_or_pat",
        _TENANT_ACTION_PREVIEW,
        project_role="editor",
        resolvers=("action.code_execution",),
    ),
    "tenant.copilot": declare_endpoints(
        "tenant", "session_or_pat", _TENANT_COPILOT, project_role="editor"
    ),
    "tenant.editor": declare_endpoints(
        "tenant", "session_or_pat", _TENANT_EDITOR, project_role="editor"
    ),
}


_declared_browser_client_ids = {
    entry.id
    for entries in _BASE_GROUPS.values()
    for entry in entries
    if entry.id in _BROWSER_CLIENT_IDS
}
if _declared_browser_client_ids != _BROWSER_CLIENT_IDS:
    raise RuntimeError(
        "browser-client endpoint membership references unknown policy IDs: "
        f"{sorted(_BROWSER_CLIENT_IDS - _declared_browser_client_ids)!r}"
    )
_BASE_GROUPS = {
    key: tuple(
        replace(entry, browser_client=True)
        if entry.id in _BROWSER_CLIENT_IDS
        else entry
        for entry in entries
    )
    for key, entries in _BASE_GROUPS.items()
}


def base_endpoint_groups() -> Mapping[str, tuple[EndpointPolicy, ...]]:
    """Base declarations by group key, in canonical composition order."""

    return MappingProxyType(dict(_BASE_GROUPS))


# The one public base catalog: every open/shared endpoint, exactly once.
BASE_ENDPOINT_CATALOG: tuple[EndpointPolicy, ...] = tuple(
    entry for group in _BASE_GROUPS.values() for entry in order_group(group)
)


def _plain(value: Any) -> Mapping[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    raise ValueError(
        f"route policy value must be a mapping or dataclass, got {type(value)!r}"
    )


def _text(row: Mapping[str, Any], *names: str, default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value is not None:
            return str(value)
    return default


def _identity(owner: str, name: str, method: str) -> RouteIdentity:
    clean_owner = owner.strip().lower()
    clean_name = name.strip()
    clean_method = method.strip().upper()
    if clean_owner not in {"outer", "tenant"} or not clean_name:
        raise ValueError("missing or unsupported hosted route owner/name")
    if clean_method not in _SUPPORTED_METHODS:
        raise ValueError(f"unsupported hosted route method: {clean_method!r}")
    return clean_owner, clean_name, clean_method


def _decision(row: Mapping[str, Any]) -> RoutePolicyDecision:
    legacy_keys = sorted({"path", "route_template", "template"} & set(row))
    if legacy_keys:
        raise ValueError(
            f"hosted endpoint policy cannot contain legacy path keys: {legacy_keys!r}"
        )
    owner = _text(row, "route_owner", "owner")
    name = _text(row, "route_name", "registered_route_name", "name")
    method = _text(row, "method")
    identity = _identity(owner, name, method)
    auth = _text(row, "auth", default="session_or_pat")
    if auth not in _AUTH_MODES:
        raise ValueError(f"unsupported hosted auth mode {auth!r} for {identity!r}")
    project_role_value = row.get("project_role", row.get("required_project_role"))
    project_role = None if project_role_value is None else str(project_role_value)
    if project_role is not None and project_role not in _PROJECT_ROLES:
        raise ValueError(f"unsupported project role {project_role!r} for {identity!r}")
    if auth == "public" and project_role is not None:
        raise ValueError(f"impossible public project policy for {identity!r}")
    raw_resolvers = row.get("resolvers", row.get("resolver_ids", ()))
    if raw_resolvers is None:
        raw_resolvers = ()
    if isinstance(raw_resolvers, str):
        raw_resolvers = (raw_resolvers,)
    resolvers = tuple(str(value) for value in raw_resolvers)
    if len(resolvers) != len(set(resolvers)):
        raise ValueError(f"duplicate resolver id for {identity!r}")
    return RoutePolicyDecision(
        id=_text(
            row, "id", default=f"{identity[0]}.{identity[1]}.{identity[2].lower()}"
        ),
        route_owner=identity[0],
        route_name=identity[1],
        method=identity[2],
        auth=auth,
        forwards_to_tenant=bool(row.get("forwards_to_tenant", False)),
        project_role=project_role,
        resolvers=resolvers,
        reserves_funding=bool(
            row.get("reserves_funding", row.get("reserves_hosted_run_funding", False))
        ),
        pat_forbidden_detail=(
            str(row["pat_forbidden_detail"])
            if row.get("pat_forbidden_detail") is not None
            else None
        ),
    )


def _registered(row: Mapping[str, Any]) -> tuple[RouteIdentity, str]:
    identity = _identity(
        _text(row, "route_owner", "owner"),
        _text(row, "route_name", "registered_route_name", "name"),
        _text(row, "method"),
    )
    path = _text(row, "path", "route_template", "template")
    if not path.startswith("/api"):
        raise ValueError(f"missing registered API route template for {identity!r}")
    return identity, path


def compile_route_policy(
    declarations: Sequence[Any],
    registered_routes: Sequence[Any],
    resolvers: Mapping[str, Callable[..., Any]],
) -> CompiledRoutePolicy:
    """Compile one edition's policy; reject incomplete/ambiguous compositions.

    The same compiler serves every edition: pass the edition's declarations,
    the routes that edition actually registered, and the resolvers it can
    honor. A registered route no declaration classifies (e.g. an external
    managed composition's route under a base-only composition) fails closed
    here.
    """

    decisions: dict[RouteIdentity, RoutePolicyDecision] = {}
    for raw in declarations:
        decision = _decision(_plain(raw))
        identity = (decision.route_owner, decision.route_name, decision.method)
        if identity in decisions:
            raise ValueError(
                f"duplicate or ambiguous hosted route policy: {identity!r}"
            )
        decisions[identity] = decision

    inventory: dict[RouteIdentity, str] = {}
    wires: dict[tuple[str, str, str], RouteIdentity] = {}
    for raw in registered_routes:
        identity, path = _registered(_plain(raw))
        if identity in inventory:
            raise ValueError(
                f"duplicate registered hosted route identity: {identity!r}"
            )
        wire = (identity[0], identity[2], path)
        previous_identity = wires.get(wire)
        if previous_identity is not None:
            raise ValueError(
                "duplicate registered hosted route wire for one owner: "
                f"{wire!r} names {previous_identity!r} and {identity!r}"
            )
        inventory[identity] = path
        wires[wire] = identity

    missing = sorted(set(inventory) - set(decisions))
    if missing:
        raise ValueError(
            f"unclassified or missing hosted route policy: {missing[:12]!r}"
        )
    extra = sorted(set(decisions) - set(inventory))
    if extra:
        raise ValueError(
            f"policy declarations are not registered routes: {extra[:12]!r}"
        )

    missing_resolvers = sorted(
        {
            resolver_id
            for decision in decisions.values()
            for resolver_id in decision.resolvers
            if resolver_id not in resolvers
        }
    )
    if missing_resolvers:
        raise ValueError(f"missing hosted route policy resolver: {missing_resolvers!r}")

    return CompiledRoutePolicy(
        _decisions=MappingProxyType(dict(decisions)),
        _resolvers=MappingProxyType(dict(resolvers)),
    )


__all__ = [
    "BASE_ENDPOINT_CATALOG",
    "AuthMode",
    "CompiledRoutePolicy",
    "EndpointPolicy",
    "LOCAL_MCP_ENDPOINT_IDS",
    "LOCAL_PROVIDER_ENDPOINT_IDS",
    "LOCAL_RUNTIME_SETTINGS_ENDPOINT_IDS",
    "LOCAL_CONFIGURATION_ENDPOINT_IDS",
    "ProjectRole",
    "RouteIdentity",
    "RouteOwner",
    "RoutePolicyDecision",
    "RouteSpec",
    "base_endpoint_groups",
    "compile_route_policy",
    "declare_endpoints",
    "order_group",
]
