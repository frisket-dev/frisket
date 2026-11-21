"""Route-bound OCR calls to the Frisket models sidecar."""

from __future__ import annotations

from pathlib import Path

import httpx

from frisket.execution.dispatch_control import dispatch_for_context

from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.ops._sidecar import sidecar_post
from frisket.ops.base import ActionContext, ActionProgramInvocationHalt
from frisket.ops.ocr_engines_local import LIGHT_ENGINE


async def ocr_sidecar(engine: str, pages: list[Path], ctx: ActionContext) -> list[dict]:
    """POST /ocr on the frisket-models sidecar: multipart page bytes
    (the sidecar may be another machine — it never reads our disk),
    bearer-token auth, array batching; per page text + bbox blocks.
    No language field — the settled contract is pages + engine only,
    and its engines auto-detect script. URL/bearer/429-retry use the shared
    ``sidecar_post``."""
    files = [("files", (p.name, p.read_bytes(), "image/png")) for p in pages]
    admission = routed_admission_in_scope(ctx.extras)
    preview = preview_resolution_in_scope(ctx.extras)
    if admission is not None:
        connection = admission.binding.connection
    elif preview is not None:
        connection = preview.connection
    else:
        raise ActionProgramInvocationHalt(
            "promise_violation", "OCR gateway requires its admitted route"
        )
    body = await sidecar_post(
        ctx,
        "/ocr",
        dispatch=dispatch_for_context(ctx),
        files=files,
        data={"engine": engine},
        op="ocr",
        light_engine=LIGHT_ENGINE,
        timeout=httpx.Timeout(
            connect=float(connection.connect_timeout_seconds or 10.0),
            read=float(connection.timeout_seconds or 3600.0),
            write=300.0,
            pool=10.0,
        ),
        connection=connection,
    )
    returned_pages = body.get("pages") if isinstance(body, dict) else None
    if not isinstance(returned_pages, list):
        raise ValueError("OCR sidecar response must contain a pages list")
    return returned_pages
