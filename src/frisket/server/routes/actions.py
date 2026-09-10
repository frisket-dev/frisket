"""Global and project-scoped action catalog route registration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request

from frisket.authoring import actions as action_contract
from frisket.authoring.action_metadata import action_available_in_edition
from frisket.contracts.action import ActionCatalog
from frisket.server.action_catalog_hints import (
    action_catalog_payload_with_launcher_hints,
    project_action_catalog_payload_with_launcher_hints,
    _apply_connected_account_hints,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.workspace import Workspace


def register_action_catalog_routes(
    app: FastAPI,
    *,
    workspace: Workspace,
    sidecar_capabilities: Callable[[], dict[str, Any]],
    edition: str = "solo",
) -> None:
    def for_edition(payload: dict[str, Any]) -> dict[str, Any]:
        if edition == "solo":
            return payload
        return {
            **payload,
            "actions": [
                item
                for item in payload.get("actions", [])
                if action_available_in_edition(item.get("kind"), edition)
            ],
        }

    @app.get("/api/actions/schema")
    def actions_schema() -> dict:
        return action_contract.action_schema()

    @app.get(
        "/api/actions/v1/catalog",
        response_model=ActionCatalog,
        responses=http_error_responses(401, 500),
    )
    def v1_action_catalog() -> ActionCatalog:
        return ActionCatalog.model_validate(
            for_edition(
                action_catalog_payload_with_launcher_hints(sidecar_capabilities())
            )
        )

    @app.get(
        "/api/projects/{pid}/actions/v1/catalog",
        response_model=ActionCatalog,
        responses=http_error_responses(401, 403, 404, 409, 500),
    )
    def project_v1_action_catalog(request: Request, pid: str) -> ActionCatalog:
        project = workspace.get(pid)
        router = workspace.router_for(project)
        execution_composition = workspace.execution_composition_for(
            project,
            router,
            workspace.edition_execution_composition_context_for(request),
        )
        payload = for_edition(
            project_action_catalog_payload_with_launcher_hints(
                project,
                sidecar_capabilities=sidecar_capabilities(),
                execution_composition=execution_composition,
                # Without this, a team-edition org key (workspace.py's
                # org_provider_keys(), the SAME resolver router_for() uses
                # at run time) was invisible to the catalog's availability
                # hints — an LLM remote engine could run but reported
                # "unavailable" here.
                org_provider_keys=workspace.org_provider_keys(),
                has_local_model_endpoint=bool(router.local_endpoints),
                effective_router=router,
                # export.google_sheets gate (action_catalog_hints.py
                # _apply_connected_account_hints): the bare local tier never
                # sets executor_deps_factory (server/app.py create_app), so
                # this is False there and the hint fires; team (once it
                # wires _team_executor_deps_factory, frisket.team.app) and
                # hosted both set it unconditionally, so this is True there
                # regardless of whether an operator has actually configured
                # a Google OAuth app for THIS composition -- that finer gate
                # is deliberately not probed per-request here (would mean
                # invoking the factory speculatively); it still fails
                # closed and loud at run time (`google_sheets_client_
                # unavailable`) if unconfigured, same as today.
                connected_account_resolver_configured=(
                    workspace.executor_deps_factory is not None
                ),
            )
        )
        _apply_connected_account_hints(
            payload["actions"],
            connected_account_resolver_configured=workspace.executor_deps_factory
            is not None,
        )
        return ActionCatalog.model_validate(payload)
