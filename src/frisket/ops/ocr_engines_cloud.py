"""Route-bound OCR through a connected Frisket Cloud account."""

from __future__ import annotations

import mimetypes
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from frisket.cloud_execution import (
    CloudExecutionRefused,
    CloudMediaUpload,
    cloud_execution_accounting,
    cloud_media_execution,
)
from frisket.contracts.http.cloud_connection import CloudOCROptions, CloudOCRResult
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.targets import CAPABILITY_OCR, OcrOptionSupport
from frisket.ops.base import OpContext


def _admitted_support(ctx: OpContext, engine: str) -> OcrOptionSupport:
    admission = routed_admission_in_scope(ctx.extras)
    if (
        admission is None
        or admission.binding.facts.engine != engine
        or admission.route.engine != engine
        or admission.route.target_snapshot.get("capability") != CAPABILITY_OCR
    ):
        raise CloudExecutionRefused(
            "route_changed", "Cloud OCR requires its exact admitted engine support"
        )
    support = next(
        (
            support
            for support in admission.binding.target.engines
            if support.capability == CAPABILITY_OCR and support.engine == engine
        ),
        None,
    )
    if support is None or not isinstance(support.options, OcrOptionSupport):
        raise CloudExecutionRefused(
            "route_changed", "Cloud OCR requires its exact admitted engine support"
        )
    return support.options


def cloud_pdf_input_supported(ctx: OpContext, engine: str) -> bool:
    """Return the PDF support fixed by the exact admitted Cloud route."""

    return _admitted_support(ctx, engine).pdf_input


async def ocr_cloud(
    engine: str,
    pages: list[Path],
    ctx: OpContext,
    usage: dict[str, Any],
    *,
    language: str | None = None,
    keep_text_position: bool | None = None,
) -> list[dict[str, Any]]:
    """Send all rendered pages as one paid Cloud execution.

    The runner supplies the durable request identity and exact payer through
    ``OpContext.cloud_execution``.  This adapter owns only the lifetime of the
    open page streams and projects the typed result into the existing OCR
    result shape.
    """

    transport, attempt = cloud_media_execution(ctx, "ocr", engine)
    geometry_support = _admitted_support(ctx, engine).geometry
    if keep_text_position is True and not geometry_support:
        raise CloudExecutionRefused(
            "route_changed", "Cloud OCR route does not support positioned text"
        )
    geometry = geometry_support if keep_text_position is None else keep_text_position
    if not pages:
        raise ValueError("Cloud OCR requires at least one rendered page")

    with ExitStack() as stack:
        uploads = [
            CloudMediaUpload(
                file_name=page.name,
                content_type=mimetypes.guess_type(page.name)[0]
                or "application/octet-stream",
                stream=stack.enter_context(page.open("rb")),
            )
            for page in pages
        ]
        response = await transport.execute_media(
            attempt,
            capability="ocr",
            engine=engine,
            options=CloudOCROptions(
                language=language,
                geometry=geometry,
                searchable_pdf=False,
            ),
            uploads=uploads,
        )

    accounting = cloud_execution_accounting(attempt, response, capability="ocr")
    usage.update(accounting)
    result = response.result
    if not isinstance(result, CloudOCRResult):
        raise RuntimeError("Cloud OCR returned no OCR result")
    if len(result.pages) != len(pages):
        raise RuntimeError(
            "Cloud OCR result page count differs from the uploaded page count"
        )
    expected_pages = list(range(1, len(pages) + 1))
    if [page.page for page in result.pages] != expected_pages:
        raise RuntimeError("Cloud OCR result pages are not ordered and contiguous")
    return [page.model_dump(mode="json") for page in result.pages]


async def ocr_cloud_pdf(
    engine: str,
    pdf: Path,
    ctx: OpContext,
    usage: dict[str, Any],
    *,
    page_count: int,
    dpi: int,
    language: str | None = None,
    keep_text_position: bool | None = None,
) -> list[dict[str, Any]]:
    """Send one admitted PDF as one Cloud purchase without local rasterization."""

    transport, attempt = cloud_media_execution(ctx, "ocr", engine)
    support = _admitted_support(ctx, engine)
    if not support.pdf_input:
        raise CloudExecutionRefused(
            "route_changed",
            "Cloud OCR route does not support PDF input; reconnect or upgrade Frisket Cloud",
        )
    if keep_text_position is True and not support.geometry:
        raise CloudExecutionRefused(
            "route_changed", "Cloud OCR route does not support positioned text"
        )
    geometry = support.geometry if keep_text_position is None else keep_text_position
    if type(page_count) is not int or page_count <= 0:
        raise ValueError("Cloud PDF OCR requires a positive page count")

    with pdf.open("rb") as stream:
        response = await transport.execute_media(
            attempt,
            capability="ocr",
            engine=engine,
            options=CloudOCROptions(
                language=language,
                geometry=geometry,
                searchable_pdf=False,
                pdf_dpi=dpi,
            ),
            uploads=[
                CloudMediaUpload(
                    file_name="source.pdf",
                    content_type="application/pdf",
                    stream=stream,
                )
            ],
        )

    accounting = cloud_execution_accounting(attempt, response, capability="ocr")
    usage.update(accounting)
    result = response.result
    if not isinstance(result, CloudOCRResult):
        raise RuntimeError("Cloud PDF OCR returned no OCR result")
    if len(result.pages) != page_count:
        raise RuntimeError("Cloud PDF OCR result page count differs from the source")
    if [page.page for page in result.pages] != list(range(1, page_count + 1)):
        raise RuntimeError("Cloud PDF OCR result pages are not ordered and contiguous")
    if geometry and any(
        page.error is None and (page.width is None or page.height is None)
        for page in result.pages
    ):
        raise RuntimeError("positioned Cloud PDF OCR returned no coordinate frame")
    return [page.model_dump(mode="json") for page in result.pages]
