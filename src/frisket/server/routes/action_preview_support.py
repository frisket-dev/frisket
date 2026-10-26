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

from fastapi import FastAPI, HTTPException, Query, Request
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
    ActionPreviewErrorResponse,
    ActionPreviewRunRequest,
    ActionPreviewStartResponse,
    ActionPreviewStatusResponse,
)
from frisket.contracts.http.action_compare import (
    ActionCompareContextRequest,
    ActionCompareContextResponse,
    ActionComparePreviewRequest,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.action_param_validation import (
    ActionParamValidationService,
)
from frisket.server.services.action_preview_runs import ActionPreviewRunService
from frisket.server.services.action_compare import ActionCompareService
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
    compare_service: ActionCompareService | None = None,
    import_admission: ImportAdmission | None = None,
) -> None:
    if compare_service is not None:

        @app.post(
            "/api/projects/{pid}/actions/v1/compare-context",
            response_model=ActionCompareContextResponse,
            response_model_exclude_unset=True,
            responses=http_error_responses(400, 401, 403, 404, 409, 422, 500),
        )
        def v1_action_compare_context(
            pid: str, body: ActionCompareContextRequest
        ) -> ActionCompareContextResponse:
            return ActionCompareContextResponse.model_validate(
                compare_service.context(pid, body.model_dump(mode="json"))
            )

        @app.post(
            "/api/projects/{pid}/actions/v1/compare-preview",
            status_code=202,
            response_model=ActionPreviewStartResponse,
            response_model_exclude_unset=True,
            responses={
                **ACTION_PREVIEW_ERROR_RESPONSES,
                409: {"model": ActionPreviewErrorResponse},
                **http_error_responses(401, 403, 422, 500),
            },
        )
        def v1_action_compare_preview_start(
            request: Request,
            pid: str,
            body: ActionComparePreviewRequest,
        ) -> ActionPreviewStartResponse | JSONResponse:
            response = service.start_compare_preview(
                pid, body.model_dump(mode="json"), request_context=request
            )
            if response.status_code != 202:
                return JSONResponse(
                    status_code=response.status_code, content=response.payload
                )
            return ActionPreviewStartResponse.model_validate(response.payload)

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
    def v1_action_preview_cancel(
        pid: str,
        preview_id: str,
        discard: bool = Query(default=False),
    ) -> Response:
        response = (
            service.cancel_preview(pid, preview_id, discard=True)
            if discard
            else service.cancel_preview(pid, preview_id)
        )
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
    def v1_action_preview_artifact(
        request: Request, pid: str, preview_id: str, artifact_id: str
    ):
        opened = service.open_preview_file(pid, preview_id, artifact_id)
        if opened is None:
            raise HTTPException(status_code=404, detail="Preview file not found.")
        stream, artifact = opened

        status_code = 200
        start = 0
        end = artifact.size - 1
        selected = re.fullmatch(
            r"bytes=(\d{0,18})-(\d{0,18})", request.headers.get("range", "")
        )
        if request.headers.get("range"):
            if selected is None or not any(selected.groups()):
                stream.close()
                return Response(
                    status_code=416,
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{artifact.size}",
                    },
                )
            raw_start, raw_end = selected.groups()
            if raw_start:
                start = int(raw_start)
                requested_end = int(raw_end) if raw_end else artifact.size - 1
                if requested_end < start:
                    start = artifact.size
                end = min(requested_end, artifact.size - 1, start + 65_535)
            else:
                suffix = min(int(raw_end), 65_536)
                start = max(0, artifact.size - suffix)
                end = artifact.size - 1
            if start >= artifact.size or end < start:
                stream.close()
                return Response(
                    status_code=416,
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{artifact.size}",
                    },
                )
            stream.seek(start)
            status_code = 206

        remaining = end - start + 1

        def chunks():
            left = remaining
            try:
                while left and (chunk := stream.read(min(64 * 1024, left))):
                    left -= len(chunk)
                    yield chunk
            finally:
                stream.close()

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(remaining),
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.filename, safe='')}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{artifact.size}"
        return StreamingResponse(
            chunks(),
            status_code=status_code,
            media_type=_preview_media_type(artifact.mime),
            headers=headers,
            background=BackgroundTask(stream.close),
        )
