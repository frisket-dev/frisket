"""Sheet grid route registration."""

from __future__ import annotations

from fastapi import FastAPI, Query, Request

from frisket.contracts.http.models import (
    ColumnStatsQuery,
    ColumnStats,
    DocumentBrowseQuery,
    DocumentPage,
    SheetData,
    SheetDataQuery,
    SheetRowLocationQuery,
    SheetRowLocation,
)
from frisket.server.route_errors import (
    http_error_responses,
    reject_unknown_query_parameters,
)
from frisket.server.services.sheet_grid import SheetGridService


def register_sheet_grid_routes(
    app: FastAPI,
    *,
    service: SheetGridService,
) -> None:
    @app.get(
        "/api/projects/{pid}/sheets/{sheet_id}/data",
        response_model=SheetData,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
    )
    def sheet_data(
        request: Request,
        pid: str,
        sheet_id: int,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=0, le=1000),
        parent_row_id: int = None,  # type: ignore[assignment]
        filter_: str = Query(default=None, alias="filter"),  # type: ignore[assignment]
        sort: str = None,  # type: ignore[assignment]
        row_ids: str = Query(default=None),  # type: ignore[assignment]
        scope_row_ids: str = Query(default=None),  # type: ignore[assignment]
        column_ids: str = Query(default=None),  # type: ignore[assignment]
    ) -> SheetData:
        reject_unknown_query_parameters(request, SheetDataQuery)
        return SheetData.model_validate(
            service.sheet_data(
                pid,
                sheet_id,
                offset=offset,
                limit=limit,
                parent_row_id=parent_row_id,
                filter_=filter_,
                sort=sort,
                row_ids=row_ids,
                scope_row_ids=scope_row_ids,
                column_ids=column_ids,
            )
        )

    @app.get(
        "/api/projects/{pid}/sheets/{sheet_id}/documents",
        response_model=DocumentPage,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def browse_sheet_documents(
        request: Request,
        pid: str,
        sheet_id: int,
        source_column_id: int = Query(ge=1),
        title_column_id: int | None = Query(default=None, ge=1),
        parent_row_id: int | None = Query(default=None, ge=1),
        filter_: str | None = Query(default=None, alias="filter"),
        sort: str | None = None,
        scope_row_ids: str | None = None,
        q: str | None = Query(default=None, max_length=256),
        cursor: str | None = Query(default=None, max_length=2048),
        anchor_row_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> DocumentPage:
        reject_unknown_query_parameters(request, DocumentBrowseQuery)
        return DocumentPage.model_validate(
            service.document_page(
                pid,
                sheet_id,
                source_column_id=source_column_id,
                title_column_id=title_column_id,
                parent_row_id=parent_row_id,
                filter_=filter_,
                sort=sort,
                scope_row_ids=scope_row_ids,
                q=q,
                cursor=cursor,
                anchor_row_id=anchor_row_id,
                limit=limit,
            )
        )

    @app.get(
        "/api/projects/{pid}/sheets/{sheet_id}/columns/{column_id}/stats",
        response_model=ColumnStats,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def column_stats(
        request: Request,
        pid: str,
        sheet_id: int,
        column_id: int,
        force: bool = False,
        parent_row_id: int | None = None,
        filter_: str | None = Query(default=None, alias="filter"),
        sort: str | None = None,
        scope_row_ids: str | None = None,
    ) -> ColumnStats:
        reject_unknown_query_parameters(request, ColumnStatsQuery)
        return ColumnStats.model_validate(
            service.column_stats(
                pid,
                sheet_id,
                column_id,
                force=force,
                parent_row_id=parent_row_id,
                filter_=filter_,
                sort=sort,
                scope_row_ids=scope_row_ids,
            )
        )

    @app.get(
        "/api/projects/{pid}/sheets/{sheet_id}/rows/{row_id}/locate",
        response_model=SheetRowLocation,
        response_model_exclude_unset=True,
        responses=http_error_responses(400, 401, 403, 404, 422, 500),
    )
    def locate_sheet_row(
        request: Request,
        pid: str,
        sheet_id: int,
        row_id: int,
        page_size: int = Query(default=500, ge=1, le=1000),
        parent_row_id: int | None = None,
        filter_: str | None = Query(default=None, alias="filter"),
        sort: str | None = None,
        row_ids: str | None = None,
        scope_row_ids: str | None = None,
    ) -> SheetRowLocation:
        reject_unknown_query_parameters(request, SheetRowLocationQuery)
        return SheetRowLocation.model_validate(
            service.locate_sheet_row(
                pid,
                sheet_id,
                row_id,
                page_size=page_size,
                parent_row_id=parent_row_id,
                filter_=filter_,
                sort=sort,
                row_ids=row_ids,
                scope_row_ids=scope_row_ids,
            )
        )
