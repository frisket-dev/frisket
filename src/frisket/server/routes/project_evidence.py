"""Project evidence route registration."""

from __future__ import annotations

from fastapi import FastAPI, Query

from frisket.contracts.http.project_evidence import (
    PROJECT_EVIDENCE_ERROR_RESPONSES,
    CellEvidenceResponse,
    CellTextAnnotationsResponse,
    ColumnEvidenceResponse,
    EvidenceViewerResponse,
)
from frisket.server.route_errors import http_error_responses

from frisket.server.services.project_evidence import (
    ProjectEvidenceService,
)


def register_project_evidence_routes(
    app: FastAPI,
    *,
    service: ProjectEvidenceService,
) -> None:
    @app.get(
        "/api/projects/{pid}/cells/{row_id}/{column_id}/evidence",
        response_model=CellEvidenceResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_EVIDENCE_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def cell_evidence(
        pid: str,
        row_id: int,
        column_id: int,
        include_stale: bool = Query(default=False),
    ) -> CellEvidenceResponse:
        return CellEvidenceResponse.model_validate(
            service.cell_evidence(
                pid,
                row_id=row_id,
                column_id=column_id,
                include_stale=include_stale,
            )
        )

    # This is the inverse of cell_evidence. That endpoint answers "what
    # supports this output cell"; this one answers "what
    # annotation layers point AT the text I am rendering", resolved through the
    # coordinate surface. Same (row_id, column_id) addressing, so the sheet is
    # derived rather than sent — one locator, one meaning.
    @app.get(
        "/api/projects/{pid}/cells/{row_id}/{column_id}/annotations",
        response_model=CellTextAnnotationsResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_EVIDENCE_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def cell_text_annotations(
        pid: str, row_id: int, column_id: int
    ) -> CellTextAnnotationsResponse:
        return CellTextAnnotationsResponse.model_validate(
            service.cell_text_annotations(pid, row_id=row_id, column_id=column_id)
        )

    @app.get(
        "/api/projects/{pid}/evidence/links/{evidence_link_id}/viewer",
        response_model=EvidenceViewerResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_EVIDENCE_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def evidence_viewer(pid: str, evidence_link_id: str) -> EvidenceViewerResponse:
        return EvidenceViewerResponse.model_validate(
            service.evidence_viewer(pid, evidence_link_id)
        )

    # The Grounded Answers reading view's middle-pane batch, grouped per
    # column instead of per cell (see the store-layer helper for the
    # scoping/projection it reuses). Optional `row_ids` (comma-separated)
    # scopes the batch to a virtualization window.
    @app.get(
        "/api/projects/{pid}/sheets/{sheet_id}/columns/{column_id}/evidence",
        response_model=ColumnEvidenceResponse,
        response_model_exclude_unset=True,
        responses={
            **PROJECT_EVIDENCE_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def column_evidence(
        pid: str,
        sheet_id: int,
        column_id: int,
        row_ids: str | None = Query(default=None),
    ) -> ColumnEvidenceResponse:
        return ColumnEvidenceResponse.model_validate(
            service.column_evidence(
                pid,
                sheet_id=sheet_id,
                column_id=column_id,
                row_ids=row_ids,
            )
        )

    @app.get("/api/projects/{pid}/evidence/spans/{span_stable_id}/clip")
    async def evidence_span_clip(
        pid: str,
        span_stable_id: str,
        pad_ms: int = Query(default=500, ge=0, le=5_000),
    ):
        return await service.evidence_span_clip(pid, span_stable_id, pad_ms=pad_ms)

    # Link-level (not span-level) -- ONE clip for a whole contiguous run of
    # a citation's cited temporal spans, addressed by the link + its run
    # index (evidence.citation_temporal_runs), rather than a
    # client-supplied raw ms range: the server stays the single source of
    # truth for what a "run" is, so a run the viewer highlighted always
    # clips to exactly that range.
    @app.get(
        "/api/projects/{pid}/evidence/links/{evidence_link_id}/runs/{run_index}/clip"
    )
    async def evidence_link_run_clip(
        pid: str,
        evidence_link_id: str,
        run_index: int,
        pad_ms: int = Query(default=500, ge=0, le=5_000),
    ):
        return await service.evidence_link_run_clip(
            pid, evidence_link_id, run_index, pad_ms=pad_ms
        )
