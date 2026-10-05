"""Project research route registration."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Path, Query

from frisket.contracts.http.history_review import (
    ReviewBatchRequest,
    ReviewBundlesPage,
    ReviewCount,
    ReviewRunsPage,
    ReviewRunStatus,
    ReviewRunStatusRequest,
)
from frisket.contracts.http.project_search import ProjectSearchPage
from frisket.contracts.http.run_provenance import ProvenanceManifest
from frisket.server.paging import PageLimit100, PageOffset
from frisket.server.route_errors import http_error_responses
from frisket.server.services.project_backfill_activity import (
    ProjectBackfillActivityService,
)
from frisket.server.services.project_entity_review import (
    ProjectEntityReviewService,
)
from frisket.server.services.project_provenance import ProjectProvenanceService
from frisket.server.services.project_search import ProjectSearchService


def register_project_search_routes(
    app: FastAPI,
    *,
    service: ProjectSearchService,
) -> None:
    @app.get(
        "/api/projects/{pid}/search",
        response_model=ProjectSearchPage,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def search_ep(
        pid: str,
        q: str,
        limit: PageLimit100 = 50,
        mode: str = "keyword",
        rerank: str = "auto",
    ) -> ProjectSearchPage:
        return service.search(
            pid,
            q=q,
            limit=limit,
            mode=mode,
            rerank=rerank,
        )


def register_project_provenance_routes(
    app: FastAPI,
    *,
    service: ProjectProvenanceService,
) -> None:
    @app.get(
        "/api/projects/{pid}/provenance",
        response_model=ProvenanceManifest,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def provenance(
        pid: str,
        runs_offset: PageOffset = 0,
        runs_limit: PageLimit100 = 25,
        receipts_offset: PageOffset = 0,
        receipts_limit: PageLimit100 = 25,
    ) -> ProvenanceManifest:
        """Data-flow provenance manifest: which providers/models/actions
        touched this project, with bounded recent run and receipt windows."""
        return ProvenanceManifest.model_validate(
            service.manifest(
                pid,
                runs_offset=runs_offset,
                runs_limit=runs_limit,
                receipts_offset=receipts_offset,
                receipts_limit=receipts_limit,
            )
        )


def register_project_backfill_activity_routes(
    app: FastAPI,
    *,
    service: ProjectBackfillActivityService,
) -> None:
    @app.get("/api/projects/{pid}/activity/backfills")
    def backfill_activity(
        pid: str,
        column_id: int | None = None,
        offset: PageOffset = 0,
        limit: PageLimit100 = 25,
    ) -> dict[str, Any]:
        """Receipt-backed backfill activity feed (paged, column-filterable).

        This remains the v1 receipt-observability surface for backfills:
        tests/test_backfill_receipt_activity.py pins that activity pages
        are built from durable receipts (including replay dedupe) and that
        the route lives in this module; the endpoint catalog grants
        session_or_pat/viewer, i.e. PAT-bearing programmatic readers.
        Removal condition: remove only when an
        receipt-backed observability surface (e.g. a receipts/history route)
        covers backfill activity and test_backfill_receipt_activity.py
        plus the ``backfill_activity`` endpoint-catalog entry are retired in
        the same change.
        """
        return service.page(
            pid,
            column_id=column_id,
            offset=offset,
            limit=limit,
        )


def register_project_entity_review_routes(
    app: FastAPI,
    *,
    service: ProjectEntityReviewService,
) -> None:
    @app.get("/api/projects/{pid}/entities")
    def entities_ep(pid: str) -> list[dict[str, Any]]:
        return service.entities(pid)

    @app.get("/api/projects/{pid}/review/queue")
    def review_queue_ep(
        pid: str,
        sheet_id: int | None = None,
        run_id: int | None = Query(default=None, ge=1),
    ) -> list[dict[str, Any]]:
        return service.review_queue(pid, sheet_id=sheet_id, run_id=run_id)

    @app.get(
        "/api/projects/{pid}/review/bundles",
        response_model=ReviewBundlesPage,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def review_bundles_ep(
        pid: str,
        sheet_id: int | None = None,
        offset: PageOffset = 0,
        limit: PageLimit100 = 25,
        run_id: int | None = Query(default=None, ge=1),
        include_reviewed: bool = False,
        field_id: int | None = Query(default=None, ge=1),
        order: Literal["confidence", "row"] = "confidence",
        cursor: int | None = Query(default=None, ge=0),
    ) -> ReviewBundlesPage:
        if cursor is not None and order != "row":
            raise HTTPException(status_code=422, detail="cursor requires order=row")
        if order == "row" and run_id is None:
            raise HTTPException(status_code=422, detail="order=row requires run_id")
        if order == "row" and offset != 0:
            raise HTTPException(status_code=422, detail="order=row requires offset=0")
        return ReviewBundlesPage.model_validate(
            service.review_bundles(
                pid,
                sheet_id=sheet_id,
                offset=offset,
                limit=limit,
                run_id=run_id,
                include_reviewed=include_reviewed,
                field_id=field_id,
                order=order,
                cursor=cursor,
            )
        )

    @app.post(
        "/api/projects/{pid}/review/bundles",
        response_model=ReviewBundlesPage,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def review_batch_ep(pid: str, request: ReviewBatchRequest) -> ReviewBundlesPage:
        return ReviewBundlesPage.model_validate(service.review_batch(pid, request))

    @app.get(
        "/api/projects/{pid}/review/runs",
        response_model=ReviewRunsPage,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def review_runs_ep(
        pid: str,
        sheet_id: int | None = Query(default=None, ge=1),
        run_id: int | None = Query(default=None, ge=1),
        offset: PageOffset = 0,
        limit: int = Query(default=25, ge=1, le=50),
    ) -> ReviewRunsPage:
        return ReviewRunsPage.model_validate(
            service.review_runs(
                pid,
                sheet_id=sheet_id,
                run_id=run_id,
                offset=offset,
                limit=limit,
            )
        )

    @app.post(
        "/api/projects/{pid}/review/runs/{run_id}/status",
        response_model=ReviewRunStatus,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def review_run_status_ep(
        pid: str,
        run_id: Annotated[int, Path(ge=1)],
        request: ReviewRunStatusRequest,
    ) -> ReviewRunStatus:
        return ReviewRunStatus.model_validate(
            service.set_review_run_status(pid, run_id=run_id, status=request.status)
        )

    @app.get(
        "/api/projects/{pid}/review/count",
        response_model=ReviewCount,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def review_count_ep(
        pid: str, run_id: int | None = Query(default=None, ge=1)
    ) -> ReviewCount:
        return ReviewCount.model_validate(service.review_count(pid, run_id=run_id))
