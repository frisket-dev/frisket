"""Hosted Datalab and routed vision-model OCR engines."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.credential_use import (
    CredentialUseContext,
    CredentialUseRefusal,
    require_consented_credential,
)
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops.media_metadata import image_dimensions

DATALAB_ENGINE = "datalab"
MINIMAX_M3_ENGINE = "openrouter/minimax/minimax-m3"
MINIMAX_M3_MAX_IMAGE_EDGE = 3584


def _datalab_credential(ctx: OpContext) -> Any:
    """Resolve value, source, and owner together before checking consent."""
    from frisket.credentials import resolve_credential_for_use
    from frisket.execution.definitions import DATALAB_API_KEY_ENV

    return resolve_credential_for_use(
        ctx.project,
        DATALAB_API_KEY_ENV,
        context=ctx.credential_use_context,
    )


def _datalab_credential_parts(
    credential: Any,
) -> tuple[str | None, str, Any]:
    if credential is None:
        return None, "none", None
    return credential.value, credential.source, credential.owner


def halt_unless_consented_credential(
    route: Any,
    selected_source: Any,
    effect: str,
    *,
    selected_owner: Any = None,
    context: CredentialUseContext,
) -> None:
    """The §3.5 fence's halt vocabulary, at the dispatch layer that owns it —
    the OCR twin of ``sdk/ops/transcribe_engines.py``'s. ``credential_use`` states the
    direct constraint and refuses before effect; ``run.backfill`` is the one
    supported resume door. The halt vocabulary is owned by ``ops.base``,
    which the execution leaf modules deliberately do not import."""
    try:
        require_consented_credential(
            cost_posture=route.cost_posture,
            selected_source=selected_source,
            selected_owner=selected_owner,
            context=context,
            effect=effect,
        )
    except CredentialUseRefusal as exc:
        raise RecipeInvocationHalt(
            "promise_violation",
            f"{exc}; select an authorized credential before resuming",
        ) from exc


async def ocr_datalab(pages: list[Path], ctx: OpContext, usage: dict) -> list[dict]:
    """Datalab's hosted OCR API (datalab.to), pay-per-call,
    migrated onto ``/convert(output_format=
    "json")`` per Datalab's own migration guide for the deprecated
    ``/ocr`` endpoint. Credential-gated via DATALAB_API_KEY
    (``resolve_credential``: env -> project secrets). Mirrors
    ``_ocr_vlm``'s per-page loop (pages are already rasterized upstream
    by ``_page_images``) rather than batching like the sidecar client:
    Datalab's ``/convert`` takes one ``file`` per call, and per-page cost
    is identical either way (billed per page, not per call).

    Every page's actual reported cost
    (``datalab_ocr``'s ``cost_usd``) accumulates into ``usage["cost"]``
    and ``usage["calls"]`` increments per page, so the recipe's
    ``(data, meta)`` return carries the REAL spend into MapRunner/
    receipts instead of the silent-zero default. A page whose response
    omitted cost fields (``cost_usd is None``) falls back to the
    external_pricing per-page estimate for exactly that page and flags
    ``usage["cost_estimated"] = True`` so the caller can mark the
    result as estimated rather than reported.

    Cancellation checks
    ``ctx.extras['cancelled']`` (the SAME cooperative-cancel signal
    MapRunner checks between rows, runner/map_runner.py's ``one_row``)
    before every page, and threads it into ``datalab_ocr`` so a
    multi-minute poll loop mid-page stops promptly instead of only
    after the whole row completes."""
    from frisket.execution.price_book import datalab_ocr_page_rate
    from frisket.ops.integrations.datalab import DatalabEngineError, datalab_ocr
    from frisket.ops.media_metadata import image_dimensions

    # ONE resolution for the key AND its provenance: the fence below must
    # be checking the credential this call actually sends, not a second
    # answer computed from the same inputs.
    api_key, credential_source, credential_owner = _datalab_credential_parts(
        _datalab_credential(ctx)
    )
    if not api_key:
        raise DatalabEngineError(
            code="auth",
            message="DATALAB_API_KEY is not configured (Settings → Secrets).",
        )
    # Check the credential class selected by this dispatch against the
    # class named by consent before the first page leaves the machine.
    # Hosted environment credentials have already normalized to the
    # platform class + deployment owner here. An org-BYOK consent that
    # selects that deployment key still refuses without spending.
    admission = routed_admission_in_scope(ctx.extras)
    if admission is not None:
        halt_unless_consented_credential(
            admission.route,
            credential_source,
            "the hosted Datalab OCR call",
            selected_owner=credential_owner,
            context=ctx.credential_use_context,
        )
    usage["credential_source"] = credential_source
    should_cancel = (ctx.extras or {}).get("cancelled")
    out: list[dict] = []
    for p in pages:
        if should_cancel is not None and should_cancel():
            raise DatalabEngineError(
                code="cancelled",
                message="Datalab OCR stopped: the run was cancelled.",
            )
        page_bytes = p.read_bytes()
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        page_size = image_dimensions(page_bytes)
        accepted_accounting: dict[str, Any] | None = None

        def persist_accepted(accounting: dict[str, Any]) -> None:
            nonlocal accepted_accounting
            from frisket.sdk.ops._datalab_accounting import (
                persist_datalab_accepted_accounting,
            )

            accepted_accounting = accounting
            persist_datalab_accepted_accounting(ctx, accounting)
            usage.setdefault("model_calls", []).extend(accounting["model_calls"])

        page_results, cost_usd = await datalab_ocr(
            ctx.http,
            api_key,
            page_bytes,
            p.name,
            mime,
            page_size=page_size,
            should_cancel=should_cancel,
            credential_source=credential_source,
            on_accepted=persist_accepted,
        )
        if accepted_accounting is None:
            raise RuntimeError("Datalab returned without its accepted-job accounting")
        out.extend(page_results)
        usage["calls"] += 1
        if cost_usd is None:
            # The page's own reported cost is missing, so this page is
            # rated at the provider's list price — asked of THE price
            # book (the one-mint rule, now covering the per-page rate
            # too), never re-read from the pricing catalog here.
            rate = datalab_ocr_page_rate()
            estimate = float(rate) if rate is not None else None
            usage["cost_estimated"] = True
            if usage["cost"] is not None and estimate is not None:
                usage["cost"] += estimate
            else:
                usage["cost"] = None
        elif usage["cost"] is not None:
            usage["cost"] += cost_usd
        if accepted_accounting is not None:
            [fact] = accepted_accounting["model_calls"]
            fact["units"] = {"pages": 1, "requests": 1}
            if cost_usd is None:
                fact["provider_cost_usd"] = estimate
                fact["cost_source"] = "estimated" if estimate is not None else "unknown"
            if admission is not None:
                bound = bind_fact_to_route(admission.route, fact)
                fact.clear()
                fact.update(bound)
    return out


async def ocr_vlm(
    model: str,
    pages: list[Path],
    ctx: OpContext,
    usage: dict,
    language: str | None = None,
    *,
    recipe_version: str,
) -> list[dict]:
    """Remote VLM engines ride the existing router: one structured-output
    call per page with the page as an image part. ``language`` rides the
    user turn as a hint — cheap, and it steers ambiguous scripts."""
    from frisket.ai.llm.structured import (
        StructuredCompleter,
        StructuredRequest,
        _sum_response,
    )

    router = (ctx.extras or {}).get("router")
    if router is None:
        raise RuntimeError("remote ocr engines need the model router in OpContext")
    routed = routed_admission_in_scope(ctx.extras)
    if routed is not None:
        # §3.5 pre-effect USE constraint, the same seat the remote
        # TRANSCRIPTION path holds: the router genuinely chooses among a
        # project key, an org BYOK key and the platform's own, so the class
        # it selected is compared to the class the consent named before the
        # page bytes leave the machine. A router that cannot report its
        # provenance reports NONE — never a fabricated "local", which would
        # make the check pass for the wrong reason.
        provider = model.partition("/")[0]
        reader = getattr(router, "credential_source_for", None)
        halt_unless_consented_credential(
            routed.route,
            reader(provider) if callable(reader) else "none",
            f"the {provider} vision-model OCR call",
            context=ctx.credential_use_context,
        )
    schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "every word of text visible, in reading order",
            }
        },
        "required": ["text"],
    }
    ask = "Transcribe the text."
    if language:
        ask += f" The text is in {language}."
    out: list[dict] = []
    wire_history = []
    usage["input_pages"] = 0
    for p in pages:
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        image_data = p.read_bytes()
        temperature = 0.0
        params: dict[str, Any] = {}
        reasoning_policy = None
        if model == MINIMAX_M3_ENGINE:
            # MiniMax recommends temperature=1.0 and top_p=0.95 for M3;
            # its docs say disabled thinking minimizes latency, and its
            # OmniDocBench evaluation bounds the image long edge at 3,584px.
            # Keep this exact profile local to the one curated OCR model
            # instead of inventing a general prompt-profile system.
            from frisket.ai.vision.region_locator import prepare_image_asset

            dimensions = image_dimensions(image_data)
            if dimensions is None or max(dimensions) > MINIMAX_M3_MAX_IMAGE_EDGE:
                image = prepare_image_asset(
                    image_data,
                    source_media_type=mime,
                    max_edge=MINIMAX_M3_MAX_IMAGE_EDGE,
                )
                image_data = image.data
                mime = image.media_type
            temperature = 1.0
            params = {"top_p": 0.95}
            reasoning_policy = "disabled"
        image_part = {
            "type": "image",
            "media_type": mime,
            "data": base64.b64encode(image_data).decode(),
        }
        text_part = {"type": "text", "text": ask}
        user_content = [image_part, text_part]
        if model == MINIMAX_M3_ENGINE:
            # OpenRouter's MiniMax guide recommends prompt text before
            # images. Keep that ordering local to M3; existing VLM OCR
            # engines retain their established image-first contract.
            user_content = [text_part, image_part]
        # Routed through StructuredCompleter --
        # validation + repair is the completer's job now (jsonschema);
        # `repair_attempts=1` per the caller defaults table.
        req = StructuredRequest(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise OCR engine. Transcribe ALL text "
                    "in the image exactly as written — every word, "
                    "number, and symbol, in reading order. Do not "
                    "summarize, translate, or describe.",
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
            schema=schema,
            repair_attempts=1,
            temperature=temperature,
            params=params,
            reasoning_policy=reasoning_policy,
        )
        try:
            result = await StructuredCompleter(router).complete(
                req, recipe_version=recipe_version
            )
        except Exception as exc:
            page_wire_calls = list(getattr(exc, "wire_calls", []) or [])
            if page_wire_calls:
                accumulate_vlm_response_usage(
                    usage, _sum_response(model, page_wire_calls)
                )
                usage["input_pages"] += 1
            if wire_history:
                known = {id(call) for call in wire_history}
                exc.wire_calls = [
                    *wire_history,
                    *(call for call in page_wire_calls if id(call) not in known),
                ]
            raise
        resp = result.response
        accumulate_vlm_response_usage(usage, resp)
        usage["input_pages"] += 1
        wire_history.extend(result.wire_calls)
        out.append(
            {"text": (result.data or {}).get("text", ""), "blocks": []}
        )  # VLMs return no geometry
    return out


def accumulate_vlm_response_usage(usage: dict, resp: Any) -> None:
    """Retain one page's summed wire accounting, including failed repairs."""

    usage["calls"] += 1
    usage["requests"] = usage.get("requests", 0) + int(
        not getattr(resp, "cached", False)
    )
    usage["in"] += resp.tokens_in or 0
    usage["out"] += resp.tokens_out or 0
    # resp.cost None = unpriced model; one unknown call makes the row's total
    # unknown (None), never silently $0.
    if getattr(resp, "cached", False):
        pass
    elif resp.cost is None:
        usage["cost"] = None
    elif usage["cost"] is not None:
        usage["cost"] += resp.cost
    # Match cost's poison-to-None discipline for measured wire duration.
    current_duration_ms = usage.get("duration_ms", 0)
    if getattr(resp, "cached", False) or resp.duration_ms is None:
        usage["duration_ms"] = None
    elif current_duration_ms is not None:
        usage["duration_ms"] = current_duration_ms + resp.duration_ms
