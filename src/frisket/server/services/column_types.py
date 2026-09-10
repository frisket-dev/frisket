"""Column type catalog service for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.authoring import column_types as column_type_registry
from frisket.server.workspace import Workspace
from frisket.authoring.workbench.plugin_runtime_capabilities import (
    project_allows_plugin_column_type,
)


class ColumnTypeCatalogService:
    def __init__(self, workspace: Workspace | None = None) -> None:
        self._workspace = workspace

    def list_column_types(self, project_id: str | None = None) -> list[dict[str, Any]]:
        import frisket.ops  # noqa: F401 - imports built-in recipe/type plugins

        specs = column_type_registry.column_types()
        if project_id is not None:
            if self._workspace is None:
                return [
                    t.to_public() for t in specs if t.core or t.plugin == "external"
                ]
            project = self._workspace.get(project_id)
            specs = [
                type_spec
                for type_spec in specs
                if project_allows_plugin_column_type(project, type_spec)
            ]
        else:
            specs = [t for t in specs if t.core or t.plugin == "external"]
        return [t.to_public() for t in specs]
