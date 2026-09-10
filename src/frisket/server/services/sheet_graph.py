from __future__ import annotations

from typing import Any

from frisket.features.graph.sheet_graph import (
    GraphViewConfig,
    SheetGraphError,
    build_sheet_graph,
)
from frisket.server.route_errors import RouteError
from frisket.server.workspace import Workspace


class SheetGraphRouteError(RouteError):
    pass


class SheetGraphService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def sheet_graph(
        self,
        project_id: str,
        sheet_id: int,
        *,
        direction: str | None = None,
        node_label_column_id: int | None = None,
        node_color_column_id: int | None = None,
        node_size_column_id: int | None = None,
        edge_label_column_id: int | None = None,
        limit_nodes: int = 200,
        limit_edges: int = 500,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        config = GraphViewConfig(
            direction=direction,
            node_label_column_id=node_label_column_id,
            node_color_column_id=node_color_column_id,
            node_size_column_id=node_size_column_id,
            edge_label_column_id=edge_label_column_id,
            limit_nodes=limit_nodes,
            limit_edges=limit_edges,
        )
        try:
            return build_sheet_graph(project, sheet_id=sheet_id, config=config)
        except SheetGraphError as exc:
            raise SheetGraphRouteError(
                exc.status_code,
                {"code": exc.code, "message": exc.message},
            ) from exc
