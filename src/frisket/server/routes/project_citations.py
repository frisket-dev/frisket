"""Project citation route registration."""

from __future__ import annotations

from fastapi import FastAPI, Query

from frisket.contracts.http.project_citations import (
    PROJECT_CITATIONS_ERROR_RESPONSES,
    CellCitationsResponse,
    CellTextAnnotationsResponse,
    CitationViewerResponse,
    ColumnCitationsResponse,
    RowSourceDocumentsResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.project_citations import ProjectCitationsService


def register_project_citation_routes(
    app: FastAPI,
    *,
    service: ProjectCitationsService,
) -> None:
    @app.get(
        "/api/projects/{pid}/cells/{row_id}/{column_id}/citations",
        response_model=CellCitationsResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_CITATIONS_ERROR_RESPONSES,
            **http_error_responses(401, 403, 409, 422, 500),
        },
    )
    def cell_citations(
        pid: str,
        row_id: int,
        column_id: int,
        include_stale: bool = Query(default=False),
    ) -> CellCitationsResponse:
        return CellCitationsResponse.model_validate(
            service.cell_citations(
                pid,
                row_id=row_id,
                column_id=column_id,
                include_stale=include_stale,
            )
        )

    @app.get(
        "/api/projects/{pid}/rows/{row_id}/source-documents",
        response_model=RowSourceDocumentsResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_CITATIONS_ERROR_RESPONSES,
            **http_error_responses(401, 403, 409, 422, 500),
        },
    )
    def row_source_documents(pid: str, row_id: int) -> RowSourceDocumentsResponse:
        return RowSourceDocumentsResponse.model_validate(
            service.row_source_documents(pid, row_id=row_id)
        )

    # This is the inverse of cell_citations. That endpoint answers "what
    # supports this output cell"; this one answers "what
    # annotation layers point AT the text I am rendering", resolved through the
    # coordinate surface. Same (row_id, column_id) addressing, so the sheet is
    # derived rather than sent — one locator, one meaning.
    @app.get(
        "/api/projects/{pid}/cells/{row_id}/{column_id}/annotations",
        response_model=CellTextAnnotationsResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_CITATIONS_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def cell_text_annotations(
        pid: str,
        row_id: int,
        column_id: int,
        start: int | None = Query(default=None, ge=0),
        limit: int | None = Query(default=None, ge=1, le=16_384),
        revision: int | None = Query(default=None, gt=0),
        occurrence_id: str | None = Query(default=None, max_length=100),
    ) -> CellTextAnnotationsResponse:
        return CellTextAnnotationsResponse.model_validate(
            service.cell_text_annotations(
                pid,
                row_id=row_id,
                column_id=column_id,
                start=start,
                limit=limit,
                revision=revision,
                occurrence_id=occurrence_id,
            )
        )

    @app.get(
        "/api/projects/{pid}/citations/{citation_id}/viewer",
        response_model=CitationViewerResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_CITATIONS_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def citation_viewer(
        pid: str,
        citation_id: str,
        locate_current: bool = Query(default=False),
        text_artifact_id: int | None = Query(default=None, gt=0),
        text_start: int | None = Query(default=None, ge=0),
        text_limit: int | None = Query(default=None, ge=1, le=16_384),
        text_revision: int | None = Query(default=None, gt=0),
        text_span_id: str | None = Query(default=None, max_length=160),
    ) -> CitationViewerResponse:
        return CitationViewerResponse.model_validate(
            service.citation_viewer(
                pid,
                citation_id,
                locate_current=locate_current,
                text_artifact_id=text_artifact_id,
                text_start=text_start,
                text_limit=text_limit,
                text_revision=text_revision,
                text_span_id=text_span_id,
            )
        )

    # The Grounded Answers reading view's middle-pane batch, grouped per
    # column instead of per cell (see the store-layer helper for the
    # scoping/projection it reuses). Optional `row_ids` (comma-separated)
    # scopes the batch to a virtualization window.
    @app.get(
        "/api/projects/{pid}/sheets/{sheet_id}/columns/{column_id}/citations",
        response_model=ColumnCitationsResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_CITATIONS_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def column_citations(
        pid: str,
        sheet_id: int,
        column_id: int,
        row_ids: str | None = Query(default=None),
    ) -> ColumnCitationsResponse:
        return ColumnCitationsResponse.model_validate(
            service.column_citations(
                pid,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
            )
        )

    @app.get("/api/projects/{pid}/citations/spans/{span_stable_id}/clip")
    async def citation_span_clip(
        pid: str,
        span_stable_id: str,
        pad_ms: int = Query(default=500, ge=0, le=5_000),
    ):
        return await service.citation_span_clip(pid, span_stable_id, pad_ms=pad_ms)

    # Citation-level (not span-level) -- ONE clip for a whole contiguous run of
    # a citation's cited temporal spans, addressed by the citation + its run
    # index (citations.citation_temporal_runs), rather than a
    # client-supplied raw ms range: the server stays the single source of
    # truth for what a "run" is, so a run the viewer highlighted always
    # clips to exactly that range.
    @app.get("/api/projects/{pid}/citations/{citation_id}/runs/{run_index}/clip")
    async def citation_run_clip(
        pid: str,
        citation_id: str,
        run_index: int,
        pad_ms: int = Query(default=500, ge=0, le=5_000),
    ):
        return await service.citation_run_clip(
            pid, citation_id, run_index, pad_ms=pad_ms
        )
