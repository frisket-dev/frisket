"""Project-bound normal action services for prepared Project Ask dispatch."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from frisket.server.action_catalog_hints import (
    project_action_catalog_payload_with_launcher_hints,
)
from frisket.server.services.action_previews import ActionPreviewService
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


ContextProvider = Callable[[], Any]
SidecarCapabilitiesProvider = Callable[[], dict[str, Any]]


class ProjectAskActionHost:
    """Bind normal catalog, quote, and execution services to one project."""

    def __init__(
        self,
        workspace: Workspace,
        project_id: str,
        context_provider: ContextProvider,
        sidecar_capabilities_provider: SidecarCapabilitiesProvider,
    ) -> None:
        self._workspace = workspace
        self._project_id = project_id
        self._context_provider = context_provider
        self._sidecar_capabilities_provider = sidecar_capabilities_provider
        self._preview = ActionPreviewService(workspace)
        self._runs = ActionRunService(workspace)

    def catalog(self) -> dict[str, Any]:
        """Build the live project catalog with the normal execution facts."""

        project = self._workspace.get(self._project_id)
        context = self._context_provider()
        router = self._workspace.router_for(project)
        composition = self._workspace.execution_composition_for(
            project,
            router,
            self._workspace.edition_execution_composition_context_for(context),
        )
        return project_action_catalog_payload_with_launcher_hints(
            project,
            sidecar_capabilities=self._sidecar_capabilities_provider(),
            execution_composition=composition,
            org_provider_keys=self._workspace.org_provider_keys(),
            has_local_model_endpoint=bool(router.local_endpoints),
            effective_router=router,
            connected_account_resolver_configured=(
                self._workspace.executor_deps_factory is not None
            ),
        )

    def quote(self, action: dict[str, Any]) -> dict[str, Any]:
        """Quote with the normal preview service under current trusted context."""

        return self._preview.estimate(
            self._project_id,
            dict(action),
            request_context=self._context_provider(),
        )

    def run(self, project_id: str, body: dict[str, Any]) -> Any:
        """Run through the normal action service for this host's project only."""

        if project_id != self._project_id:
            raise ValueError("Project Ask action host is bound to a different project")
        return self._runs.run_action(
            project_id,
            dict(body),
            request_context=self._context_provider(),
        )
