"""Datalab hosted document APIs (datalab.to) — pay-per-call REMOTE engines for
media.ocr and media.to_markdown. They follow the hosted-engine shape in
integrations/deepl.py + google_translate.py: the same injectable-async-client
shape over ``OpContext.http`` so tests replay ``httpx.MockTransport`` and
never touch the network.

Datalab (github.com/datalab-to) is the vendor behind the Surya 2 and Chandra
models Frisket offers as local/team sidecar engines. This module adds Datalab's
paid hosted OCR REST API as a separate credential-gated option: no
local/sidecar compute, billed per page, key resolved via
``resolve_credential`` (env -> project secrets).

API SHAPE (observed against the vendor service and cross-checked against
documentation.datalab.to):

- Base URL: ``https://www.datalab.to/api/v1`` (documentation.datalab.to/
  llms.txt).
- Auth: ``X-API-Key: <key>`` header — no bearer scheme, no query param.
- POST ``/convert`` — document -> markdown/html/json/chunks (the "Marker"
  product). Multipart ``file`` + form fields (``output_format``, ``mode`` one
  of fast/balanced/accurate, ``max_pages``, ``word_bboxes``, ...). ALWAYS
  async: returns ``{success, error, request_id, request_check_url,
  versions}`` immediately (documentation.datalab.to/api-reference/
  convert-document.md).
- OCR MIGRATION (corrects the prior claim of "no replacement named"):
  Datalab's own migration guide (documentation.datalab.to/platform/
  migration) explicitly names ``/convert`` as the
  deprecated ``/ocr`` endpoint's replacement — "Use the Document Conversion
  endpoint instead, which includes OCR as part of its processing pipeline."
  This module now submits OCR requests to ``/convert`` with
  ``output_format="json", word_bboxes="true"`` (NOT the deprecated ``/ocr``
  route) and normalizes the returned block tree into frisket's common OCR
  page shape; see ``_ocr_page_from_json`` for the recorded shape and the
  bbox-rescaling this required.
- GET ``<request_check_url>`` — poll until ``status`` is terminal. Recorded
  live values: ``"processing"`` (pending) and ``"complete"`` (done); Datalab's
  docs additionally document ``"failed"`` for a job-level failure (not
  reproduced live — no input that reliably fails on hand). ANY OTHER status
  string (a documentation-undescribed value, a typo, something like
  "cancelled"/"expired") is treated as a FAILURE, not a silent success —
  Datalab has not documented a full enum, so failing closed on an unknown
  status is the only honest option: this used to
  return the body as if it were a successful terminal state, which let a
  response missing ``markdown``/``pages`` silently become an empty-but-
  "successful" result.
- A completed ``/convert`` (markdown mode) body carries ``markdown`` plus
  ``page_count``, ``total_cost``, ``cost_breakdown.final_cost_cents``. A
  completed ``/convert`` (json mode, the OCR path) body carries
  ``json.children``: a recursive block tree, one top-level "Page" block per
  submitted page, each with its own ``children`` of content blocks (e.g.
  "Text") carrying ``bbox``/``polygon`` (in DATALAB'S OWN INTERNAL RENDER
  RESOLUTION, NOT the input image's native pixel size — recorded live: a
  300x100 input PNG produced a Page-level ``bbox`` of
  ``[0, 0, 2352, 784]``, a ~7.84x internal upscale) and ``metadata.
  confidence``; leaf blocks' ``html`` field carries per-word
  ``<span data-bbox="x0 y0 x1 y1" data-confidence="c">word</span>``
  fragments when ``word_bboxes=true``. ``_ocr_page_from_json`` rescales
  every extracted bbox back into the CALLER-supplied source-image pixel
  space (the convention every other OCR engine here uses — rapidocr/
  tesseract/sidecar/the old /ocr endpoint all report bboxes in the same
  frame as the input image) using the Page block's own bbox as the
  reference frame; without a supplied ``page_size`` the raw (un-rescaled,
  Datalab-internal-resolution) coordinates are returned rather than
  fabricating a scale.
- Pricing: a recorded 1-page
  ``mode=fast`` ``/convert`` call (both markdown and json output_format)
  billed ``cost_breakdown.final_cost_cents == 1.0`` — one cent/page on this
  key's plan. Datalab's public pricing (datalab.to/pricing, documentation.
  datalab.to/platform/billing.md) states rates vary by processor/mode/plan
  and are resolved per-team server-side, so this is a representative
  default, not a contractual rate (see external_pricing.py's env-var
  overrides, used as the fallback estimate when a
  response is missing cost fields).
- Errors observed/documented: 401 with ``{"detail": "Invalid API key..."}``
  — a DIFFERENT shape than the success envelope (captured in
  tests/fixtures/datalab/auth_failure.json); 422 validation
  errors in FastAPI's ``{"detail": [{"loc","msg","type"}, ...]}`` shape; the
  docs additionally mention 402 (insufficient credits), 429 (rate/page-
  concurrency limit — "success: false" + an error message when >5,000 pages
  are in flight), and 423 (on-prem license validation) which this client
  classifies as quota/quota(retryable)/auth respectively; anything >=500 is
  a transient ``http`` error.
- Supported input types: PDF, Word (doc/docx), PowerPoint (ppt/pptx), PNG,
  JPG, WebP (documentation.datalab.to/api-reference/convert-document.md).
  NOT raw HTML or plain text, even though frisket's shared to_markdown
  input descriptor advertises text/html columns for the local
  ``markitdown``/``trafilatura_html`` engines — ``sdk/ops/to_markdown.py``
  gates the ``datalab`` engine to this list BEFORE submission.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_module
import re
import time
from typing import Any, Awaitable, Callable

import httpx

from frisket.ai.models.metadata import ModelCallMeta, duration_ms_value
from frisket.ops.integrations.hosted_error import HostedEngineError, call_hosted_json

DATALAB_BASE_URL = "https://www.datalab.to/api/v1"
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_POLL_TIMEOUT_S = 180.0

# The "processing"/"complete" pair is ground-truthed; "failed" and the
# upper-case variants are Datalab's documented-but-unobserved values,
# normalized here so a documentation drift in casing doesn't strand a job as
# permanently pending. Anything OUTSIDE both sets fails closed rather than
# being treated as a successful terminal state.
_PENDING_STATUSES = {"processing", "in_progress", "queued", "pending"}
_SUCCESS_STATUSES = {"complete", "completed"}
_FAILURE_STATUSES = {"failed"}

_ACCEPTED_WARNING = (
    "Datalab accepted the provider job, but its final cost and page meter are "
    "not yet available."
)


# This is the SAME class object as ``TranslateEngineError``, not a subclass,
# so exception handlers catching both (``except (TranslateEngineError,
# DatalabEngineError)``) keep working unchanged: ``code`` is the taxonomy
# bucket (auth / quota / bad_request / http / transport / cancelled),
# ``message`` is what the row shows, ``retryable`` marks transient failures.
DatalabEngineError = HostedEngineError


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _validation_detail(body: Any) -> str:
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            messages = [
                str(item.get("msg"))
                for item in detail
                if isinstance(item, dict) and item.get("msg")
            ]
            if messages:
                return "; ".join(messages)
    return ""


def _raise_for_error_status(status: int, body: Any) -> None:
    detail = _validation_detail(body)
    suffix = f": {detail}" if detail else ""
    if status == 401:
        raise DatalabEngineError(
            code="auth",
            message=(
                f"Datalab rejected the API key (401){suffix}. Check "
                "DATALAB_API_KEY in Settings → Secrets."
            ),
        )
    if status == 402:
        raise DatalabEngineError(
            code="quota",
            message=f"Datalab reports insufficient credits (402){suffix}.",
        )
    if status == 403:
        raise DatalabEngineError(
            code="auth",
            message=f"Datalab denied the request (403){suffix}; check the API key's permissions.",
        )
    if status == 422:
        raise DatalabEngineError(
            code="bad_request",
            message=f"Datalab rejected the request (422){suffix}.",
        )
    if status == 423:
        raise DatalabEngineError(
            code="auth",
            message=f"Datalab license validation failed (423){suffix}.",
        )
    if status == 429:
        raise DatalabEngineError(
            code="quota",
            message=f"Datalab rate/concurrency limit reached (429){suffix}; retry shortly.",
            retryable=True,
        )
    if status >= 500:
        raise DatalabEngineError(
            code="http",
            message=f"Datalab service error ({status}){suffix}; try again shortly.",
            retryable=True,
        )
    raise DatalabEngineError(
        code="http", message=f"Datalab request failed ({status}){suffix}."
    )


async def _post(
    http: httpx.AsyncClient,
    api_key: str,
    path: str,
    *,
    file_bytes: bytes,
    filename: str,
    mime: str,
    data: dict[str, str],
) -> dict[str, Any]:
    response, body = await call_hosted_json(
        lambda: http.post(
            f"{DATALAB_BASE_URL}{path}",
            headers=_headers(api_key),
            files={"file": (filename, file_bytes, mime)},
            data=data,
        ),
        error_cls=DatalabEngineError,
        provider_name="Datalab",
    )
    if response.status_code >= 400:
        _raise_for_error_status(response.status_code, body)
    if not isinstance(body, dict):
        raise DatalabEngineError(
            code="http", message="Datalab returned a non-JSON response."
        )
    # A 200 with success=false (e.g. the page-concurrency-limit case Datalab's
    # billing docs describe) is a submission-time rejection, not a job to poll.
    if body.get("success") is False:
        raise DatalabEngineError(
            code="bad_request",
            message=f"Datalab rejected the request: {body.get('error') or 'unknown error'}.",
        )
    check_url = body.get("request_check_url")
    if not isinstance(check_url, str) or not check_url:
        raise DatalabEngineError(
            code="http",
            message="Datalab's response was missing request_check_url.",
        )
    return {"request_id": body.get("request_id"), "request_check_url": check_url}


async def _poll(
    http: httpx.AsyncClient,
    api_key: str,
    check_url: str,
    *,
    poll_interval: float,
    poll_timeout: float,
    should_cancel: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Poll ``check_url`` until a terminal status.

    The deadline is WALL-CLOCK (``time.monotonic``),
    not accumulated-sleep — a slow poll GET (network latency, Datalab-side
    queueing) no longer silently extends the effective timeout past
    ``poll_timeout``. ``should_cancel`` (the same cooperative-cancellation
    callable MapRunner threads through ``OpContext.extras['cancelled']``,
    checked between ROWS at runner/map_runner.py's ``one_row``) is checked
    each iteration so a run-cancel during a long poll stops promptly instead
    of only being noticed after the whole row (which could be a 180s poll
    loop) finishes.

    ``clock``/``sleep`` are injected time seams: plain callables
    defaulting to real time, so tests pin the wall-clock-deadline claim on a
    manual clock while production callers stay unchanged.
    """
    deadline = clock() + poll_timeout
    while True:
        if should_cancel is not None and should_cancel():
            raise DatalabEngineError(
                code="cancelled",
                message="Datalab polling stopped: the run was cancelled.",
            )
        response, body = await call_hosted_json(
            lambda: http.get(check_url, headers=_headers(api_key)),
            error_cls=DatalabEngineError,
            provider_name="Datalab",
        )
        if response.status_code >= 400:
            _raise_for_error_status(response.status_code, body)
        if not isinstance(body, dict):
            raise DatalabEngineError(
                code="http", message="Datalab poll response was not JSON."
            )
        status = str(body.get("status") or "").lower()
        if body.get("success") is False or status in _FAILURE_STATUSES:
            raise DatalabEngineError(
                code="bad_request",
                message=f"Datalab processing failed: {body.get('error') or 'unknown error'}.",
            )
        if status in _SUCCESS_STATUSES:
            return body
        if status not in _PENDING_STATUSES:
            # A status OUTSIDE every documented value (not success, not the
            # one documented failure state, not pending — a documentation
            # drift, a renamed/new terminal state, a typo) fails CLOSED rather
            # than being treated as a successful body, which would let a
            # response with no markdown/pages become a silent empty
            # "success".
            raise DatalabEngineError(
                code="bad_request",
                message=f"Datalab returned an unrecognized job status {status!r}.",
            )
        if clock() >= deadline:
            raise DatalabEngineError(
                code="http",
                message=(
                    f"Datalab did not finish within {poll_timeout:.0f}s; the "
                    "job may still complete server-side."
                ),
                retryable=True,
            )
        if should_cancel is not None and should_cancel():
            raise DatalabEngineError(
                code="cancelled",
                message="Datalab polling stopped: the run was cancelled.",
            )
        await sleep(min(poll_interval, max(0.0, deadline - clock())))


def _extract_cost_usd(body: dict[str, Any]) -> float | None:
    """Real spend reported by Datalab for one completed job, in USD.

    This used to be discarded entirely (sdk/ops/ocr.py and
    sdk/ops/to_markdown.py only pulled ``pages``/``markdown`` out of the
    response), so every Datalab run recorded as free.
    ``cost_breakdown.final_cost_cents``
    is the ground-truthed field (a recorded 1-page
    mode=fast call billed ``1.0`` — one cent); ``total_cost`` is a fallback
    when ``cost_breakdown`` is absent, ASSUMED to be the same cents unit
    (both fields carried the identical value ``1`` in every recording on
    hand — not independently confirmed against a >1-cent charge). Returns
    None when neither field is present so the caller can fall back to the
    external_pricing per-page estimate and mark the result as estimated
    rather than fabricating a number.
    """
    cost_breakdown = body.get("cost_breakdown")
    if isinstance(cost_breakdown, dict):
        cents = cost_breakdown.get("final_cost_cents")
        if isinstance(cents, (int, float)):
            return round(float(cents) / 100.0, 6)
    total_cost = body.get("total_cost")
    if isinstance(total_cost, (int, float)):
        return round(float(total_cost) / 100.0, 6)
    return None


def _accepted_submission_accounting(
    submitted: dict[str, Any],
    *,
    capability: str,
    credential_source: str,
) -> dict[str, Any]:
    """Unknown-cost fact minted at Datalab's acceptance boundary.

    A successful submit hands the work to a separately running provider job;
    cancellation or timeout after that point cannot turn it back into "no
    call".  The check URL is the stable provider-job identity even when the
    optional request id is absent, so it also makes the fact idempotent when a
    recipe persists it once at acceptance and again at its row-error boundary.
    """
    check_url = str(submitted["request_check_url"])
    fact = ModelCallMeta.provider_call(
        capability=capability,
        engine="datalab",
        provider="datalab",
        provider_kind="platform_api",
        model_ids=["datalab"],
        credential_source=credential_source,
        provider_reported_cost_usd=None,
        provider_cost_usd=None,
        cost_source="unknown",
        units={"requests": 1},
        request_id=(
            str(submitted["request_id"])
            if submitted.get("request_id") is not None
            else None
        ),
        warnings=[_ACCEPTED_WARNING],
        duration_ms=None,
    ).as_dict()
    fact["id"] = "datalab_accepted_" + hashlib.sha256(check_url.encode()).hexdigest()
    return {
        "tokens_in": None,
        "tokens_out": None,
        "cost": None,
        "model_calls": [fact],
    }


def _notify_accepted(
    accounting: dict[str, Any],
    callback: Callable[[dict[str, Any]], None] | None,
) -> None:
    if callback is None:
        return
    try:
        callback(accounting)
    except Exception as exc:
        # The provider already owns the job.  Continuing to poll and return a
        # successful row would erase the accounting-store failure; stop with
        # the same unknown fact attached so the outer boundary can still make
        # the ambiguity visible.
        raise DatalabEngineError(
            code="accounting",
            message=(
                "Datalab accepted the job, but its provider accounting could "
                "not be recorded; retrying may submit another paid job."
            ),
            accounting=accounting,
            provider_job_accepted=True,
        ) from exc


def _attach_accepted_accounting(
    error: DatalabEngineError,
    accounting: dict[str, Any],
) -> None:
    if error.accounting is None:
        error.accounting = accounting
    error.provider_job_accepted = True


def _complete_submission_accounting(
    accounting: dict[str, Any],
    *,
    cost_usd: float | None,
    pages: int | None,
    duration_ms: int | None,
) -> None:
    """Monotonically enrich the accepted fact after a completed poll.

    The dict is deliberately mutated in place: a recipe's acceptance callback
    holds this same envelope after durably writing its unknown form, then
    returns the enriched form through MapRunner so the fact writer can
    reconcile the one call rather than minting a second call. ``duration_ms``
    is the caller's already-bracketed (submit -> completed poll) elapsed
    time, run through :func:`duration_ms_value` so this write-side domain and
    the store's agree on what counts as measured.
    """
    [fact] = accounting["model_calls"]
    units = dict(fact.get("units") or {})
    if isinstance(pages, int) and pages > 0:
        units["pages"] = pages
    fact["units"] = units
    fact["provider_reported_cost_usd"] = cost_usd
    fact["provider_cost_usd"] = cost_usd
    fact["cost_source"] = "provider_reported" if cost_usd is not None else "unknown"
    fact["warnings"] = (
        []
        if cost_usd is not None
        else ["Datalab completed the provider job without reporting its final cost."]
    )
    fact["duration_ms"] = duration_ms_value(duration_ms)
    accounting["cost"] = cost_usd


async def datalab_convert(
    http: httpx.AsyncClient,
    api_key: str,
    file_bytes: bytes,
    filename: str,
    mime: str,
    *,
    mode: str = "fast",
    output_format: str = "markdown",
    max_pages: int | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_S,
    should_cancel: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    credential_source: str = "none",
    on_accepted: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], float | None]:
    """POST ``/convert`` (Datalab's Marker product): a whole document ->
    markdown (default) or html/json/chunks, submitted once (Datalab handles
    multi-page PDFs natively — no client-side rasterization needed, unlike
    the OCR path). Returns ``(completed_body, cost_usd)`` — the poll body
    verbatim (``markdown``, ``page_count``, ``total_cost``,
    ``cost_breakdown``, ...) alongside the extracted real spend (None if
    Datalab omitted cost fields — see ``_extract_cost_usd``). For
    ``output_format="markdown"`` (the only mode this function's caller uses
    today), a completed body with a missing or blank ``markdown`` field is
    itself a failure and is never silently replaced with an empty string.
    Raises :class:`DatalabEngineError` on any submission, transport,
    cancellation, or job-level failure.
    """
    data: dict[str, str] = {"output_format": output_format, "mode": mode}
    if max_pages is not None:
        data["max_pages"] = str(max_pages)
    t0 = clock()
    submitted = await _post(
        http,
        api_key,
        "/convert",
        file_bytes=file_bytes,
        filename=filename,
        mime=mime,
        data=data,
    )
    accounting = _accepted_submission_accounting(
        submitted,
        capability="document.convert",
        credential_source=credential_source,
    )
    _notify_accepted(accounting, on_accepted)
    try:
        body = await _poll(
            http,
            api_key,
            submitted["request_check_url"],
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            should_cancel=should_cancel,
            clock=clock,
            sleep=sleep,
        )
        cost_usd = _extract_cost_usd(body)
        raw_pages = body.get("page_count")
        pages = raw_pages if isinstance(raw_pages, int) and raw_pages > 0 else None
        _complete_submission_accounting(
            accounting,
            cost_usd=cost_usd,
            duration_ms=round((clock() - t0) * 1000),
            pages=pages,
        )
        if output_format == "markdown":
            markdown = body.get("markdown")
            if not isinstance(markdown, str) or not markdown.strip():
                raise DatalabEngineError(
                    code="bad_request",
                    message="Datalab reported success but returned no markdown content.",
                )
        return body, cost_usd
    except DatalabEngineError as exc:
        _attach_accepted_accounting(exc, accounting)
        raise


# ---------------------------------------------------------------------------
# OCR via /convert(output_format=json), the documented replacement for the
# deprecated /ocr endpoint

_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    return " ".join(html_module.unescape(_TAG_RE.sub(" ", raw)).split())


def _leaf_blocks(node: dict[str, Any]) -> list[dict[str, Any]]:
    """DFS to the leaf content blocks of Datalab's JSON block tree.

    A "Page" block's own ``html`` duplicates its single "Text" child's
    ``html`` verbatim — collecting every node
    with non-empty ``html`` would double every line. Recursing only into
    children (and taking childless nodes as the unit of text) avoids that
    without assuming a fixed block_type vocabulary (Datalab's schema is not
    fully documented; SectionHeader/ListItem/Table/etc block types are
    handled the same way as Text — whatever bbox/html/confidence they carry
    is used generically)."""
    children = node.get("children")
    if isinstance(children, list) and children:
        out: list[dict[str, Any]] = []
        for child in children:
            if isinstance(child, dict):
                out.extend(_leaf_blocks(child))
        return out
    return [node]


def _page_scale(
    page_node: dict[str, Any], page_size: tuple[int, int] | None
) -> tuple[float, float] | None:
    """(scale_x, scale_y) to project Datalab's internal render-resolution
    bbox coordinates back into ``page_size`` (the ACTUAL source image's
    pixel dimensions): a 300x100 input PNG
    produced a Page-level bbox of [0, 0, 2352, 784] (Datalab renders at its
    own internal resolution, not the input's native size). Without a
    supplied ``page_size`` (or a degenerate/missing Page bbox), returns None
    so the caller passes coordinates through unscaled rather than guessing."""
    if page_size is None:
        return None
    bbox = page_node.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    try:
        render_w = float(bbox[2]) - float(bbox[0])
        render_h = float(bbox[3]) - float(bbox[1])
    except (TypeError, ValueError):
        return None
    if render_w <= 0 or render_h <= 0:
        return None
    width, height = page_size
    if width <= 0 or height <= 0:
        return None
    return width / render_w, height / render_h


def _ocr_page_from_json(
    page_node: dict[str, Any], page_size: tuple[int, int] | None
) -> dict[str, Any]:
    scale = _page_scale(page_node, page_size)
    texts: list[str] = []
    blocks: list[dict[str, Any]] = []
    for leaf in _leaf_blocks(page_node):
        text = _html_to_text(leaf.get("html"))
        if not text:
            continue
        texts.append(text)
        block: dict[str, Any] = {"text": text}
        bbox = leaf.get("bbox")
        if isinstance(bbox, list) and len(bbox) == 4:
            try:
                x0, y0, x1, y1 = (float(v) for v in bbox)
                if scale is not None:
                    sx, sy = scale
                    x0, x1 = x0 * sx, x1 * sx
                    y0, y1 = y0 * sy, y1 * sy
                block["bbox"] = [
                    [round(x0), round(y0)],
                    [round(x1), round(y0)],
                    [round(x1), round(y1)],
                    [round(x0), round(y1)],
                ]
            except (TypeError, ValueError):
                pass
        confidence = (leaf.get("metadata") or {}).get("confidence")
        if isinstance(confidence, (int, float)):
            block["score"] = round(float(confidence), 4)
        blocks.append(block)
    return {"text": "\n".join(texts).strip(), "blocks": blocks}


async def datalab_ocr(
    http: httpx.AsyncClient,
    api_key: str,
    file_bytes: bytes,
    filename: str,
    mime: str,
    *,
    page_size: tuple[int, int] | None = None,
    max_pages: int | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_S,
    should_cancel: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    credential_source: str = "none",
    on_accepted: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], float | None]:
    """OCR via ``/convert(output_format="json", word_bboxes="true")`` — the
    documented replacement for the deprecated ``/ocr`` endpoint (Datalab's
    migration guide; see the module docstring). Returns
    ``([{"text", "blocks": [{"text", "bbox", "score"}]}, ...], cost_usd)``:
    one page dict per top-level "Page" block in Datalab's response (frisket's
    common OCR page shape, matching rapidocr/tesseract/sidecar), alongside
    the real spend (None if Datalab omitted cost fields).

    ``page_size`` should be the ACTUAL (width, height) in pixels of
    ``file_bytes`` — Datalab renders internally at its own resolution, so
    without this the returned bboxes are in Datalab's render space, not the
    source image's (see ``_page_scale``). Raises :class:`DatalabEngineError`
    on any submission, transport, cancellation, or job-level failure.
    """
    data: dict[str, str] = {
        "output_format": "json",
        "word_bboxes": "true",
        "mode": "fast",
    }
    if max_pages is not None:
        data["max_pages"] = str(max_pages)
    t0 = clock()
    submitted = await _post(
        http,
        api_key,
        "/convert",
        file_bytes=file_bytes,
        filename=filename,
        mime=mime,
        data=data,
    )
    accounting = _accepted_submission_accounting(
        submitted,
        capability="ocr",
        credential_source=credential_source,
    )
    _notify_accepted(accounting, on_accepted)
    try:
        body = await _poll(
            http,
            api_key,
            submitted["request_check_url"],
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            should_cancel=should_cancel,
            clock=clock,
        )
        cost_usd = _extract_cost_usd(body)
        json_result = body.get("json")
        page_nodes = (
            json_result.get("children") if isinstance(json_result, dict) else None
        )
        page_count = len(page_nodes) if isinstance(page_nodes, list) else None
        _complete_submission_accounting(
            accounting,
            cost_usd=cost_usd,
            pages=page_count,
            duration_ms=round((clock() - t0) * 1000),
        )
        if not isinstance(json_result, dict):
            raise DatalabEngineError(
                code="bad_request",
                message="Datalab reported success but returned no json block tree.",
            )
        if not isinstance(page_nodes, list) or not page_nodes:
            raise DatalabEngineError(
                code="bad_request",
                message="Datalab's json response had no page blocks.",
            )
        pages = [
            _ocr_page_from_json(page_node, page_size)
            for page_node in page_nodes
            if isinstance(page_node, dict)
        ]
        return pages, cost_usd
    except DatalabEngineError as exc:
        _attach_accepted_accounting(exc, accounting)
        raise
