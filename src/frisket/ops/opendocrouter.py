"""Admitted OpenDocRouter calls and OCR block projection."""

from __future__ import annotations

import math
from pathlib import Path

from frisket.ai.models.metadata import provider_cost_total
from frisket.credentials import resolve_credential_for_use
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.targets import CAPABILITY_OCR
from frisket.ops.base import OpContext
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.ops.integrations.opendocrouter import parse_document
from frisket.ops.media_metadata import image_dimensions
from frisket.ops.ocr_engines_hosted import halt_unless_consented_credential
from frisket.sdk.ops._hosted_accounting import persist_hosted_accepted_accounting


async def parse_with_context(
    *,
    ctx: OpContext,
    path: Path,
    engine: str,
    capability: str,
    layout: bool = False,
    on_accounting=None,
):
    credential = resolve_credential_for_use(
        ctx.project, "OPEN_DOC_ROUTER_API_KEY", context=ctx.credential_use_context
    )
    if credential is None:
        raise HostedEngineError(
            "auth", "Add an OpenDocRouter API key to use this engine."
        )
    admission = routed_admission_in_scope(ctx.extras)
    if admission:
        halt_unless_consented_credential(
            admission.route,
            credential.source,
            "OpenDocRouter document processing",
            selected_owner=credential.owner,
            context=ctx.credential_use_context,
        )

    accepted = False

    def record(accounting):
        nonlocal accepted
        if not accepted:
            persist_hosted_accepted_accounting(ctx, accounting)
            accepted = True
        if on_accounting:
            on_accounting(accounting)

    try:
        return await parse_document(
            ctx.http,
            path=path,
            engine=engine,
            api_key=credential.value,
            capability=capability,
            credential_source=credential.source,
            layout=layout,
            should_cancel=(ctx.extras or {}).get("cancelled"),
            on_accounting=record,
        )
    except HostedEngineError as exc:
        # A halted/cancelled job has no returned-row writer. Preserve a final
        # charge learned during cancellation reconciliation before propagating.
        if exc.provider_job_accepted and exc.accounting:
            try:
                persist_hosted_accepted_accounting(ctx, exc.accounting)
            except Exception as persistence_error:
                # Keep the accepted-effect marker even when the ledger cannot
                # be written: an ordinary row failure could be resubmitted.
                raise exc from persistence_error
        raise


def ocr_page(page: dict, size: tuple[int, int] | None) -> dict:
    """Normalize block geometry to the input image's pixel coordinate frame.

    An element's quote can span multiple rectangles. Use their envelope as a
    block, never duplicate its text or pretend that these are word boxes.
    Layout failure leaves usable Markdown with no fabricated geometry.
    """
    text = page["markdown"]
    lines = text.split("\n")
    layout = page.get("layout") or {}
    blocks = []
    if layout.get("status") == "ok" and size is not None:
        for element in layout.get("elements", []):
            span = element.get("lines")
            boxes = element.get("boxes") or []
            if not (
                isinstance(span, list)
                and len(span) == 2
                and all(type(i) is int for i in span)
                and 0 <= span[0] <= span[1] < len(lines)
            ):
                continue
            quote = "\n".join(lines[span[0] : span[1] + 1])
            if not quote.strip() or not boxes:
                continue
            rects = []
            for box in boxes:
                try:
                    x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
                    if (
                        not all(math.isfinite(v) for v in (x, y, w, h))
                        or w <= 0
                        or h <= 0
                    ):
                        continue
                    # Rotated boxes need a polygon transformation; do not claim
                    # unrotated precision for them.
                    if box.get("r", 0) != 0:
                        continue
                    rects.append((max(0, x), max(0, y), min(1, x + w), min(1, y + h)))
                except (KeyError, ValueError, TypeError):
                    continue
            if not rects:
                continue
            x0 = min(r[0] for r in rects) * size[0]
            y0 = min(r[1] for r in rects) * size[1]
            x1 = max(r[2] for r in rects) * size[0]
            y1 = max(r[3] for r in rects) * size[1]
            if x0 >= x1 or y0 >= y1:
                continue
            block = {"text": quote, "bbox": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}
            score = element.get("confidence")
            if (
                isinstance(score, (int, float))
                and math.isfinite(score)
                and 0 <= score <= 1
            ):
                block["score"] = score
            blocks.append(block)
    return {"text": text, "blocks": blocks}


async def ocr_pages(
    engine: str, paths: list[Path], ctx: OpContext, usage: dict
) -> list[dict]:
    facts: dict[str, dict] = {}

    def record(accounting):
        for fact in accounting["model_calls"]:
            facts[fact["id"]] = fact
        usage.update(
            model_calls=list(facts.values()),
            calls=len(facts),
            cost=provider_cost_total(f["provider_cost_usd"] for f in facts.values()),
            **{
                "in": sum(f["units"].get("input_tokens", 0) for f in facts.values()),
                "out": sum(f["units"].get("output_tokens", 0) for f in facts.values()),
            },
        )

    result = []
    for path in paths:
        pages, _ = await parse_with_context(
            ctx=ctx,
            path=path,
            engine=engine,
            capability=CAPABILITY_OCR,
            layout=True,
            on_accounting=record,
        )
        result.append(ocr_page(pages[0], image_dimensions(path.read_bytes())))
    return result
