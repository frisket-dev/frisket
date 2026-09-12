"""Passive setup scope and current download-operation projection."""

from __future__ import annotations

import os
from typing import Any

from frisket.engine.jobs import model_pull_store
from frisket.execution.definitions import (
    MODELS_GATEWAY_TOKEN_ENV,
    MODELS_GATEWAY_URL_ENV,
)
from frisket.server.provider_config import ENV_VAR, provider_key_status
from frisket.server.services.selector_choices_capabilities import (
    SelectorCapabilities,
    SelectorModelsGatewayStatusFor,
)
from frisket.server.services.selector_choices_projection import (
    _credential_source,
    _optional_str,
)
from frisket.server.workspace import Workspace


class SelectorSetupService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        edition: str,
        models_gateway_status_for: SelectorModelsGatewayStatusFor | None = None,
    ) -> None:
        self._workspace = workspace
        self._edition = edition
        self._models_gateway_status_for = models_gateway_status_for

    def api_key(
        self,
        *,
        project: Any,
        provider: str,
        router: Any,
        capabilities: SelectorCapabilities,
    ) -> dict[str, Any]:
        status_rows = {
            row["id"]: row for row in provider_key_status(self._workspace.root)
        }
        workspace_status = status_rows.get(provider, {})
        project_row = project.provider_key_catalog_rows().get(provider)
        source = _credential_source(router, provider)
        org_configured = source == "organization"
        platform_configured = source == "platform"
        env_name = ENV_VAR[provider]
        return {
            "kind": "api_key",
            "provider": provider,
            "scopes": [
                {
                    "scope": "environment",
                    "configured": workspace_status.get("source") == "env",
                    "can_mutate": False,
                    "source": "environment"
                    if workspace_status.get("source") == "env"
                    else "missing",
                    "environment_names": [env_name],
                    "settings_location": None,
                    "hint": workspace_status.get("hint")
                    if workspace_status.get("source") == "env"
                    else None,
                },
                {
                    "scope": "workspace",
                    "configured": workspace_status.get("source") == "local_file",
                    "can_mutate": capabilities.configure_workspace_credentials,
                    "source": "workspace"
                    if workspace_status.get("source") == "local_file"
                    else "missing",
                    "environment_names": [env_name],
                    "settings_location": "workspace_ai_providers",
                    "hint": workspace_status.get("hint")
                    if workspace_status.get("source") == "local_file"
                    else None,
                },
                {
                    "scope": "project",
                    "configured": project_row is not None,
                    "can_mutate": capabilities.configure_project_credentials,
                    "source": "project" if project_row is not None else "missing",
                    "environment_names": [env_name],
                    "settings_location": "project_ai_providers",
                    "hint": project_row.get("hint") if project_row else None,
                },
                {
                    "scope": "organization",
                    "configured": org_configured or platform_configured,
                    "can_mutate": capabilities.configure_organization_credentials
                    and not platform_configured,
                    "source": "platform"
                    if platform_configured
                    else "organization"
                    if org_configured
                    else "missing",
                    "environment_names": [env_name],
                    "settings_location": "organization_ai_providers",
                    "hint": None,
                },
            ],
        }

    def models_gateway(self, capabilities: SelectorCapabilities) -> dict[str, Any]:
        env_configured = bool(os.environ.get(MODELS_GATEWAY_URL_ENV)) and bool(
            os.environ.get(MODELS_GATEWAY_TOKEN_ENV)
        )
        passive = (
            self._models_gateway_status_for(capabilities.configure_models_gateway)
            if self._models_gateway_status_for is not None
            else {}
        )
        scope = passive.get("authority") or (
            "organization" if self._edition != "solo" else "workspace"
        )
        source = passive.get("source")
        stored_configured = bool(passive.get("configured")) and source != "environment"
        return {
            "kind": "models_gateway",
            "scopes": [
                {
                    "scope": "environment",
                    "configured": env_configured,
                    "can_mutate": False,
                    "source": "environment" if env_configured else "missing",
                    "environment_names": [
                        MODELS_GATEWAY_URL_ENV,
                        MODELS_GATEWAY_TOKEN_ENV,
                    ],
                    "settings_location": None,
                    "hint": None,
                },
                {
                    "scope": scope,
                    "configured": stored_configured,
                    "can_mutate": capabilities.configure_models_gateway
                    and bool(passive.get("can_mutate", True)),
                    "source": scope if stored_configured else "missing",
                    "environment_names": [
                        MODELS_GATEWAY_URL_ENV,
                        MODELS_GATEWAY_TOKEN_ENV,
                    ],
                    "settings_location": f"{scope}_models_gateway",
                    "hint": _optional_str(passive.get("token_hint")),
                },
            ],
        }

    def model_pull_operations(
        self, setup_ref: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        rows = model_pull_store.list_recent(
            self._workspace.queue.engine,
            str(self._workspace.root),
            limit=20,
        )
        active = [row for row in rows if row.is_active]
        matching = next((row for row in active if row.model_ref == setup_ref), None)
        blocked = next((row for row in active if row.model_ref != setup_ref), None)
        return (
            model_pull_store.to_dto(matching) if matching is not None else None,
            model_pull_store.to_dto(blocked) if blocked is not None else None,
        )
