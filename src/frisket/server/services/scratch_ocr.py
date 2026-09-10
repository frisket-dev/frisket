"""Receipt-backed OCR previews over transient uploaded media."""

from __future__ import annotations

import hashlib
import io
import tempfile
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Mapping

from frisket.engine.store import Project
from frisket.ops.ocr_engines import (
    LIGHT_ENGINE,
    OcrEngines,
    rapidocr_execution_scope,
)
from frisket.preview.ocr import (
    OcrComparePreviewError,
    OcrCompareSource,
    _canonical_engine,
    _coerce_pages,
    _page_messages,
    _str_or_none,
    normalize_ocr_blocks,
    render_selected_pdf_pages,
)
from frisket.redaction import safe_error


def paid_ocr_scratch_plan(
    project: Project,
    *,
    media_bytes: bytes,
    payload: Mapping[str, Any],
    composition: Any,
) -> tuple[Any, dict[str, Any]]:
    """Prepare one accounted OCR candidate over transient image/PDF bytes."""

    from frisket.actions.media import OcrParams, ocr_options
    from frisket.execution.resolve_for_action import (
        BoundedScratchInput,
        resolve_for_action,
    )
    from frisket.execution.resolver import Refusal, ResolvedExecution
    from frisket.server.services.scratch_action_preview import (
        ScratchActionPreviewPlan,
        ScratchActionRecipe,
        scratch_estimate,
    )

    engine = payload.get("engine")
    if not isinstance(engine, str) or not engine.strip():
        raise OcrComparePreviewError(
            "invalid_params", "OCR compare requires one engine", field="engine"
        )
    pages = _coerce_pages(payload.get("pages", [1]))
    params_payload = {
        key: payload[key]
        for key in OcrParams.model_fields
        if key not in {"source", "engine"} and key in payload
    }
    params_payload.update(source="scratch", engine=engine.strip())
    try:
        params = OcrParams.model_validate(params_payload)
        normalized_options = ocr_options(params).normalize(params.engine.root)
    except (TypeError, ValueError) as exc:
        raise OcrComparePreviewError(
            "invalid_params", str(exc), field="payload"
        ) from exc

    filename = _str_or_none(payload.get("filename"))
    mime = _str_or_none(payload.get("mime"))
    source_kind, source_suffix, source_mime = _ocr_source_type(
        media_bytes, filename=filename, mime=mime
    )
    if source_kind == "image" and pages != [1]:
        raise OcrComparePreviewError(
            "invalid_page_ref",
            "OCR image comparison accepts only page 1",
            field="pages",
            details={"missing_pages": [page for page in pages if page != 1]},
        )
    if normalized_options["searchable_pdf"] and source_kind != "pdf":
        raise OcrComparePreviewError(
            "invalid_params",
            "Searchable PDF output requires a PDF upload.",
            field="searchable_pdf",
        )

    canonical_engine = _canonical_engine(params.engine.root)
    digest = hashlib.sha256(media_bytes).hexdigest()
    selection = {"pages": list(pages)}
    source_identity = {"sha256": digest, "selection": selection}
    work_scope = {
        "schema_version": "frisket.scratch-work-scope.v1",
        "identity": digest,
        "selection": selection,
        "source_count": 1,
    }
    spec = {
        "action_kind": "media.ocr",
        "sheet_id": 0,
        "params": params.model_dump(mode="json"),
        "engine": params.engine.root,
        **normalized_options,
    }
    recipe = ScratchActionRecipe("media.ocr", "ocr")
    resolved = resolve_for_action(
        project,
        spec,
        recipe,
        composition=composition,
        bounded_scratch_input=BoundedScratchInput(len(pages), work_scope),
    )
    if isinstance(resolved, Refusal):
        raise OcrComparePreviewError("no_live_target", resolved.remedy, field="engine")
    if not isinstance(resolved, ResolvedExecution):
        raise RuntimeError("OCR scratch resolution returned no route")
    estimate = scratch_estimate(resolved, quantity_name=None, quantity=len(pages))

    async def run(context) -> Any:
        from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
        from frisket.engine.executor.table_preview import (
            PreviewFile,
            TablePreviewResult,
        )
        from frisket.engine.runner.preview import PreviewColumn
        from frisket.execution.attempt import routed_admission_in_scope
        from frisket.execution.runtime_binding import bind_fact_to_route

        if context.cancelled:
            from frisket.ops.base import RecipeInvocationHalt

            raise RecipeInvocationHalt("local_session_failed", "Preview was cancelled.")
        with tempfile.TemporaryDirectory(prefix="frisket-ocr-paid-scratch-") as tmp:
            scratch = Path(tmp)
            source = scratch / f"source{source_suffix}"
            source.write_bytes(media_bytes)
            source_media = {
                "filename": filename or source.name,
                "mime": source_mime,
            }
            recipe_impl = OcrEngines(cancelled=context.cancel_event.is_set)
            async with AsyncExitStack() as stack:
                if canonical_engine == LIGHT_ENGINE:
                    recipe_impl.pool = await stack.enter_async_context(
                        rapidocr_execution_scope(
                            expected_rows=1,
                            language=normalized_options.get("language"),
                        )
                    )
                limits = context.execution_limits
                rendered_document = await render_selected_pdf_pages(
                    OcrCompareSource(
                        sheet_id=0,
                        row_id=0,
                        column_id=0,
                        column_name="scratch",
                        column_type="file",
                        blob_hash=digest,
                        filename=filename,
                        mime=source_mime,
                        size=len(media_bytes),
                        page_count=1 if source_kind == "image" else None,
                        path=source,
                        media=source_media,
                    ),
                    pages,
                    dpi=normalized_options["dpi"],
                    scratch=scratch,
                    max_pdf_pages=(
                        limits.max_pdf_pages if limits is not None else None
                    ),
                )
                rendered = rendered_document.pages
                selected = [page.path for page in rendered]
                usage: dict[str, Any] = {
                    "calls": 0,
                    "in": 0,
                    "out": 0,
                    "cost": 0.0,
                    "duration_ms": 0,
                    "input_pages": len(selected),
                }
                ctx = context.op_context()
                output_pages: list[dict[str, Any]] = []
                returned = False
                started = time.perf_counter()
                try:
                    output_pages = await recipe_impl.run_engine_on_pages(
                        canonical_engine,
                        selected,
                        ctx,
                        usage=usage,
                        language=normalized_options.get("language"),
                        scratch=scratch,
                    )
                    returned = True
                finally:
                    if usage.get("model_calls") or usage["calls"] or returned:
                        calls = usage.get("model_calls") or [
                            recipe_impl._model_call_for(
                                canonical_engine,
                                source,
                                output_pages,
                                usage,
                                ctx=ctx,
                            ).as_dict()
                        ]
                        admission = routed_admission_in_scope(ctx.extras)
                        if admission is not None:
                            calls = [
                                bind_fact_to_route(admission.route, call)
                                for call in calls
                            ]
                        context.write_model_calls(calls)
                runtime_ms = max(0, round((time.perf_counter() - started) * 1000))

            warnings: list[str] = []
            if len(output_pages) != len(rendered):
                warnings.append(
                    "OCR engine returned a different page count than requested."
                )
            rows = []
            failed_pages = 0
            for index, page in enumerate(rendered):
                raw = output_pages[index] if index < len(output_pages) else {}
                text = str(raw.get("text") or "")
                blocks = normalize_ocr_blocks(
                    raw.get("blocks"), width=page.width, height=page.height
                )
                page_warnings = _page_messages(raw.get("warnings"))
                page_errors = _page_messages(raw.get("errors"))
                if raw.get("error"):
                    page_errors.extend(_page_messages(raw["error"]))
                if index >= len(output_pages):
                    page_errors.append("OCR engine returned no result for this page.")
                if page_errors and not text and not blocks:
                    failed_pages += 1
                warnings.extend(
                    f"Page {page.page}: {message}" for message in page_warnings
                )
                warnings.extend(
                    f"Page {page.page} failed: {message}" for message in page_errors
                )
                rows.append(
                    {
                        "page": {"value": page.page},
                        "text": {"value": text},
                        "blocks": {"value": blocks},
                        "warnings": {"value": page_warnings},
                        "errors": {"value": page_errors},
                        "runtime_ms": {"value": runtime_ms},
                    }
                )
                context.progress(index + 1, len(rendered))

            columns = [
                PreviewColumn(name="page", column_type="number"),
                PreviewColumn(name="text", column_type="text"),
                PreviewColumn(name="blocks", column_type="json"),
                PreviewColumn(name="warnings", column_type="json"),
                PreviewColumn(name="errors", column_type="json"),
                PreviewColumn(name="runtime_ms", column_type="number"),
            ]
            if failed_pages == len(rendered):
                raise OcrComparePreviewError(
                    "ocr_preview_engine_failed",
                    "OCR failed on every selected page.",
                    field="engine",
                    details={"failed_pages": [page.page for page in rendered]},
                )
            artifacts = {}
            stager = None
            if normalized_options["searchable_pdf"]:
                from frisket.authoring.output_names import (
                    OutputNameTemplateError,
                    format_output_name,
                    source_stem,
                )
                from frisket.ops.searchable_pdf import compose_searchable_pdf

                overlays: list[dict[str, Any]] = [
                    {} for _item in range(rendered_document.page_count or max(pages))
                ]
                for page, output in zip(pages, output_pages, strict=False):
                    overlays[page - 1] = output
                try:
                    searchable, composed = compose_searchable_pdf(
                        media_bytes, overlays, dpi=normalized_options["dpi"]
                    )
                    try:
                        stem = source_stem(filename or "document.pdf")
                    except OutputNameTemplateError:
                        stem = "document"
                    output_filename = format_output_name(
                        "{source_stem}.searchable.pdf", {"source_stem": stem}
                    )
                    stager = AdmittedImportBlobStager(
                        cancelled=context.cancel_event.is_set
                    )
                    handle = stager.stage(
                        io.BytesIO(searchable),
                        filename=output_filename,
                        mime="application/pdf",
                    )
                    artifact = PreviewFile(uuid.uuid4().hex)
                    artifacts[artifact.id] = stager.describe(handle)
                    columns.append(PreviewColumn(name="pdf", column_type="file"))
                    for index, row in enumerate(rows):
                        row["pdf"] = {"value": artifact if index == 0 else None}
                    degraded = [item.index + 1 for item in composed if item.degraded]
                    if degraded:
                        warnings.append(
                            "Searchable PDF text was degraded on pages "
                            + ", ".join(str(page) for page in degraded)
                            + "."
                        )
                except Exception as exc:  # noqa: BLE001 - text rows remain useful
                    safe = safe_error("searchable_pdf_failed", exc, max_chars=300)
                    warnings.append(safe.detail)
                    if stager is not None:
                        stager.close()
                        stager = None
                    artifacts = {}

            return TablePreviewResult(
                columns=columns,
                rows=rows,
                total=len(rows),
                warnings=tuple(warnings),
                artifacts=artifacts,
                stager=stager,
            )

    return (
        ScratchActionPreviewPlan(
            action_kind="media.ocr",
            recipe=recipe,
            spec=spec,
            resolved_execution=resolved,
            estimate=estimate,
            work_scope=work_scope,
            source_identity=source_identity,
            run=run,
        ),
        {
            "filename": filename,
            "mime": mime,
            "size": len(media_bytes),
            "kind": source_kind,
            "pages": list(pages),
        },
    )


def _ocr_source_type(
    media_bytes: bytes, *, filename: str | None, mime: str | None
) -> tuple[str, str, str]:
    if media_bytes.startswith(b"%PDF-"):
        return "pdf", ".pdf", "application/pdf"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(media_bytes)) as image:
            image.verify()
            image_format = str(image.format or "").upper()
    except Exception as exc:
        raise OcrComparePreviewError(
            "invalid_input_ref",
            "OCR compare requires a readable image or PDF upload.",
            field="file",
            details={"filename": filename, "mime": mime},
        ) from exc
    formats = {
        "BMP": (".bmp", "image/bmp"),
        "GIF": (".gif", "image/gif"),
        "JPEG": (".jpg", "image/jpeg"),
        "PNG": (".png", "image/png"),
        "TIFF": (".tiff", "image/tiff"),
        "WEBP": (".webp", "image/webp"),
    }
    suffix, detected_mime = formats.get(image_format, (".img", "image/*"))
    return "image", suffix, detected_mime
