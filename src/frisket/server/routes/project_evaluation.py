"""Expected-answer editor and comparison scoring routes; execution stays in preview APIs."""

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from frisket.contracts.http.evaluation import (
    ComparisonCaptureRequest,
    ComparisonSettings,
    EvaluationCell,
    EvaluationCellRequest,
    EvaluationJudgmentRequest,
    EvaluationLinksQuery,
    EvaluationRequest,
    EvaluationResponse,
    EvaluationReviewQuery,
    EvaluationShelfQuery,
    ExpectedAnswerRequest,
    ExpectedJoinPreview,
    ExpectedLink,
    ExpectedLinkInput,
    ExpectedLinkPreviewInput,
    ExpectedLinks,
    ExpectedProjection,
    ExpectedProjectionRequest,
    ExpectedRuleChange,
    ExpectedRuleSetting,
    SavedComparison,
    SavedComparisons,
    SavedComparisonSummary,
    ShelfRename,
    ShelfSaveRequest,
)
from frisket.server.route_errors import (
    http_error_responses,
    reject_unknown_query_parameters,
)
from frisket.server.services.evaluation import EvaluationService


def register_project_evaluation_routes(app: FastAPI, *, service: EvaluationService):
    errors = http_error_responses(400, 401, 403, 404, 409, 422, 500)
    root = "/api/projects/{pid}/evaluation"

    @app.get(root + "/links", response_model=ExpectedLinks, responses=errors)
    def evaluation_links(request: Request, pid: str, sheet_id: int = Query(gt=0)):
        reject_unknown_query_parameters(request, EvaluationLinksQuery)
        return service.links(pid, sheet_id)

    @app.post(
        root + "/links/preview", response_model=ExpectedJoinPreview, responses=errors
    )
    def evaluation_link_preview(pid: str, body: ExpectedLinkPreviewInput):
        return service.preview_link(pid, body.model_dump(mode="json"))

    @app.post(root + "/links", response_model=ExpectedLink, responses=errors)
    def evaluation_link_create(pid: str, body: ExpectedLinkInput):
        return service.save_link(pid, body)

    @app.put(root + "/links/{link_id}", response_model=ExpectedLink, responses=errors)
    def evaluation_link_update(pid: str, link_id: int, body: ExpectedLinkInput):
        return service.save_link(pid, body, link_id)

    @app.delete(root + "/links/{link_id}", status_code=204, responses=errors)
    def evaluation_link_delete(pid: str, link_id: int):
        service.delete_link(pid, link_id)
        return Response(status_code=204)

    @app.post(root + "/projection", response_model=ExpectedProjection, responses=errors)
    def evaluation_projection(pid: str, body: ExpectedProjectionRequest):
        return service.projection(pid, body.sheet_id, body.row_ids)

    @app.put(
        root + "/rules/{expected_column_id}",
        response_model=ExpectedRuleSetting,
        responses=errors,
    )
    def evaluation_rule_update(
        pid: str, expected_column_id: int, body: ExpectedRuleChange
    ):
        return service.set_rule(pid, expected_column_id, body.rule)

    @app.post(root + "/score", response_model=EvaluationResponse, responses=errors)
    def evaluation_score(pid: str, body: EvaluationRequest):
        return service.score(pid, body)

    @app.put(root + "/judgment", response_model=EvaluationResponse, responses=errors)
    def evaluation_judgment(
        pid: str, body: EvaluationJudgmentRequest, request: Request
    ):
        user = getattr(request.state, "user", None)
        actor = (
            f"user:{user['id']}"
            if isinstance(user, dict) and user.get("id") is not None
            else "local:project"
        )
        return service.judgment(pid, body, actor=actor)

    @app.post(root + "/expected-answer", response_model=ExpectedLink, responses=errors)
    def evaluation_expected_answer(pid: str, body: ExpectedAnswerRequest):
        return service.expected_answer(pid, body)

    @app.post(root + "/capture", response_model=SavedComparisons, responses=errors)
    def evaluation_capture(pid: str, body: ComparisonCaptureRequest):
        return service.capture(pid, body)

    @app.delete(root + "/capture/{saved_id}", status_code=204, responses=errors)
    def evaluation_capture_discard(pid: str, saved_id: int):
        service.discard_capture(pid, saved_id)
        return Response(status_code=204)

    @app.post(root + "/cell", response_model=EvaluationCell, responses=errors)
    def evaluation_cell(pid: str, body: EvaluationCellRequest):
        return service.cell(pid, body)

    @app.get(root + "/shelf", response_model=SavedComparisons, responses=errors)
    def evaluation_shelf_list(
        request: Request,
        pid: str,
        sheet_id: int = Query(gt=0),
        action_id: str = Query(min_length=1),
    ):
        reject_unknown_query_parameters(request, EvaluationShelfQuery)
        return service.shelf(pid, sheet_id, action_id)

    @app.post(root + "/shelf", response_model=SavedComparisons, responses=errors)
    def evaluation_shelf_save(pid: str, body: ShelfSaveRequest):
        return service.shelve(pid, body)

    @app.get(
        root + "/shelf/{saved_id}", response_model=SavedComparison, responses=errors
    )
    def evaluation_shelf_get(pid: str, saved_id: int):
        return service.saved(pid, saved_id)

    @app.patch(
        root + "/shelf/{saved_id}",
        response_model=SavedComparisonSummary,
        responses=errors,
    )
    def evaluation_shelf_rename(pid: str, saved_id: int, body: ShelfRename):
        return service.rename_saved(pid, saved_id, body.label)

    @app.delete(root + "/shelf/{saved_id}", status_code=204, responses=errors)
    def evaluation_shelf_delete(pid: str, saved_id: int):
        service.delete_saved(pid, saved_id)
        return Response(status_code=204)

    @app.post(
        root + "/download.csv",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "text/csv": {"schema": {"type": "string", "format": "binary"}}
                }
            },
            **errors,
        },
    )
    def evaluation_download_csv(pid: str, body: EvaluationRequest):
        return Response(
            service.download_csv(pid, body),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="comparison.csv"'},
        )

    @app.post(
        root + "/settings.json", response_model=ComparisonSettings, responses=errors
    )
    def evaluation_download_settings(pid: str, body: EvaluationRequest):
        return JSONResponse(
            service.download_settings(pid, body),
            headers={
                "Content-Disposition": 'attachment; filename="comparison-settings.json"'
            },
        )

    @app.get(
        root + "/review.csv",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "text/csv": {"schema": {"type": "string", "format": "binary"}}
                }
            },
            **errors,
        },
    )
    def evaluation_review_csv(request: Request, pid: str, run_id: int = Query(gt=0)):
        reject_unknown_query_parameters(request, EvaluationReviewQuery)
        return Response(
            service.review_csv(pid, run_id),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="review.csv"'},
        )
