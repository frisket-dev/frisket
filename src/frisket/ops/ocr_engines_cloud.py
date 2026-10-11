"""Route-bound OCR through a connected Frisket Cloud account."""

from __future__ import annotations

import mimetypes
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from frisket.cloud_execution import (
    CloudMediaUpload,
    cloud_execution_accounting,
    cloud_media_execution,
)
from frisket.contracts.http.cloud_connection import CloudOCROptions, CloudOCRResult
from frisket.ops.base import OpContext


async def ocr_cloud(
    engine: str,
    pages: list[Path],
    ctx: OpContext,
    usage: dict[str, Any],
    *,
    language: str | None = None,
) -> list[dict[str, Any]]:
    """Send all rendered pages as one paid Cloud execution.

    The runner supplies the durable request identity and exact payer through
    ``OpContext.cloud_execution``.  This adapter owns only the lifetime of the
    open page streams and projects the typed result into the existing OCR
    result shape.
    """

    transport, attempt = cloud_media_execution(ctx, "ocr", engine)
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
                geometry=True,
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
