"""Sheet feature route registration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import Response

from frisket.contracts.http.column_types import ColumnTypeList
from frisket.contracts.http.graph_lineage import SheetGraphResponse
from frisket.contracts.http.history_review import ColumnRunsPage
from frisket.server.paging import PageLimit100, PageOffset
from frisket.server.route_errors import http_error_responses
from frisket.server.services.column_runs import (
    ColumnRunHistoryService,
)
from frisket.server.services.column_types import ColumnTypeCatalogService
from frisket.server.services.graph import (
    GraphNeighborhoodService,
)
from frisket.server.services.map_points import MapPointsService
from frisket.server.services.sheet_graph import SheetGraphService


def register_column_type_routes(
    app: FastAPI,
    *,
    service: ColumnTypeCatalogService,
) -> None:
    @app.get(
        "/api/column-types",
        response_model=ColumnTypeList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 500),
    )
    def list_column_types() -> ColumnTypeList:
        return ColumnTypeList.model_validate(service.list_column_types())

    @app.get(
        "/api/projects/{pid}/column-types",
        response_model=ColumnTypeList,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 500),
    )
    def list_project_column_types(pid: str) -> ColumnTypeList:
        return ColumnTypeList.model_validate(service.list_column_types(pid))


def register_graph_routes(
    app: FastAPI,
    *,
    service: GraphNeighborhoodService,
) -> None:
    @app.get("/api/projects/{pid}/graph/neighborhood")
    def graph_neighborhood(
        pid: str,
        anchor_id: str = Query(..., min_length=1),
        anchor_kind: str = Query(default="entity"),
        depth: int = Query(default=1, ge=1, le=2),
        review_state: str | None = Query(default=None),
        limit_nodes: int = Query(default=200, ge=1, le=500),
        limit_edges: int = Query(default=500, ge=1, le=1000),
    ) -> dict[str, Any]:
        return service.graph_neighborhood(
            pid,
            anchor_id=anchor_id,
            anchor_kind=anchor_kind,
            depth=depth,
            review_state=review_state,
            limit_nodes=limit_nodes,
            limit_edges=limit_edges,
        )


def register_sheet_graph_routes(
    app: FastAPI,
    *,
    service: SheetGraphService,
) -> None:
    @app.get(
        "/api/projects/{pid}/sheets/{sheet_id}/graph",
        response_model=SheetGraphResponse,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def sheet_graph(
        pid: str,
        sheet_id: int,
        direction: str | None = Query(default=None),
        node_label_column_id: int | None = Query(default=None),
        node_color_column_id: int | None = Query(default=None),
        node_size_column_id: int | None = Query(default=None),
        edge_label_column_id: int | None = Query(default=None),
        limit_nodes: int = Query(default=200, ge=1, le=2000),
        limit_edges: int = Query(default=500, ge=1, le=5000),
    ) -> SheetGraphResponse:
        return SheetGraphResponse.model_validate(
            service.sheet_graph(
                pid,
                sheet_id,
                direction=direction,
                node_label_column_id=node_label_column_id,
                node_color_column_id=node_color_column_id,
                node_size_column_id=node_size_column_id,
                edge_label_column_id=edge_label_column_id,
                limit_nodes=limit_nodes,
                limit_edges=limit_edges,
            )
        )


def register_map_points_routes(
    app: FastAPI,
    *,
    service: MapPointsService,
) -> None:
    @app.get("/api/projects/{pid}/sheets/{sheet_id}/map/points")
    def sheet_map_points(
        pid: str,
        sheet_id: int,
        column_id: int = Query(...),
        bbox: str | None = Query(default=None),
        filter_: str | None = Query(default=None, alias="filter"),
        sort: str | None = None,
        attrs: str | None = Query(default=None),
        format_: str = Query(default="arrow", alias="format"),
    ) -> Response:
        transport = service.sheet_map_points(
            pid,
            sheet_id,
            column_id=column_id,
            bbox=bbox,
            filter_=filter_,
            sort=sort,
            attrs=attrs,
            format_=format_,
        )
        return Response(
            content=transport.content,
            media_type=transport.media_type,
            headers=transport.headers,
        )


def register_column_run_history_routes(
    app: FastAPI,
    *,
    service: ColumnRunHistoryService,
) -> None:
    @app.get(
        "/api/projects/{pid}/columns/{column_id}/runs",
        response_model=ColumnRunsPage,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def column_runs(
        pid: str,
        column_id: int,
        offset: PageOffset = 0,
        limit: PageLimit100 = 20,
    ) -> ColumnRunsPage:
        return ColumnRunsPage.model_validate(
            service.column_runs(
                pid,
                column_id,
                offset=offset,
                limit=limit,
            )
        )
