"""Action estimate, validation, and preview-job route registrations.

All preview-job handlers are synchronous ``def`` (not ``async def``): the
preview compute reaches ``asyncio.run(...)`` on its job thread, and an
``async def`` handler runs on the event loop where a fresh ``asyncio.run``
would raise. Matching ``/run``'s sync shape lets Starlette run these in a
threadpool.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from frisket.contracts.http.action_estimate_validation import (
    ACTION_PREFLIGHT_ERROR_RESPONSES,
    ActionEstimateResult,
    ActionEstimateValidationRequest,
    ActionParamValidationResult,
)
from frisket.contracts.http.action_preview_runs import (
    ACTION_PREVIEW_ERROR_RESPONSES,
    ActionPreviewRunRequest,
    ActionPreviewStartResponse,
    ActionPreviewStatusResponse,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.action_param_validation import (
    ActionParamValidationService,
)
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.import_admission import (
    ImportAdmission,
    import_admission_refusal,
    is_import_action,
)
from frisket.server.services.action_previews import ActionPreviewService


def _preview_media_type(declared: str) -> str:
    # Import MIME metadata is user-authored, not an already-validated HTTP header.
    media_type = declared.split(";", 1)[0].strip().lower()
    if any(ord(char) < 32 or ord(char) > 126 for char in declared) or not re.fullmatch(
        r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type
    ):
        return "application/octet-stream"
    return media_type


def register_action_preview_routes(
    app: FastAPI,
    *,
    service: ActionPreviewService,
) -> None:
    @app.post(
        "/api/projects/{pid}/actions/v1/estimate",
        response_model=ActionEstimateResult,
        response_model_exclude_unset=True,
        responses=ACTION_PREFLIGHT_ERROR_RESPONSES,
    )
    def action_v1_estimate(
        request: Request,
        pid: str,
        body: ActionEstimateValidationRequest,
    ) -> dict[str, Any]:
        return service.estimate(pid, body.action, request_context=request)


def register_action_param_validation_routes(
    app: FastAPI,
    *,
    service: ActionParamValidationService,
) -> None:
    @app.post(
        "/api/projects/{pid}/actions/v1/validate-params",
        response_model=ActionParamValidationResult,
        response_model_exclude_unset=True,
        responses=ACTION_PREFLIGHT_ERROR_RESPONSES,
    )
    def action_v1_validate_params(
        pid: str,
        body: ActionEstimateValidationRequest,
    ) -> dict[str, Any]:
        return service.validate_params(pid, body.action)


def register_action_preview_run_routes(
    app: FastAPI,
    *,
    service: ActionPreviewRunService,
    import_admission: ImportAdmission | None = None,
) -> None:
    @app.post(
        "/api/projects/{pid}/actions/v1/preview",
        status_code=202,
        response_model=ActionPreviewStartResponse,
        response_model_exclude_unset=True,
        responses={
            **ACTION_PREVIEW_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def v1_action_preview_start(
        request: Request,
        pid: str,
        body: ActionPreviewRunRequest,
    ) -> ActionPreviewStartResponse | JSONResponse:
        is_import = is_import_action(body.root.get("action_id"))
        permit = (
            import_admission.try_acquire()
            if import_admission is not None and is_import
            else None
        )
        if import_admission is not None and is_import and permit is None:
            return import_admission_refusal()
        try:
            response = service.start_preview(
                pid,
                body.root,
                request_context=request,
                on_finished=permit.release if permit is not None else None,
            )
        except BaseException:
            if permit is not None:
                permit.release()
            raise
        if response.status_code != 202 and permit is not None:
            permit.release()
        if response.status_code != 202:
            return JSONResponse(
                status_code=response.status_code, content=response.payload
            )
        return ActionPreviewStartResponse.model_validate(response.payload)

    @app.get(
        "/api/projects/{pid}/actions/v1/preview/{preview_id}",
        response_model=ActionPreviewStatusResponse,
        response_model_exclude_unset=True,
        responses={
            **ACTION_PREVIEW_ERROR_RESPONSES,
            **http_error_responses(401, 403, 422, 500),
        },
    )
    def v1_action_preview_status(
        pid: str, preview_id: str
    ) -> ActionPreviewStatusResponse | JSONResponse:
        response = service.get_preview(pid, preview_id)
        if response.status_code != 200:
            return JSONResponse(
                status_code=response.status_code, content=response.payload
            )
        return ActionPreviewStatusResponse.model_validate(response.payload)

    @app.delete(
        "/api/projects/{pid}/actions/v1/preview/{preview_id}",
        status_code=204,
        response_class=Response,
        responses=http_error_responses(401, 403, 404, 422, 500),
    )
    def v1_action_preview_cancel(pid: str, preview_id: str) -> Response:
        response = service.cancel_preview(pid, preview_id)
        return Response(status_code=response.status_code)

    @app.get(
        "/api/projects/{pid}/actions/v1/preview/{preview_id}/artifacts/{artifact_id}",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "An invocation-owned preview file.",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            },
            **http_error_responses(401, 403, 404, 422, 500),
        },
    )
    def v1_action_preview_artifact(pid: str, preview_id: str, artifact_id: str):
        opened = service.open_preview_file(pid, preview_id, artifact_id)
        if opened is None:
            raise HTTPException(status_code=404, detail="Preview file not found.")
        stream, artifact = opened

        def chunks():
            try:
                while chunk := stream.read(64 * 1024):
                    yield chunk
            finally:
                stream.close()

        return StreamingResponse(
            chunks(),
            media_type=_preview_media_type(artifact.mime),
            headers={
                "Content-Length": str(artifact.size),
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.filename, safe='')}",
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
            background=BackgroundTask(stream.close),
        )
