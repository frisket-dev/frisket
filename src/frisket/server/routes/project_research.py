"""Project research route registration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from frisket.contracts.http.copilot import CopilotReply, CopilotRequest
from frisket.contracts.http.history_review import ReviewBundlesPage, ReviewCount
from frisket.contracts.http.models import EmptyQuery
from frisket.contracts.http.project_search import ProjectSearchHits
from frisket.contracts.http.run_provenance import ProvenanceManifest
from frisket.engine.runner import ProviderKeyRefusal
from frisket.server.paging import PageLimit100, PageOffset
from frisket.server.route_errors import (
    http_error_responses,
    reject_unknown_query_parameters,
)
from frisket.server.services.project_backfill_activity import (
    ProjectBackfillActivityService,
)
from frisket.server.services.project_copilot import (
    ProjectCopilotService,
    ProjectCopilotUpstreamError,
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
        response_model=ProjectSearchHits,
        response_model_exclude_unset=True,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def search_ep(
        pid: str,
        q: str,
        limit: int = 50,
        mode: str = "keyword",
        rerank: str = "auto",
    ) -> ProjectSearchHits:
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


def register_project_copilot_routes(
    app: FastAPI,
    *,
    service: ProjectCopilotService,
) -> None:
    @app.post(
        "/api/projects/{pid}/copilot",
        response_model=CopilotReply,
        responses=http_error_responses(401, 403, 404, 409, 422, 500, 502),
    )
    async def copilot_ep(
        request: Request, pid: str, body: CopilotRequest
    ) -> CopilotReply:
        reject_unknown_query_parameters(request, EmptyQuery)
        try:
            reply = await service.chat(pid, body.model_dump(mode="json"))
        except ProviderKeyRefusal as exc:
            # See the refusal-seat table at action_runs._v1_action_result_http_status.
            # Copilot chat is an explicit paid action, not the action-run 402
            # consent protocol.  A project-key cap is therefore a typed state
            # conflict: refuse before egress and name the exact settings knob
            # instead of leaking the typed refusal through FastAPI as a 500.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.error_code,
                    "message": exc.action_message(),
                    "details": dict(exc.details),
                },
            ) from exc
        except ProjectCopilotUpstreamError as exc:
            raise HTTPException(502, exc.detail) from exc
        return CopilotReply.model_validate(reply)


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
    ) -> ReviewBundlesPage:
        return ReviewBundlesPage.model_validate(
            service.review_bundles(
                pid,
                sheet_id=sheet_id,
                offset=offset,
                limit=limit,
                run_id=run_id,
                include_reviewed=include_reviewed,
            )
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
