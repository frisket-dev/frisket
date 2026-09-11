#!/usr/bin/env python3
"""Export the browser OpenAPI surface from the real local/team compositions."""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import types
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_args, get_origin

# App construction lazily imports a large route tree. Those imports are part of
# projection, not output, and must not leave bytecode beside source files.
sys.dont_write_bytecode = True

from fastapi.openapi.utils import get_openapi  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from starlette.routing import BaseRoute, Mount  # noqa: E402

from frisket.contracts.http.endpoint_catalog import (  # noqa: E402
    BASE_ENDPOINT_CATALOG,
    EndpointPolicy,
)

RouteIdentity = tuple[str, str, str]
WireIdentity = tuple[str, str]


class ExportError(RuntimeError):
    """The effective browser surface cannot be projected safely."""


@dataclass(frozen=True)
class Composition:
    edition: str
    app: Any
    root_owner: str


@dataclass(frozen=True)
class EffectiveRoute:
    edition: str
    owner: str
    name: str
    method: str
    path: str
    route: APIRoute

    @property
    def identity(self) -> RouteIdentity:
        return self.owner, self.name, self.method

    @property
    def wire(self) -> WireIdentity:
        return self.method, self.path


@dataclass(frozen=True)
class _Projection:
    record: EffectiveRoute
    operation_id: str
    operation: dict[str, Any]
    components: dict[str, Any]
    schema_fingerprint: str


# D/E are per-edition canonical operations, not aliases.
CANONICAL_OPERATION_IDS: Mapping[RouteIdentity, str] = MappingProxyType(
    {
        ("tenant", "ack_notification", "POST"): "tenant.ack_notification.post",
        ("tenant", "action_job_detail", "GET"): "tenant.action_job_detail.get",
        ("tenant", "action_jobs", "GET"): "tenant.action_jobs.get",
        ("tenant", "action_v1_estimate", "POST"): "tenant.action_v1_estimate.post",
        (
            "tenant",
            "action_v1_validate_params",
            "POST",
        ): "tenant.action_v1_validate_params.post",
        ("tenant", "v1_action_run", "POST"): "tenant.v1_action_run.post",
        (
            "tenant",
            "v1_action_preview_cancel",
            "DELETE",
        ): "tenant.v1_action_preview_cancel.delete",
        (
            "tenant",
            "v1_action_preview_start",
            "POST",
        ): "tenant.v1_action_preview_start.post",
        (
            "tenant",
            "v1_action_preview_status",
            "GET",
        ): "tenant.v1_action_preview_status.get",
        ("tenant", "action_run_status", "GET"): "tenant.action_run_status.get",
        (
            "tenant",
            "action_run_trace_row",
            "GET",
        ): "tenant.action_run_trace_row.get",
        ("tenant", "cancel_run", "POST"): "tenant.cancel_run.post",
        ("tenant", "cluster_preview", "POST"): "tenant.cluster_preview.post",
        ("tenant", "cell_evidence", "GET"): "tenant.cell_evidence.get",
        (
            "tenant",
            "cell_text_annotations",
            "GET",
        ): "tenant.cell_text_annotations.get",
        ("tenant", "column_runs", "GET"): "tenant.column_runs.get",
        (
            "tenant",
            "reviewed_run_revision",
            "GET",
        ): "tenant.reviewed_run_revision.get",
        ("tenant", "column_evidence", "GET"): "tenant.column_evidence.get",
        ("tenant", "column_stats", "GET"): "tenant.column_stats.get",
        (
            "tenant",
            "column_values_preview",
            "POST",
        ): "tenant.column_values_preview.post",
        ("tenant", "copilot_ep", "POST"): "tenant.copilot_ep.post",
        ("tenant", "create_lens", "POST"): "tenant.create_lens.post",
        (
            "tenant",
            "create_notification_channel",
            "POST",
        ): "tenant.create_notification_channel.post",
        (
            "tenant",
            "create_notification_route",
            "POST",
        ): "tenant.create_notification_route.post",
        ("tenant", "create_view", "POST"): "tenant.create_view.post",
        ("tenant", "create_watch", "POST"): "tenant.create_watch.post",
        ("tenant", "delete_watch", "DELETE"): "tenant.delete_watch.delete",
        ("tenant", "delete_project", "DELETE"): "tenant.delete_project.delete",
        ("tenant", "delete_view_ep", "DELETE"): "tenant.delete_view_ep.delete",
        ("tenant", "diagnose", "GET"): "tenant.diagnose.get",
        (
            "tenant",
            "embedding_hybrid_preview",
            "POST",
        ): "tenant.embedding_hybrid_preview.post",
        (
            "tenant",
            "embedding_index_export",
            "POST",
        ): "tenant.embedding_index_export.post",
        ("tenant", "embedding_indexes", "GET"): "tenant.embedding_indexes.get",
        (
            "tenant",
            "embedding_provider_catalog",
            "GET",
        ): "tenant.embedding_provider_catalog.get",
        (
            "tenant",
            "embedding_similarity_preview",
            "POST",
        ): "tenant.embedding_similarity_preview.post",
        (
            "tenant",
            "entity_mention_documents",
            "POST",
        ): "tenant.entity_mention_documents.post",
        (
            "tenant",
            "entity_mention_occurrences",
            "POST",
        ): "tenant.entity_mention_occurrences.post",
        (
            "tenant",
            "entity_mentions_preview",
            "POST",
        ): "tenant.entity_mentions_preview.post",
        (
            "tenant",
            "import_paste_draft",
            "POST",
        ): "tenant.import_paste_draft.post",
        ("tenant", "import_paste_confirm", "POST"): "tenant.import_paste_confirm.post",
        ("tenant", "import_csv", "POST"): "tenant.import_csv.post",
        ("tenant", "append_csv", "POST"): "tenant.append_csv.post",
        ("tenant", "update_csv", "POST"): "tenant.update_csv.post",
        (
            "tenant",
            "import_csv_update_preview",
            "POST",
        ): "tenant.import_csv_update_preview.post",
        (
            "tenant",
            "import_update_preview",
            "POST",
        ): "tenant.import_update_preview.post",
        ("tenant", "append_xlsx", "POST"): "tenant.append_xlsx.post",
        ("tenant", "import_xlsx_preview", "POST"): "tenant.import_xlsx_preview.post",
        (
            "tenant",
            "import_xlsx_update_preview",
            "POST",
        ): "tenant.import_xlsx_update_preview.post",
        ("tenant", "update_xlsx", "POST"): "tenant.update_xlsx.post",
        (
            "tenant",
            "import_bulk_execute",
            "POST",
        ): "tenant.import_bulk_execute.post",
        (
            "tenant",
            "import_bulk_plan",
            "POST",
        ): "tenant.import_bulk_plan.post",
        (
            "tenant",
            "import_csv_preview",
            "POST",
        ): "tenant.import_csv_preview.post",
        ("tenant", "import_files", "POST"): "tenant.import_files.post",
        (
            "tenant",
            "import_followthemoney",
            "POST",
        ): "tenant.import_followthemoney.post",
        ("tenant", "import_pdf", "POST"): "tenant.import_pdf.post",
        ("tenant", "import_urls", "POST"): "tenant.import_urls.post",
        ("tenant", "import_xlsx", "POST"): "tenant.import_xlsx.post",
        (
            "tenant",
            "ocr_compare_scratch",
            "POST",
        ): "tenant.ocr_compare_scratch.post",
        (
            "tenant",
            "ocr_compare_scratch_estimate",
            "POST",
        ): "tenant.ocr_compare_scratch_estimate.post",
        ("tenant", "evidence_viewer", "GET"): "tenant.evidence_viewer.get",
        ("tenant", "get_project", "GET"): "tenant.get_project.get",
        ("tenant", "get_project_network", "GET"): "tenant.get_project_network.get",
        ("tenant", "get_project_provider_keys", "GET"): (
            "tenant.get_project_provider_keys.get"
        ),
        ("tenant", "get_project_retention", "GET"): (
            "tenant.get_project_retention.get"
        ),
        ("tenant", "get_project_secrets", "GET"): "tenant.get_project_secrets.get",
        ("tenant", "get_project_settings", "GET"): "tenant.get_project_settings.get",
        ("tenant", "history", "GET"): "tenant.history.get",
        ("tenant", "get_source_ep", "GET"): "tenant.get_source_ep.get",
        (
            "tenant",
            "get_source_health_ep",
            "GET",
        ): "tenant.get_source_health_ep.get",
        ("tenant", "list_column_types", "GET"): "tenant.list_column_types.get",
        ("tenant", "list_walkthroughs", "GET"): "tenant.list_walkthroughs.get",
        ("tenant", "list_lenses", "GET"): "tenant.list_lenses.get",
        (
            "tenant",
            "list_notification_channels",
            "GET",
        ): "tenant.list_notification_channels.get",
        (
            "tenant",
            "list_notification_delivery_requests",
            "GET",
        ): "tenant.list_notification_delivery_requests.get",
        (
            "tenant",
            "list_notifications",
            "GET",
        ): "tenant.list_notifications.get",
        (
            "tenant",
            "list_notification_routes",
            "GET",
        ): "tenant.list_notification_routes.get",
        (
            "tenant",
            "list_project_column_types",
            "GET",
        ): "tenant.list_project_column_types.get",
        ("tenant", "list_sheets", "GET"): "tenant.list_sheets.get",
        ("tenant", "list_sources", "GET"): "tenant.list_sources.get",
        ("tenant", "list_views", "GET"): "tenant.list_views.get",
        (
            "tenant",
            "list_watch_run_events",
            "GET",
        ): "tenant.list_watch_run_events.get",
        ("tenant", "list_watch_runs", "GET"): "tenant.list_watch_runs.get",
        ("tenant", "list_watches", "GET"): "tenant.list_watches.get",
        ("tenant", "locate_sheet_row", "GET"): "tenant.locate_sheet_row.get",
        (
            "tenant",
            "mark_notification_read",
            "POST",
        ): "tenant.mark_notification_read.post",
        (
            "tenant",
            "mark_notifications_seen",
            "POST",
        ): "tenant.mark_notifications_seen.post",
        (
            "tenant",
            "notifications_summary",
            "GET",
        ): "tenant.notifications_summary.get",
        ("tenant", "patch_view", "PATCH"): "tenant.patch_view.patch",
        (
            "tenant",
            "replace_view_definition",
            "PUT",
        ): "tenant.replace_view_definition.put",
        ("tenant", "patch_watch", "PATCH"): "tenant.patch_watch.patch",
        (
            "tenant",
            "patch_notification_channel",
            "PATCH",
        ): "tenant.patch_notification_channel.patch",
        (
            "tenant",
            "patch_notification_route",
            "PATCH",
        ): "tenant.patch_notification_route.patch",
        ("tenant", "project_v1_action_catalog", "GET"): (
            "tenant.project_v1_action_catalog.get"
        ),
        ("tenant", "project_diagnose", "GET"): "tenant.project_diagnose.get",
        ("tenant", "project_lineage", "GET"): "tenant.project_lineage.get",
        ("tenant", "provenance", "GET"): "tenant.provenance.get",
        ("tenant", "resolve_lens", "GET"): "tenant.resolve_lens.get",
        (
            "tenant",
            "replace_rules_preview",
            "POST",
        ): "tenant.replace_rules_preview.post",
        (
            "tenant",
            "review_bundles_ep",
            "GET",
        ): "tenant.review_bundles_ep.get",
        ("tenant", "review_count_ep", "GET"): "tenant.review_count_ep.get",
        (
            "tenant",
            "runtime_projection_artifact",
            "POST",
        ): "tenant.runtime_projection_artifact.post",
        (
            "tenant",
            "runtime_projection_build",
            "POST",
        ): "tenant.runtime_projection_build.post",
        (
            "tenant",
            "runtime_projection_status",
            "POST",
        ): "tenant.runtime_projection_status.post",
        ("tenant", "seed_sample_project", "POST"): ("tenant.seed_sample_project.post"),
        ("tenant", "run_watch", "POST"): "tenant.run_watch.post",
        (
            "tenant",
            "test_notification_route",
            "POST",
        ): "tenant.test_notification_route.post",
        (
            "tenant",
            "topic_segmentation_compare_scratch",
            "POST",
        ): "tenant.topic_segmentation_compare_scratch.post",
        (
            "tenant",
            "transcribe_compare_scratch",
            "POST",
        ): "tenant.transcribe_compare_scratch.post",
        (
            "tenant",
            "transcribe_compare_scratch_estimate",
            "POST",
        ): "tenant.transcribe_compare_scratch_estimate.post",
        (
            "tenant",
            "translate_compare_scratch",
            "POST",
        ): "tenant.translate_compare_scratch.post",
        (
            "tenant",
            "unack_notification",
            "POST",
        ): "tenant.unack_notification.post",
        ("tenant", "run_rows", "GET"): "tenant.run_rows.get",
        ("tenant", "search_ep", "GET"): "tenant.search_ep.get",
        ("tenant", "sheet_data", "GET"): "tenant.sheet_data.get",
        ("tenant", "sheet_graph", "GET"): "tenant.sheet_graph.get",
        ("tenant", "spend", "GET"): "tenant.spend.get",
        ("tenant", "project_attempts", "GET"): "tenant.project_attempts.get",
        ("tenant", "update_project", "PATCH"): "tenant.update_project.patch",
        ("tenant", "update_project_retention", "PATCH"): (
            "tenant.update_project_retention.patch"
        ),
        ("tenant", "compact_project", "POST"): "tenant.compact_project.post",
        ("tenant", "delete_project_provider_key", "DELETE"): (
            "tenant.delete_project_provider_key.delete"
        ),
        ("tenant", "delete_project_secret", "DELETE"): (
            "tenant.delete_project_secret.delete"
        ),
        ("tenant", "set_project_provider_key", "POST"): (
            "tenant.set_project_provider_key.post"
        ),
        ("tenant", "set_project_secret", "POST"): "tenant.set_project_secret.post",
        ("tenant", "update_project_settings", "PATCH"): (
            "tenant.update_project_settings.patch"
        ),
        ("tenant", "validate_project_provider_key", "POST"): (
            "tenant.validate_project_provider_key.post"
        ),
        ("tenant", "update_sheet", "PATCH"): "tenant.update_sheet.patch",
        ("tenant", "delete_sheet", "DELETE"): "tenant.delete_sheet.delete",
        ("tenant", "v1_action_catalog", "GET"): "tenant.v1_action_catalog.get",
        ("tenant", "v1_receipt_lookup", "GET"): "tenant.v1_receipt_lookup.get",
        (
            "tenant",
            "workbench_plugin_settings",
            "GET",
        ): "tenant.workbench_plugin_settings.get",
        (
            "tenant",
            "patch_workbench_plugin_settings",
            "PATCH",
        ): "tenant.patch_workbench_plugin_settings.patch",
        (
            "tenant",
            "workbench_plugin_local_install_route",
            "POST",
        ): "tenant.workbench_plugin_local_install_route.post",
        (
            "tenant",
            "activate_workbench_plugin",
            "POST",
        ): "tenant.activate_workbench_plugin.post",
        (
            "tenant",
            "activate_workbench_plugin_backend",
            "POST",
        ): "tenant.activate_workbench_plugin_backend.post",
        (
            "tenant",
            "disable_workbench_plugin_route",
            "POST",
        ): "tenant.disable_workbench_plugin_route.post",
        (
            "tenant",
            "uninstall_workbench_plugin_route",
            "POST",
        ): "tenant.uninstall_workbench_plugin_route.post",
        ("tenant", "workbench_plugins", "GET"): "tenant.workbench_plugins.get",
        ("tenant", "cancel_model_pull", "POST"): "tenant.cancel_model_pull.post",
        (
            "tenant",
            "create_local_endpoint",
            "POST",
        ): "tenant.create_local_endpoint.post",
        (
            "tenant",
            "discover_local_endpoints",
            "POST",
        ): "tenant.discover_local_endpoints.post",
        (
            "tenant",
            "delete_local_endpoint",
            "DELETE",
        ): "tenant.delete_local_endpoint.delete",
        (
            "tenant",
            "delete_provider_key",
            "DELETE",
        ): "tenant.delete_provider_key.delete",
        ("tenant", "get_model_pull", "GET"): "tenant.get_model_pull.get",
        ("tenant", "list_model_pulls", "GET"): "tenant.list_model_pulls.get",
        ("tenant", "list_providers", "GET"): "tenant.list_providers.get",
        ("tenant", "provider_status", "GET"): "tenant.provider_status.get",
        ("tenant", "pull_artifact", "POST"): "tenant.pull_artifact.post",
        (
            "tenant",
            "update_local_endpoint",
            "PATCH",
        ): "tenant.update_local_endpoint.patch",
        ("tenant", "set_provider_key", "PUT"): "tenant.set_provider_key.put",
        (
            "tenant",
            "uninstall_artifact",
            "POST",
        ): "tenant.uninstall_artifact.post",
        (
            "tenant",
            "validate_provider_key",
            "POST",
        ): "tenant.validate_provider_key.post",
        (
            "tenant",
            "create_mcp_server",
            "POST",
        ): "tenant.create_mcp_server.post",
        (
            "tenant",
            "delete_mcp_server",
            "DELETE",
        ): "tenant.delete_mcp_server.delete",
        (
            "tenant",
            "import_mcp_servers",
            "POST",
        ): "tenant.import_mcp_servers.post",
        (
            "tenant",
            "list_mcp_servers",
            "GET",
        ): "tenant.list_mcp_servers.get",
        (
            "tenant",
            "test_mcp_server",
            "POST",
        ): "tenant.test_mcp_server.post",
        (
            "tenant",
            "update_mcp_server",
            "PATCH",
        ): "tenant.update_mcp_server.patch",
        ("outer", "health", "GET"): "tenant.health.get",
        ("tenant", "health", "GET"): "tenant.health.get",
        ("outer", "runtime_config", "GET"): "tenant.runtime_config.get",
        ("tenant", "runtime_config", "GET"): "tenant.runtime_config.get",
        (
            "tenant",
            "product_telemetry_installation",
            "POST",
        ): "tenant.product_telemetry_installation.post",
        (
            "tenant",
            "product_telemetry_project",
            "POST",
        ): "tenant.product_telemetry_project.post",
        (
            "tenant",
            "update_runtime_config",
            "PATCH",
        ): "tenant.update_runtime_config.patch",
        ("outer", "instance_info", "GET"): "outer.instance_info.get",
        (
            "outer",
            "list_oauth_connections",
            "GET",
        ): "outer.list_oauth_connections.get",
        ("outer", "me", "GET"): "outer.me.get",
        ("outer", "update_profile", "PATCH"): "outer.update_profile.patch",
        ("outer", "client_errors", "POST"): "outer.client_errors.post",
        ("outer", "diagnostic_bundle", "POST"): "outer.diagnostic_bundle.post",
        ("outer", "admin_overview", "GET"): "outer.admin_overview.get",
        ("outer", "admin_browser_audit", "GET"): "outer.admin_browser_audit.get",
        (
            "outer",
            "admin_browser_cancel_job",
            "POST",
        ): "outer.admin_browser_cancel_job.post",
        ("outer", "admin_browser_errors", "GET"): "outer.admin_browser_errors.get",
        ("outer", "admin_browser_health", "GET"): "outer.admin_browser_health.get",
        (
            "outer",
            "admin_browser_invite_user",
            "POST",
        ): "outer.admin_browser_invite_user.post",
        ("outer", "admin_browser_jobs", "GET"): "outer.admin_browser_jobs.get",
        (
            "outer",
            "admin_browser_remove_user",
            "DELETE",
        ): "outer.admin_browser_remove_user.delete",
        (
            "outer",
            "admin_browser_revoke_invite",
            "DELETE",
        ): "outer.admin_browser_revoke_invite.delete",
        (
            "outer",
            "admin_browser_update_user_role",
            "PATCH",
        ): "outer.admin_browser_update_user_role.patch",
        ("outer", "admin_browser_users", "GET"): "outer.admin_browser_users.get",
        ("outer", "create_token", "POST"): "outer.create_token.post",
        ("outer", "list_tokens", "GET"): "outer.list_tokens.get",
        ("outer", "revoke_token", "DELETE"): "outer.revoke_token.delete",
        ("outer", "get_project_blob", "GET"): "tenant.get_blob.get",
        ("tenant", "get_blob", "GET"): "tenant.get_blob.get",
        (
            "outer",
            "update_project_sensitivity",
            "PATCH",
        ): "outer.update_project_sensitivity.patch",
        (
            "tenant",
            "update_project_sensitivity",
            "PATCH",
        ): "outer.update_project_sensitivity.patch",
        ("tenant", "update_project_network", "PATCH"): (
            "outer.update_project_network.patch"
        ),
        ("outer", "update_project_network", "PATCH"): (
            "outer.update_project_network.patch"
        ),
        ("tenant", "create_project", "POST"): "tenant.create_project.post",
        ("outer", "create_project", "POST"): "outer.create_project.post",
        ("tenant", "list_projects", "GET"): "tenant.list_projects.get",
        ("outer", "list_projects", "GET"): "outer.list_projects.get",
        (
            "tenant",
            "provider_catalog",
            "GET",
        ): "tenant.provider_catalog.get",
        ("outer", "list_org_keys", "GET"): "outer.list_org_keys.get",
        ("outer", "set_org_key", "POST"): "outer.set_org_key.post",
        ("outer", "list_org_env_vars", "GET"): "outer.list_org_env_vars.get",
        ("outer", "set_org_env_var", "POST"): "outer.set_org_env_var.post",
        (
            "outer",
            "delete_org_env_var",
            "DELETE",
        ): "outer.delete_org_env_var.delete",
        (
            "outer",
            "org_media_proxy_status",
            "GET",
        ): "outer.org_media_proxy_status.get",
        (
            "outer",
            "validate_org_key",
            "POST",
        ): "outer.validate_org_key.post",
        (
            "outer",
            "delete_org_key",
            "DELETE",
        ): "outer.delete_org_key.delete",
        ("outer", "list_members", "GET"): "outer.list_members.get",
        ("outer", "set_member", "POST"): "outer.set_member.post",
        ("outer", "remove_member", "DELETE"): "outer.remove_member.delete",
        ("outer", "list_project_invites", "GET"): "outer.list_project_invites.get",
        ("outer", "create_project_invite", "POST"): "outer.create_project_invite.post",
        (
            "outer",
            "revoke_project_invite",
            "DELETE",
        ): "outer.revoke_project_invite.delete",
    }
)

# Route-order facts, not operation aliases.
INTENTIONAL_DISTINCT_SHADOWS = frozenset(
    {
        (
            ("outer", "create_project", "POST"),
            ("tenant", "create_project", "POST"),
        ),
        (
            ("outer", "delete_project", "DELETE"),
            ("tenant", "delete_project", "DELETE"),
        ),
        (
            ("outer", "list_projects", "GET"),
            ("tenant", "list_projects", "GET"),
        ),
    }
)


def _join_path(prefix: str, path: str) -> str:
    return (prefix.rstrip("/") + "/" + path.lstrip("/")).rstrip("/") or "/"


def _flatten_routes(
    routes: Sequence[BaseRoute],
    *,
    edition: str,
    prefix: str = "",
    owner: str,
) -> list[EffectiveRoute]:
    flattened: list[EffectiveRoute] = []
    for route in routes:
        if isinstance(route, Mount):
            child_owner = "tenant" if owner == "outer" else owner
            flattened.extend(
                _flatten_routes(
                    route.routes,
                    edition=edition,
                    prefix=_join_path(prefix, route.path),
                    owner=child_owner,
                )
            )
            continue
        if not isinstance(route, APIRoute):
            continue
        path = _join_path(prefix, route.path)
        for method in sorted(route.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            flattened.append(
                EffectiveRoute(
                    edition=edition,
                    owner=owner,
                    name=str(route.name),
                    method=method,
                    path=path,
                    route=route,
                )
            )
    return flattened


class _PolicyProjection:
    def __init__(
        self,
        policies: Sequence[EndpointPolicy],
        canonical_ids: Mapping[RouteIdentity, str],
        *,
        require_canonical_ids: bool,
    ) -> None:
        self._policies: dict[RouteIdentity, EndpointPolicy] = {}
        self._canonical_ids = canonical_ids
        for policy in policies:
            identity = (
                policy.route_owner,
                policy.route_name,
                policy.method.upper(),
            )
            if identity in self._policies:
                raise ExportError(f"duplicate endpoint-policy identity: {identity!r}")
            self._policies[identity] = policy
        self._members_by_id: dict[str, bool] = {}
        for identity, policy in self._policies.items():
            if (
                require_canonical_ids
                and policy.browser_client
                and identity not in canonical_ids
            ):
                raise ExportError(
                    f"browser member lacks canonical mapping: {identity!r}"
                )
            operation_id = self.operation_id(identity)
            if policy.browser_client and not operation_id:
                raise ExportError(
                    f"browser member lacks an explicit operation ID: {identity!r}"
                )
            if operation_id:
                self._members_by_id[operation_id] = (
                    self._members_by_id.get(operation_id, False)
                    or policy.browser_client
                )

    def operation_id(self, identity: RouteIdentity) -> str | None:
        canonical = self._canonical_ids.get(identity)
        if canonical is not None:
            return canonical
        policy = self._policies.get(identity)
        return policy.id if policy is not None and policy.id.strip() else None

    def is_browser_member(self, identity: RouteIdentity) -> bool:
        operation_id = self.operation_id(identity)
        return bool(operation_id and self._members_by_id.get(operation_id, False))


def _contains_untyped_json(model: Any) -> bool:
    if model in {Any, object, dict, list, set, tuple}:
        return True
    if model is None or model is type(None):
        return False
    origin = get_origin(model)
    if origin is None:
        return False
    args = get_args(model)
    if origin in {dict, list, set, tuple} and not args:
        return True
    if origin in {types.UnionType}:
        return any(_contains_untyped_json(arg) for arg in args)
    return any(_contains_untyped_json(arg) for arg in args)


def _success_responses(operation: Mapping[str, Any]) -> list[tuple[str, Any]]:
    success: list[tuple[str, Any]] = []
    for status, response in operation.get("responses", {}).items():
        try:
            code = int(status)
        except (TypeError, ValueError):
            continue
        if 200 <= code < 300:
            success.append((str(status), response))
    return success


def _json_media_type(media_type: str) -> bool:
    clean = media_type.partition(";")[0].strip().lower()
    return clean == "application/json" or clean.endswith("+json")


def _native_form_data_request(
    record: EffectiveRoute, content: Mapping[str, Any]
) -> bool:
    """The fixed browser upload allowlist passes built FormData to fetch."""

    return record.identity in {
        ("tenant", "import_csv", "POST"),
        ("tenant", "append_csv", "POST"),
        ("tenant", "update_csv", "POST"),
        ("tenant", "import_csv_update_preview", "POST"),
        ("tenant", "append_xlsx", "POST"),
        ("tenant", "import_xlsx_preview", "POST"),
        ("tenant", "import_xlsx_update_preview", "POST"),
        ("tenant", "update_xlsx", "POST"),
        ("tenant", "import_bulk_plan", "POST"),
        ("tenant", "import_csv_preview", "POST"),
        ("tenant", "import_xlsx", "POST"),
        ("tenant", "import_pdf", "POST"),
        ("tenant", "import_files", "POST"),
        ("tenant", "import_followthemoney", "POST"),
        ("tenant", "ocr_compare_scratch", "POST"),
        ("tenant", "ocr_compare_scratch_estimate", "POST"),
        ("tenant", "transcribe_compare_scratch", "POST"),
        ("tenant", "transcribe_compare_scratch_estimate", "POST"),
        ("tenant", "topic_segmentation_compare_scratch", "POST"),
    } and set(content) == {"multipart/form-data"}


def _validate_content(record: EffectiveRoute, operation: Mapping[str, Any]) -> None:
    request_body = operation.get("requestBody")
    if isinstance(request_body, Mapping):
        request_content = request_body.get("content", {})
        if not isinstance(request_content, Mapping):
            raise ExportError(
                f"invalid request content for {record.identity!r}: expected object"
            )
        if _native_form_data_request(record, request_content):
            request_content = {}
        unsupported = sorted(
            media_type
            for media_type in request_content
            if not _json_media_type(str(media_type))
        )
        if unsupported:
            raise ExportError(
                f"unsupported request content for {record.identity!r}: {unsupported!r}"
            )

    success = _success_responses(operation)
    if not success:
        raise ExportError(f"no declared success response for {record.identity!r}")
    has_json = False
    for status, response in success:
        content = response.get("content", {}) if isinstance(response, Mapping) else {}
        if not content:
            if status != "204":
                raise ExportError(
                    f"untyped JSON response for {record.identity!r}: status {status}"
                )
            continue
        unsupported = sorted(
            media_type
            for media_type in content
            if not _json_media_type(str(media_type))
        )
        if unsupported:
            raise ExportError(
                f"unsupported response content for {record.identity!r}: {unsupported!r}"
            )
        has_json = True
    if has_json and _contains_untyped_json(record.route.response_model):
        raise ExportError(f"untyped JSON response for {record.identity!r}")


def _schema_contract(
    operation: Mapping[str, Any], components: Mapping[str, Any]
) -> str:
    contract = {
        key: operation[key]
        for key in ("parameters", "requestBody", "responses", "callbacks")
        if key in operation
    }
    return json.dumps(
        {"operation": contract, "components": components},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _explicit_response(route: APIRoute, status: int) -> bool:
    for raw_status in route.responses:
        try:
            if int(raw_status) == status:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _is_framework_validation_response(response: object) -> bool:
    if not isinstance(response, Mapping):
        return False
    content = response.get("content")
    if not isinstance(content, Mapping):
        return False
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        return False
    return media.get("schema") == {"$ref": "#/components/schemas/HTTPValidationError"}


def _remove_unreachable_implicit_422(
    record: EffectiveRoute, operation: dict[str, Any]
) -> None:
    """Remove only FastAPI's impossible plain-string-path validation response."""

    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return
    response = responses.get("422")
    if (
        response is None
        or _explicit_response(record.route, 422)
        or not _is_framework_validation_response(response)
        or "requestBody" in operation
        or record.route.body_field is not None
    ):
        return

    dependant = record.route.dependant
    if any(
        getattr(dependant, field_name, ())
        for field_name in (
            "body_params",
            "query_params",
            "header_params",
            "cookie_params",
            "dependencies",
        )
    ):
        return

    parameters = operation.get("parameters", ())
    if (
        not isinstance(parameters, Sequence)
        or isinstance(parameters, (str, bytes))
        or not parameters
        or len(parameters) != len(dependant.path_params)
    ):
        return

    # OpenAPI can flatten Annotated[str, AfterValidator(...)] to a bare string
    # schema, so prove plain-string identity against FastAPI's live fields too.
    path_fields_by_alias: dict[str, Any] = {}
    for field in dependant.path_params:
        alias = field.alias
        if (
            not isinstance(alias, str)
            or not alias
            or alias in path_fields_by_alias
            or field.field_info.annotation is not str
            or field.field_info.metadata
        ):
            return
        path_fields_by_alias[alias] = field

    matched_names: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, Mapping):
            return
        name = parameter.get("name")
        schema = parameter.get("schema")
        if (
            not isinstance(name, str)
            or name in matched_names
            or name not in path_fields_by_alias
            or parameter.get("in") != "path"
            or parameter.get("required") is not True
            or not isinstance(schema, Mapping)
            or schema.get("type") != "string"
            or not set(schema).issubset({"title", "type"})
        ):
            return
        matched_names.add(name)
    if matched_names != set(path_fields_by_alias):
        return
    del responses["422"]


def _project_route(record: EffectiveRoute, operation_id: str) -> _Projection:
    schema = get_openapi(
        title="frisket browser API",
        version="0.1.0",
        openapi_version="3.1.0",
        routes=[record.route],
    )
    route_path = record.route.path_format
    try:
        raw_operation = schema["paths"][route_path][record.method.lower()]
    except KeyError as exc:
        raise ExportError(
            f"FastAPI did not project {record.identity!r} at {route_path!r}"
        ) from exc
    operation = dict(raw_operation)
    operation["operationId"] = operation_id
    if record.route.summary is None:
        operation.pop("summary", None)
    _remove_unreachable_implicit_422(record, operation)
    _validate_content(record, operation)
    components = dict(schema.get("components", {}))
    return _Projection(
        record=record,
        operation_id=operation_id,
        operation=operation,
        components=components,
        schema_fingerprint=_schema_contract(operation, components),
    )


def _intentional_shadow(
    winner: EffectiveRoute,
    loser: EffectiveRoute,
    policies: _PolicyProjection,
    intentional_distinct_shadows: frozenset[tuple[RouteIdentity, RouteIdentity]],
) -> bool:
    winner_id = policies.operation_id(winner.identity)
    loser_id = policies.operation_id(loser.identity)
    if winner_id is not None and winner_id == loser_id:
        return True
    return (winner.identity, loser.identity) in intentional_distinct_shadows


def _effective_routes(
    composition: Composition,
    policies: _PolicyProjection,
    intentional_distinct_shadows: frozenset[tuple[RouteIdentity, RouteIdentity]],
) -> list[EffectiveRoute]:
    effective: dict[WireIdentity, EffectiveRoute] = {}
    for record in _flatten_routes(
        composition.app.routes,
        edition=composition.edition,
        owner=composition.root_owner,
    ):
        winner = effective.get(record.wire)
        if winner is None:
            effective[record.wire] = record
            continue
        if _intentional_shadow(winner, record, policies, intentional_distinct_shadows):
            continue
        winner_marked = policies.is_browser_member(winner.identity)
        loser_marked = policies.is_browser_member(record.identity)
        if winner_marked and loser_marked:
            raise ExportError(
                "ambiguous selected/effective method-path pair "
                f"{record.wire!r}: {winner.identity!r} and {record.identity!r}"
            )
        if loser_marked:
            raise ExportError(
                f"browser operation {record.identity!r} is unexpectedly shadowed "
                f"by {winner.identity!r} at {record.wire!r}"
            )
    return list(effective.values())


def _merge_components(
    target: dict[str, Any], incoming: Mapping[str, Any], operation_id: str
) -> None:
    for category, raw_values in incoming.items():
        if not isinstance(raw_values, Mapping):
            if category in target and target[category] != raw_values:
                raise ExportError(
                    f"OpenAPI component conflict in {category!r} for {operation_id!r}"
                )
            target[category] = raw_values
            continue
        values = target.setdefault(category, {})
        for name, value in raw_values.items():
            if name in values and values[name] != value:
                raise ExportError(
                    f"OpenAPI component conflict for {category}.{name} "
                    f"while projecting {operation_id!r}"
                )
            values[name] = value


def export_compositions(
    compositions: Sequence[Composition],
    *,
    policies: Sequence[EndpointPolicy],
    canonical_ids: Mapping[RouteIdentity, str] = CANONICAL_OPERATION_IDS,
    intentional_distinct_shadows: frozenset[
        tuple[RouteIdentity, RouteIdentity]
    ] = INTENTIONAL_DISTINCT_SHADOWS,
    require_canonical_ids: bool = False,
) -> dict[str, Any]:
    """Project selected effective routes and merge declared-same operations."""

    policy_projection = _PolicyProjection(
        policies,
        canonical_ids,
        require_canonical_ids=require_canonical_ids,
    )
    projections: list[_Projection] = []
    for composition in compositions:
        for record in _effective_routes(
            composition, policy_projection, intentional_distinct_shadows
        ):
            if not policy_projection.is_browser_member(record.identity):
                continue
            operation_id = policy_projection.operation_id(record.identity)
            if operation_id is None:
                raise ExportError(
                    f"browser member lacks canonical operation ID: {record.identity!r}"
                )
            projections.append(_project_route(record, operation_id))

    by_id: dict[str, list[_Projection]] = {}
    by_wire: dict[WireIdentity, str] = {}
    for projection in projections:
        peers = by_id.setdefault(projection.operation_id, [])
        if peers:
            first = peers[0]
            if first.record.wire != projection.record.wire:
                raise ExportError(
                    f"duplicate operationId {projection.operation_id!r} names "
                    f"{first.record.wire!r} and {projection.record.wire!r}"
                )
            if first.record.edition == projection.record.edition:
                raise ExportError(
                    f"duplicate operationId {projection.operation_id!r} is effective "
                    f"twice in edition {projection.record.edition!r}"
                )
            if first.schema_fingerprint != projection.schema_fingerprint:
                raise ExportError(
                    "edition schema disagreement for declared-same operation "
                    f"{projection.operation_id!r}"
                )
        peers.append(projection)

        existing_id = by_wire.get(projection.record.wire)
        if existing_id is not None and existing_id != projection.operation_id:
            raise ExportError(
                "ambiguous selected/effective method-path pair "
                f"{projection.record.wire!r}: {existing_id!r} and "
                f"{projection.operation_id!r}"
            )
        by_wire[projection.record.wire] = projection.operation_id

    paths: dict[str, dict[str, Any]] = {}
    components: dict[str, Any] = {}
    for operation_id, peers in sorted(by_id.items()):
        first = peers[0]
        operation = dict(first.operation)
        operation["x-frisket-editions"] = sorted(
            {peer.record.edition for peer in peers}
        )
        operation["x-frisket-effective-identities"] = [
            {
                "edition": peer.record.edition,
                "method": peer.record.method,
                "owner": peer.record.owner,
                "routeName": peer.record.name,
            }
            for peer in sorted(
                peers,
                key=lambda item: (
                    item.record.edition,
                    item.record.owner,
                    item.record.name,
                    item.record.method,
                ),
            )
        ]
        paths.setdefault(first.record.path, {})[first.record.method.lower()] = operation
        for peer in peers:
            _merge_components(components, peer.components, operation_id)

    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "frisket browser API", "version": "0.1.0"},
        "paths": paths,
    }
    if components:
        document["components"] = components
    return document


def operation_count(document: Mapping[str, Any]) -> int:
    methods = {"delete", "get", "head", "options", "patch", "post", "put"}
    return sum(
        method in methods
        for path_item in document.get("paths", {}).values()
        for method in path_item
    )


def operation_id_changes(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[tuple[str, str, str, str], ...]:
    def ids(document: Mapping[str, Any]) -> dict[WireIdentity, str]:
        return {
            (method.upper(), path): str(operation["operationId"])
            for path, path_item in document.get("paths", {}).items()
            for method, operation in path_item.items()
            if isinstance(operation, Mapping) and "operationId" in operation
        }

    old = ids(before)
    new = ids(after)
    return tuple(
        (method, path, old[(method, path)], new[(method, path)])
        for method, path in sorted(set(old) & set(new))
        if old[(method, path)] != new[(method, path)]
    )


def deterministic_json(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _real_compositions(state_root: Path) -> tuple[Composition, Composition]:
    from frisket.server.app import create_app
    from frisket.team.app import TeamConfig, create_team_app

    state_root.mkdir(parents=True, exist_ok=True)
    local = create_app(
        state_root / "local",
        serve_spa=False,
        enable_provider_config=True,
        product_telemetry_destination=None,
    )

    async def send_magic_email(_email: str, _link: str) -> bool:
        return True

    config = TeamConfig(
        database_url=f"sqlite:///{state_root / 'team-control.db'}",
        run_queue_database_url=f"sqlite:///{state_root / 'team-queue.db'}",
        data_dir=state_root / "team-data",
        base_url="http://testserver",
        organization_name="OpenAPI Export",
        admin_emails={"export@example.invalid"},
        oidc_providers={},
    )
    # The unclaimed team constructor emits a random setup token to stderr.
    # It is inert construction state and must not contaminate deterministic JSON.
    with redirect_stderr(io.StringIO()):
        team = create_team_app(
            config,
            send_magic_email=send_magic_email,
            product_telemetry_destination=None,
        )
    return (
        Composition("local", local, "tenant"),
        Composition("team", team, "outer"),
    )


def export_real_compositions(state_root: Path) -> dict[str, Any]:
    # Team-local routes are absent from other editions and stay out of
    # BASE_ENDPOINT_CATALOG. Read their declaration owner at export time.
    from frisket.team import app as team_app

    team_local_policies = (
        *team_app._TEAM_LOCAL_MODEL_DECLARATIONS,
        *team_app._TEAM_BROWSER_AUTH_DECLARATIONS,
    )
    team_local_canonical_ids = {
        (policy.route_owner, policy.route_name, policy.method.upper()): policy.id
        for policy in team_local_policies
    }
    return export_compositions(
        _real_compositions(state_root),
        policies=(*BASE_ENDPOINT_CATALOG, *team_local_policies),
        canonical_ids={**CANONICAL_OPERATION_IDS, **team_local_canonical_ids},
        require_canonical_ids=True,
    )


def _generate_once(temp_parent: Path | None) -> bytes:
    if temp_parent is not None:
        temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="frisket-openapi-",
        dir=temp_parent,
    ) as raw_state:
        return deterministic_json(export_real_compositions(Path(raw_state)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="write JSON here; without it normal mode writes stdout",
    )
    parser.add_argument(
        "--temp-root",
        type=Path,
        help="parent for all temporary SQLite/control/queue state",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify deterministic generation and optional output parity",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = _generate_once(args.temp_root)
        if args.check:
            repeated = _generate_once(args.temp_root)
            if payload != repeated:
                print("OpenAPI export is nondeterministic", file=sys.stderr)
                return 1
            if args.output is not None:
                try:
                    current = args.output.read_bytes()
                except FileNotFoundError:
                    print(f"OpenAPI output is missing: {args.output}", file=sys.stderr)
                    return 1
                if current != payload:
                    print(f"OpenAPI output is stale: {args.output}", file=sys.stderr)
                    return 1
            return 0

        if args.output is None:
            sys.stdout.buffer.write(payload)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(payload)
        return 0
    except (ExportError, OSError, ValueError) as exc:
        print(f"OpenAPI export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
