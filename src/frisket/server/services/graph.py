"""Graph route services for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.features.graph.neighborhood import (
    GraphNeighborhoodError,
    build_graph_neighborhood,
)
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


class GraphNeighborhoodRouteError(RouteError):
    pass


class GraphNeighborhoodService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def graph_neighborhood(
        self,
        project_id: str,
        *,
        anchor_id: str,
        anchor_kind: str = "entity",
        depth: int = 1,
        review_state: str | None = None,
        limit_nodes: int = 200,
        limit_edges: int = 500,
    ) -> dict[str, Any]:
        if anchor_kind != "entity":
            raise GraphNeighborhoodRouteError(
                400,
                {
                    "code": "unsupported_anchor_kind",
                    "message": (
                        "graph neighborhood v1 supports only anchor_kind=entity"
                    ),
                },
            )
        project = self._workspace.get(project_id)
        try:
            return build_graph_neighborhood(
                project,
                anchor_id=anchor_id,
                depth=depth,
                review_states=_graph_review_states(review_state),
                limit_nodes=limit_nodes,
                limit_edges=limit_edges,
            )
        except GraphNeighborhoodError as exc:
            raise GraphNeighborhoodRouteError(
                exc.status_code,
                {"code": exc.code, "message": exc.message},
            ) from exc


def _graph_review_states(review_state: str | None) -> set[str] | None:
    if review_state is None:
        return None
    states = {token.strip().lower() for token in review_state.split(",")}
    filtered = {state for state in states if state}
    return filtered or None
