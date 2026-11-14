"""Route-bound OCR calls to the Frisket models sidecar."""

from __future__ import annotations

from pathlib import Path

import httpx

from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.ops._sidecar import sidecar_post
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops.ocr_engines_local import LIGHT_ENGINE


async def ocr_sidecar(engine: str, pages: list[Path], ctx: OpContext) -> list[dict]:
    """POST /ocr on the frisket-models sidecar: multipart page bytes
    (the sidecar may be another machine — it never reads our disk),
    bearer-token auth, array batching; per page text + bbox blocks.
    No language field — the settled contract is pages + engine only,
    and its engines auto-detect script. URL/bearer/429-retry use the shared
    ``sidecar_post``."""
    files = [("files", (p.name, p.read_bytes(), "image/png")) for p in pages]
    admission = routed_admission_in_scope(ctx.extras)
    preview = preview_resolution_in_scope(ctx.extras)
    if admission is None and preview is None:
        raise RecipeInvocationHalt(
            "promise_violation", "OCR gateway requires its admitted route"
        )
    connection = (
        admission.binding.connection if admission is not None else preview.connection
    )
    policy = getattr(
        ctx.extras.get("router"), "model_call_policy", None
    ) or ctx.extras.get("service_operation_policy")
    operation_id = None
    if policy is not None:
        import hashlib

        operation_id = policy.admit_ocr(
            engine=engine,
            prepared={
                "engine": engine,
                "pages": [hashlib.sha256(data[1][1]).hexdigest() for data in files],
            },
            page_count=len(pages),
            context=ctx,
        )
    try:
        body = await sidecar_post(
            ctx,
            "/ocr",
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
    except BaseException as error:
        if operation_id is not None:
            policy.fail_service(operation_id, error)
        raise
    if operation_id is not None:
        policy.finish_service(
            operation_id, result=body, usage={"pages": len(body["pages"])}
        )
    return body["pages"]
