"""Read-only preview route registration."""

from __future__ import annotations

import json

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from frisket.contracts.action import ActionError as V1ActionError
from frisket.contracts.http.entity_mentions import (
    EntityMentionDocumentsRequest,
    EntityMentionDocumentsResponse,
    EntityMentionOccurrencesRequest,
    EntityMentionOccurrencesResponse,
    EntityMentionsPreviewRequest,
    EntityMentionsPreviewResponse,
)
from frisket.contracts.http.preview_comparisons import (
    OcrCompareScratchEstimateResponse,
    TopicSegmentationCompareScratchResponse,
    TranscribeCompareScratchEstimateResponse,
)
from frisket.contracts.http.action_preview_runs import (
    ACTION_PREVIEW_ERROR_RESPONSES,
    ActionPreviewStartResponse,
)
from frisket.contracts.http.translate_comparison import (
    TranslateComparisonRequest,
    TranslateComparisonRequestBody,
    TranslateComparisonResponse,
)
from frisket.contracts.http.resolve_previews import (
    ClusterPreviewRequest,
    ClusterPreviewResponse,
    ColumnValuesPreviewRequest,
    ColumnValuesPreviewResponse,
    ReplaceRulesPreviewRequest,
    ReplaceRulesPreviewResponse,
)
from frisket.server import schemas
from frisket.server.route_errors import http_error_responses
from frisket.server.services.previews import PreviewRequestError, PreviewService
from frisket.server.services.action_preview_runs import ActionPreviewRunService

# A dropped bake-off sample is deliberately small; cap the scratch upload so a
# stray large file can't buffer unbounded bytes into a request-local temp file.
_SCRATCH_MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def _preview_error_response(exc: PreviewRequestError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=V1ActionError(
            code=exc.code,
            message=exc.message,
            field=exc.field,
            details=dict(exc.details or {}),
        ).model_dump(mode="json"),
    )


async def _scratch_input(
    file: UploadFile, payload: str, *, context: str
) -> tuple[bytes, dict]:
    content = await file.read(_SCRATCH_MAX_UPLOAD_BYTES + 1)
    if len(content) > _SCRATCH_MAX_UPLOAD_BYTES:
        raise PreviewRequestError(
            code="invalid_input_ref",
            message=f"{context} upload is too large",
            field="file",
        )
    try:
        parsed = json.loads(payload) if payload else {}
    except json.JSONDecodeError as exc:
        raise PreviewRequestError(
            code="invalid_params",
            message=f"{context} payload must be valid JSON",
            field="payload",
        ) from exc
    if not isinstance(parsed, dict):
        raise PreviewRequestError(
            code="invalid_params",
            message=f"{context} payload must be a JSON object",
            field="payload",
        )
    parsed.setdefault("filename", file.filename)
    parsed.setdefault("mime", file.content_type)
    return content, parsed


def register_preview_routes(
    app: FastAPI,
    *,
    service: PreviewService,
    action_preview_service: ActionPreviewRunService | None = None,
) -> None:
    @app.post("/api/projects/{pid}/queries/v1/preview")
    def query_preview(pid: str, body: schemas.QueryPreviewBody) -> dict:
        """Synchronous, receipt-free query preview.

        This remains the cheap synchronous twin of the receipt-writing
        ``query.preview``
        v1 action: tests/test_query_preview_action.py pins route<->action
        rowset and typed-error parity, and the shared endpoint catalog
        (endpoint_catalog.py ``query_preview``) grants it session_or_pat/
        editor as a programmatic surface. Removal condition:
        remove only when ``POST .../actions/v1/run`` with kind
        ``query.preview`` is declared the sole sanctioned preview path and
        the route-parity pins in tests/test_query_preview_action.py plus the
        ``query_preview`` endpoint-catalog entry are retired in that same
        change.
        """
        try:
            return service.query_preview(
                pid,
                query=body.query,
                limit=body.limit,
                offset=body.offset,
            )
        except PreviewRequestError as exc:
            raise HTTPException(400, exc.query_detail()) from exc

    @app.post("/api/projects/{pid}/ocr/compare-preview")
    async def ocr_compare_preview(pid: str, body: schemas.OcrComparePreviewBody):
        try:
            return await service.ocr_compare_preview(pid, payload=body.model_dump())
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/ocr/compare-scratch/estimate",
        response_model=OcrCompareScratchEstimateResponse,
        response_model_exclude_unset=True,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 422, 500),
        },
    )
    async def ocr_compare_scratch_estimate(
        request: Request,
        pid: str,
        file: UploadFile = File(...),
        payload: str = Form(...),
    ):
        from frisket.preview.ocr import OcrComparePreviewError

        try:
            if action_preview_service is None:
                raise RuntimeError("OCR scratch preview service is unavailable")
            media_bytes, parsed = await _scratch_input(
                file, payload, context="OCR compare scratch"
            )
            (
                plan,
                source,
                _router,
                _composition,
                _execution_context,
            ) = await run_in_threadpool(
                action_preview_service.prepare_ocr_scratch,
                pid,
                media_bytes,
                parsed,
                request_context=request,
            )
            estimate = action_preview_service.scratch_estimate(pid, plan)
            return OcrCompareScratchEstimateResponse.model_validate(
                {
                    "schema_version": "frisket.ocr_compare_estimate.v1",
                    "source": source,
                    "estimate": estimate,
                }
            )
        except OcrComparePreviewError as exc:
            return _preview_error_response(
                PreviewRequestError(
                    code=exc.code,
                    message=exc.message,
                    field=exc.field,
                    details=exc.details,
                )
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/ocr/compare-scratch",
        status_code=202,
        response_model=ActionPreviewStartResponse,
        response_model_exclude_unset=True,
        responses={
            **ACTION_PREVIEW_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 422, 500),
        },
    )
    async def ocr_compare_scratch(
        request: Request,
        pid: str,
        file: UploadFile = File(...),
        payload: str = Form(...),
    ):
        """Start one accounted OCR candidate over transient dropped media."""

        from frisket.preview.ocr import OcrComparePreviewError

        try:
            if action_preview_service is None:
                raise RuntimeError("OCR scratch preview service is unavailable")
            media_bytes, parsed = await _scratch_input(
                file, payload, context="OCR compare scratch"
            )
            confirmation = parsed.pop("confirmation", None)
            if confirmation is not None and not isinstance(confirmation, str):
                raise PreviewRequestError(
                    code="invalid_params",
                    message="confirmation must be a string",
                    field="confirmation",
                )
            (
                plan,
                _source,
                router,
                composition,
                execution_context,
            ) = await run_in_threadpool(
                action_preview_service.prepare_ocr_scratch,
                pid,
                media_bytes,
                parsed,
                request_context=request,
            )
            response = await run_in_threadpool(
                action_preview_service.start_scratch_preview,
                pid,
                plan,
                confirmation=confirmation,
                router=router,
                composition=composition,
                execution_context=execution_context,
            )
            if response.status_code != 202:
                return JSONResponse(
                    status_code=response.status_code, content=response.payload
                )
            return ActionPreviewStartResponse.model_validate(response.payload)
        except OcrComparePreviewError as exc:
            return _preview_error_response(
                PreviewRequestError(
                    code=exc.code,
                    message=exc.message,
                    field=exc.field,
                    details=exc.details,
                )
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/transcribe/compare-scratch/estimate",
        response_model=TranscribeCompareScratchEstimateResponse,
        response_model_exclude_unset=True,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 422, 500),
        },
    )
    async def transcribe_compare_scratch_estimate(
        request: Request,
        pid: str,
        file: UploadFile = File(...),
        payload: str = Form(...),
    ):
        from frisket.server.services.scratch_transcribe import (
            TranscribeComparePreviewError,
        )

        try:
            if action_preview_service is None:
                raise RuntimeError(
                    "transcription scratch preview service is unavailable"
                )
            media_bytes, parsed = await _scratch_input(
                file, payload, context="Transcribe compare scratch"
            )
            (
                plan,
                source,
                _router,
                _composition,
                _execution_context,
            ) = await run_in_threadpool(
                action_preview_service.prepare_transcribe_scratch,
                pid,
                media_bytes,
                parsed,
                request_context=request,
            )
            estimate = action_preview_service.scratch_estimate(pid, plan)
            return TranscribeCompareScratchEstimateResponse.model_validate(
                {
                    "schema_version": "frisket.transcribe_compare_estimate.v1",
                    "source": source,
                    "estimate": estimate,
                }
            )
        except TranscribeComparePreviewError as exc:
            return _preview_error_response(
                PreviewRequestError(
                    code=exc.code,
                    message=exc.message,
                    field=exc.field,
                    details=exc.details,
                )
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/transcribe/compare-scratch",
        status_code=202,
        response_model=ActionPreviewStartResponse,
        response_model_exclude_unset=True,
        responses={
            **ACTION_PREVIEW_ERROR_RESPONSES,
            **http_error_responses(401, 403, 404, 422, 500),
        },
    )
    async def transcribe_compare_scratch(
        request: Request,
        pid: str,
        file: UploadFile = File(...),
        payload: str = Form(...),
    ):
        """Start one accounted transcription over transient dropped media."""

        from frisket.server.services.scratch_transcribe import (
            TranscribeComparePreviewError,
        )

        try:
            if action_preview_service is None:
                raise RuntimeError(
                    "transcription scratch preview service is unavailable"
                )
            media_bytes, parsed = await _scratch_input(
                file, payload, context="Transcribe compare scratch"
            )
            confirmation = parsed.pop("confirmation", None)
            if confirmation is not None and not isinstance(confirmation, str):
                raise PreviewRequestError(
                    code="invalid_params",
                    message="confirmation must be a string",
                    field="confirmation",
                )
            (
                plan,
                _source,
                router,
                composition,
                execution_context,
            ) = await run_in_threadpool(
                action_preview_service.prepare_transcribe_scratch,
                pid,
                media_bytes,
                parsed,
                request_context=request,
            )
            response = await run_in_threadpool(
                action_preview_service.start_scratch_preview,
                pid,
                plan,
                confirmation=confirmation,
                router=router,
                composition=composition,
                execution_context=execution_context,
            )
            if response.status_code != 202:
                return JSONResponse(
                    status_code=response.status_code, content=response.payload
                )
            return ActionPreviewStartResponse.model_validate(response.payload)
        except TranscribeComparePreviewError as exc:
            return _preview_error_response(
                PreviewRequestError(
                    code=exc.code,
                    message=exc.message,
                    field=exc.field,
                    details=exc.details,
                )
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/translate/compare-scratch",
        response_model=TranslateComparisonResponse,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 409, 422, 500),
        },
    )
    async def translate_compare_scratch(
        pid: str, body: TranslateComparisonRequestBody
    ) -> TranslateComparisonResponse | JSONResponse:
        """Bake-off translation over a pasted text sample — scratch, no writes.

        JSON body (no upload); text-source, so no
        multipart like ocr/transcribe."""
        if isinstance(body, TranslateComparisonRequest):
            payload = body.model_dump()
        else:
            payload = body.root
        try:
            return TranslateComparisonResponse.model_validate(
                await service.translate_compare_scratch(pid, payload=payload)
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/topic-segmentation/compare-scratch",
        response_model=TopicSegmentationCompareScratchResponse,
        response_model_exclude_unset=True,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 500),
        },
    )
    async def topic_segmentation_compare_scratch(
        pid: str,
        file: UploadFile = File(...),
        payload: str = Form(...),
    ):
        """Compare topic segmentation over TXT/SRT/VTT — scratch, no writes."""

        try:
            transcript_bytes, parsed = await _scratch_input(
                file, payload, context="Topic Compare scratch"
            )
            return TopicSegmentationCompareScratchResponse.model_validate(
                await service.topic_segmentation_compare_scratch(
                    pid,
                    transcript_bytes=transcript_bytes,
                    payload=parsed,
                )
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/column-values/v1/preview",
        response_model=ColumnValuesPreviewResponse,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 409, 422, 500),
        },
    )
    def column_values_preview(pid: str, body: ColumnValuesPreviewRequest):
        """Synchronous, receipt-free distinct-values preview for one column.

        The read-only enumeration surface for the resolve.substitute /
        resolve.combine authoring UIs: frequency-sorted ``{value, count}``
        rows with search + paging."""
        try:
            return service.column_values_preview(
                pid,
                sheet_id=body.sheet_id,
                input_column=body.input_column,
                search=body.search,
                limit=body.limit,
                offset=body.offset,
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/entity-mentions/v1/preview",
        response_model=EntityMentionsPreviewResponse,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 409, 422, 500),
        },
    )
    def entity_mentions_preview(pid: str, body: EntityMentionsPreviewRequest):
        """Synchronous, receipt-free mention-group preview for the Mentions
        panel.

        Reads what `map.ner` already wrote into a column explicitly marked
        ``semantic_type='entity_mentions'`` — it never starts extraction and
        never adapts an unmarked/legacy column. Returns fingerprint-grouped
        surface groups with exact full-sheet distinct-row counts, every raw
        spelling behind each group, and the ``entity_eq`` selector that
        reproduces that same row count as a grid filter."""
        try:
            return service.entity_mentions_preview(
                pid,
                sheet_id=body.sheet_id,
                column_id=body.column_id,
                search=body.search,
                type=body.type,
                limit=body.limit,
                offset=body.offset,
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/entity-mentions/v1/documents",
        response_model=EntityMentionDocumentsResponse,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 409, 422, 500),
        },
    )
    def entity_mention_documents(pid: str, body: EntityMentionDocumentsRequest):
        """Which documents ONE normalized mention appears in, and how many
        times in each — the mention-detail panel's Level 1.

        The SCOPED counterpart to the preview above. The preview recomputes the
        whole column's GROUP BY, which is the right shape for browsing the
        inventory and the wrong shape for a per-click drill-down; this filters
        the same per-mention stream to one group and pages the rows, so the two
        surfaces cannot disagree about a group's numbers."""
        try:
            return service.entity_mention_documents(
                pid,
                sheet_id=body.sheet_id,
                column_id=body.column_id,
                type=body.type,
                fingerprint=body.fingerprint,
                text=body.text,
                limit=body.limit,
                offset=body.offset,
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/entity-mentions/v1/occurrences",
        response_model=EntityMentionOccurrencesResponse,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 409, 422, 500),
        },
    )
    def entity_mention_occurrences(pid: str, body: EntityMentionOccurrencesRequest):
        """Where inside ONE document a normalized mention occurs, with a text
        snippet around each — the mention-detail panel's Level 2.

        Route A above lists documents cheaply from the entity arrays; this
        resolves one expanded document through the coordinate substrate, so a
        snippet is drawn at coordinates the substrate still vouches for or not
        at all. A cell whose text changed after extraction returns the mismatch
        reason and no occurrences rather than a window cut in the wrong place."""
        try:
            return service.entity_mention_occurrences(
                pid,
                sheet_id=body.sheet_id,
                row_id=body.row_id,
                column_id=body.column_id,
                type=body.type,
                fingerprint=body.fingerprint,
                text=body.text,
                limit=body.limit,
                offset=body.offset,
                snippet_radius=body.snippet_radius,
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/replace-rules/v1/preview",
        response_model=ReplaceRulesPreviewResponse,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 409, 422, 500),
        },
    )
    def replace_rules_preview(pid: str, body: ReplaceRulesPreviewRequest):
        """Synchronous, receipt-free rules evaluation for resolve.replace.

        Evaluates the ordered rules server-side with the SAME engine the
        commit uses (Python ``re``), returning per-rule match counts, the
        unmatched leftovers, and an optional single test-value trace for the
        live tester — so the UI can never disagree with Apply."""
        try:
            return service.replace_rules_preview(
                pid,
                sheet_id=body.sheet_id,
                input_column=body.input_column,
                rules=body.rules,
                unmatched=body.unmatched,
                test_value=body.test_value,
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)

    @app.post(
        "/api/projects/{pid}/clusters/v1/preview",
        response_model=ClusterPreviewResponse,
        responses={
            400: {"model": V1ActionError},
            **http_error_responses(401, 403, 404, 409, 422, 500),
        },
    )
    def cluster_preview(pid: str, body: ClusterPreviewRequest) -> dict:
        """Synchronous, receipt-free cluster preview.

        The read-only, non-action twin of the cluster commit action: it
        computes duplicate-value groups for the chosen method over one column
        with NO receipt and NO side effects, so the web group-card review UI
        can render + edit groups before the user commits. The returned
        ``value_hash`` is fed back as the commit's ``expected_value_hash``
        staleness guard. Semantic clustering errors (``embedding_backend_
        unavailable``) rather than silently falling back to fingerprint.
        """
        try:
            return service.cluster_preview(
                pid,
                sheet_id=body.sheet_id,
                input_column=body.input_column,
                method=body.method,
                min_size=body.min_size,
                threshold=body.threshold,
                ngram_size=body.ngram_size,
                key_template=body.key_template,
            )
        except PreviewRequestError as exc:
            return _preview_error_response(exc)
