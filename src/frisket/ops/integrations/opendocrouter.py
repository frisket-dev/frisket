"""OpenDocRouter's document API, shared by conversion and OCR.

Official wire contract: https://www.opendocrouter.ai/docs.md (2026-10-08).
No billable submission is automatically retried. Async jobs require encrypted
24-hour result storage; delete only after validated retrieval or cancellation.
"""

from __future__ import annotations

import asyncio
import base64
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from frisket.opendocrouter_catalog import find_known_model, find_model
from frisket.ai.models.metadata import ModelCallMeta, provider_cost_value
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.ops.netguard import safe_request, EgressRefused, ResponseTooLarge

BASE_URL = "https://www.opendocrouter.ai/v1"
MAX_BYTES = 50 * 1024 * 1024
INLINE_BYTES = 2 * 1024 * 1024
POLL_SECONDS = 12.0
READ_RETRY_SECONDS = 2.0
TIMEOUT_SECONDS = 3600.0
TERMINAL_STATUSES = {"completed", "partial", "failed", "expired", "rejected"}


def input_details(path: Path) -> tuple[str, int]:
    """Identify supported bytes before any upload; a filename isn't authority."""
    if path.stat().st_size > MAX_BYTES:
        raise HostedEngineError(
            "bad_request", "OpenDocRouter accepts files up to 50 MB."
        )
    with path.open("rb") as source:
        header = source.read(1024)
    if header.startswith(b"%PDF-"):
        try:
            from pypdf import PdfReader

            with path.open("rb") as source:
                pages = len(PdfReader(source).pages)
        except Exception as exc:
            raise HostedEngineError(
                "bad_request", "This PDF could not be read."
            ) from exc
        if not 1 <= pages <= 500:
            raise HostedEngineError(
                "bad_request", "OpenDocRouter accepts PDFs with 1–500 pages."
            )
        return "application/pdf", pages
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", 1
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", 1
    raise HostedEngineError(
        "bad_request",
        "OpenDocRouter accepts PDF, PNG and JPEG files. Choose another engine for this file.",
    )


async def _request_response(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    check_cancel: Callable[[], None] | None = None,
    **kwargs,
) -> httpx.Response:
    """Retry only safe reads, at most twice; never repeat a paid submission."""
    for attempt in range(3):
        if check_cancel:
            check_cancel()
        try:
            response = await client.request(
                method, url, follow_redirects=False, **kwargs
            )
        except httpx.HTTPError:
            if method != "GET" or attempt == 2:
                raise
            response = None
        if response is not None and (
            method != "GET"
            or attempt == 2
            or response.status_code not in {408, 429, 500, 502, 503, 504}
        ):
            return response
        delay = READ_RETRY_SECONDS * (2**attempt)
        if response is not None:
            try:
                retry_after = float(response.headers.get("Retry-After", "0"))
            except ValueError:
                retry_after = 0
            # Long provider waits belong to a later attempt, not this read loop.
            if not math.isfinite(retry_after) or retry_after > 30:
                return response
            delay = max(delay, retry_after)
        await asyncio.sleep(delay)
    raise AssertionError("request attempts exhausted")


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    billable: bool = False,
    **kwargs,
) -> dict:
    try:
        response = await _request_response(client, method, url, **kwargs)
    except httpx.HTTPError as error:
        # URLs may contain signed credentials; provider text may echo input.
        raise HostedEngineError(
            "transport",
            "Could not reach OpenDocRouter. Check provider usage before retrying a submitted request.",
            post_egress_ambiguous=billable
            and not isinstance(
                error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
            ),
        ) from None
    if billable and (response.status_code >= 500 or response.is_redirect):
        # Only documented pre-execution refusals establish that no work ran.
        try:
            detail = response.json().get("error", {})
        except (ValueError, AttributeError):
            detail = {}
        refused = (
            response.status_code == 503
            and isinstance(detail, dict)
            and detail.get("code") in {"at_capacity", "model_starting"}
        )
        if not refused:
            raise HostedEngineError(
                "transport",
                "OpenDocRouter did not return a conclusive result; check provider usage before retrying.",
                post_egress_ambiguous=True,
            )
    if response.is_error or response.is_redirect:
        code, message = {
            401: ("auth", "Check your OpenDocRouter API key."),
            402: ("quota", "Your OpenDocRouter account needs more credits."),
            403: ("permission", "Your OpenDocRouter account cannot run this request."),
            413: ("bad_request", "The document exceeds OpenDocRouter's size limit."),
            415: ("bad_request", "OpenDocRouter could not recognize this file type."),
            422: ("bad_request", "OpenDocRouter could not read this document."),
            429: ("quota", "OpenDocRouter is busy. Try again later."),
        }.get(
            response.status_code,
            ("http", f"OpenDocRouter returned HTTP {response.status_code}."),
        )
        raise HostedEngineError(code, message)
    if method == "PUT" or method == "DELETE":
        return {}
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HostedEngineError(
            "http",
            "OpenDocRouter returned an unreadable response.",
            post_egress_ambiguous=billable,
        )
    return body


async def _await_completed(task: asyncio.Task) -> tuple[Any, bool]:
    """Join an owned provider task even when the runner cancels repeatedly."""
    interrupted = False
    while True:
        try:
            return await asyncio.shield(task), interrupted
        except asyncio.CancelledError:
            interrupted = True
            if task.done():
                return task.result(), interrupted


def _job_id(body: dict) -> str:
    try:
        return str(UUID(body["id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HostedEngineError(
            "http", "OpenDocRouter did not return a valid request ID."
        ) from None


def _accounting(
    body: dict, engine: str, capability: str, credential_source: str, *, fact_id: str
) -> dict:
    job_id = _job_id(body) if body else None
    fact = ModelCallMeta.provider_call(
        capability=capability,
        engine=engine,
        provider="opendocrouter",
        provider_kind="platform_api",
        model_ids=[engine.removeprefix("opendocrouter/")],
        credential_source=credential_source,
        provider_reported_cost_usd=None,
        provider_cost_usd=None,
        cost_source="unknown",
        units={"requests": 1},
        request_id=job_id,
        duration_ms=None,
    ).as_dict()
    fact["id"] = fact_id
    return {"tokens_in": None, "tokens_out": None, "cost": None, "model_calls": [fact]}


def _enrich(accounting: dict, body: dict, started: float) -> None:
    fact = accounting["model_calls"][0]
    settled = body.get("status") in TERMINAL_STATUSES
    cost = provider_cost_value(body.get("charge_usd")) if settled else None
    # Expanded/paginated output may omit meters already learned from status.
    if cost is not None:
        accounting["cost"] = fact["provider_cost_usd"] = fact[
            "provider_reported_cost_usd"
        ] = cost
        fact["cost_source"] = "provider_reported"
    fact["duration_ms"] = int((time.monotonic() - started) * 1000) if settled else None
    units = fact["units"]
    if type(body.get("page_count")) is int:
        units["pages"] = body["page_count"]
    for name in ("model_version", "price_version"):
        if isinstance(body.get(name), str):
            units[name] = body[name]
    usage = body.get("usage") or {}
    for name, key in (("input_tokens", "tokens_in"), ("output_tokens", "tokens_out")):
        value = usage.get(name)
        if type(value) is int and value >= 0:
            accounting[key] = units[name] = value


async def _read_pages(
    body: dict, count: int, read: Callable[..., Awaitable[dict]]
) -> list[dict]:
    pages = []
    cursor = None
    while True:
        batch = body.get("pages") or []
        if not isinstance(batch, list) or any(not isinstance(p, dict) for p in batch):
            raise HostedEngineError("http", "OpenDocRouter returned invalid page data.")
        pages.extend(batch)
        if len(pages) > count:
            raise HostedEngineError("http", "OpenDocRouter returned too many pages.")
        if not body.get("has_more"):
            break
        next_cursor = body.get("next_cursor")
        if (
            type(next_cursor) is not int
            or next_cursor < 0
            or (cursor is not None and next_cursor <= cursor)
        ):
            raise HostedEngineError(
                "http", "OpenDocRouter returned invalid page pagination."
            )
        cursor = next_cursor
        body = await read(expand=True, cursor=cursor)
    if (
        body.get("status") != "completed"
        or len(pages) != count
        or any(p.get("status") != "ok" for p in pages)
    ):
        failed = [str(p.get("page")) for p in pages if p.get("status") != "ok"]
        suffix = f" Failed pages: {', '.join(failed[:20])}." if failed else ""
        raise HostedEngineError(
            "http",
            "OpenDocRouter could not convert every page. Successful pages may still be charged."
            + suffix,
        )
    if sorted(p.get("page") for p in pages if type(p.get("page")) is int) != list(
        range(1, count + 1)
    ) or any(not isinstance(p.get("markdown"), str) for p in pages):
        raise HostedEngineError("http", "OpenDocRouter returned incomplete page text.")
    return sorted(pages, key=lambda p: p["page"])


async def parse_document(
    client: httpx.AsyncClient,
    *,
    path: Path,
    engine: str,
    api_key: str,
    capability: str,
    credential_source: str,
    layout: bool = False,
    should_cancel: Callable[[], bool] | None = None,
    on_accounting: Callable[[dict], None] | None = None,
) -> tuple[list[dict], dict]:
    mime, count = input_details(path)
    headers = {"Authorization": f"Bearer {api_key}"}
    model = find_model(engine)
    if model is None:
        if find_known_model(engine) is not None:
            raise HostedEngineError(
                "bad_request",
                "This OpenDocRouter model is no longer offered. Choose a current model before running this action.",
            )
        raise HostedEngineError(
            "bad_request", "This OpenDocRouter model is not in the current catalog."
        )
    asynchronous = count > model.max_sync_pages
    started = time.monotonic()
    accounting = None
    job_url = None
    delete_results = False
    remote_job = False
    interrupted = False
    pending_error = None
    body = {}
    fact_id = f"opendocrouter_{uuid4().hex}"

    def check_cancel():
        if should_cancel and should_cancel():
            raise HostedEngineError(
                "cancelled", "OpenDocRouter processing was stopped."
            )
        if time.monotonic() - started > TIMEOUT_SECONDS:
            raise HostedEngineError("timeout", "OpenDocRouter processing timed out.")

    def notify_accounting():
        if on_accounting:
            try:
                on_accounting(accounting)
            except Exception as exc:
                raise HostedEngineError(
                    "accounting",
                    "OpenDocRouter processed the request, but its accounting could not be saved. Check provider usage before retrying.",
                ) from exc

    def record(body):
        _enrich(accounting, body, started)
        notify_accounting()

    async def read(*, expand=False, cursor=None):
        params = {}
        if expand:
            params["expand"] = "markdown,layout" if layout else "markdown"
        if cursor is not None:
            params["cursor"] = cursor
        result = await _request(
            client,
            "GET",
            job_url,
            headers=headers,
            timeout=30,
            params=params,
            check_cancel=check_cancel,
        )
        record(result)
        return result

    check_cancel()
    if path.stat().st_size <= INLINE_BYTES:
        document = {
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "mime_type": mime,
        }
    else:
        upload = await _request(
            client, "POST", f"{BASE_URL}/uploads", headers=headers, timeout=30
        )
        try:
            upload_id = str(UUID(upload["upload_id"]))
            url = upload["upload_url"]
            if not isinstance(url, str):
                raise ValueError("invalid URL")
            parsed = urlsplit(url)
            port = parsed.port
            maximum = upload["max_bytes"]
            if type(maximum) is not int or maximum <= 0:
                raise ValueError("invalid size")
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HostedEngineError(
                "http", "OpenDocRouter returned invalid upload details."
            ) from None
        # Presigned storage URLs originate at the fixed, authenticated provider.
        # Never send our provider credential or client defaults to that origin.
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in (None, 443)
        ):
            raise HostedEngineError(
                "http", "OpenDocRouter returned an invalid upload destination."
            )
        if path.stat().st_size > maximum:
            raise HostedEngineError(
                "bad_request", "The file exceeds OpenDocRouter's upload limit."
            )
        check_cancel()
        try:
            response = await safe_request(
                client,
                "PUT",
                url,
                content=path.read_bytes(),
                headers={"Content-Type": mime},
                timeout=120,
                max_bytes=65536,
                follow_redirects=False,
            )
        except (httpx.HTTPError, EgressRefused, ResponseTooLarge, TimeoutError):
            raise HostedEngineError(
                "transport", "Could not upload the document to OpenDocRouter."
            ) from None
        if not 200 <= response.status_code < 300:
            raise HostedEngineError("http", "OpenDocRouter's document upload failed.")
        document = {"upload_id": upload_id}
    check_cancel()
    try:
        submit = asyncio.create_task(
            _request(
                client,
                "POST",
                f"{BASE_URL}/parse",
                headers=headers,
                timeout=600,
                billable=True,
                json={
                    "model": engine.removeprefix("opendocrouter/"),
                    "document": document,
                    "layout": layout,
                    "mode": "async" if asynchronous else "sync",
                    "cache": asynchronous,
                },
            )
        )
        # A submitted request can still incur a charge. Repeated runner
        # cancellation must not detach or cancel the provider response drain.
        body, interrupted = await _await_completed(submit)
        try:
            accounting = _accounting(
                body, engine, capability, credential_source, fact_id=fact_id
            )
        except HostedEngineError as error:
            error.post_egress_ambiguous = True
            raise
        job_url = f"{BASE_URL}/parse/{accounting['model_calls'][0]['request_id']}"
        remote_job = asynchronous or body.get("status") not in TERMINAL_STATUSES
        # Persist the accepted fact after enriching all meters already present
        # on the submission response. Later polls update the same identity.
        record(body)
        if interrupted:
            raise HostedEngineError(
                "cancelled", "OpenDocRouter processing was stopped."
            )
        check_cancel()
        while body.get("status") not in TERMINAL_STATUSES:
            check_cancel()
            await asyncio.sleep(POLL_SECONDS)
            check_cancel()
            body = await read()
        if remote_job:
            body = await read(expand=True)
        pages = await _read_pages(body, count, read)
        delete_results = True
        return pages, accounting
    except (HostedEngineError, asyncio.CancelledError) as error:
        exc = (
            error
            if isinstance(error, HostedEngineError)
            else HostedEngineError("cancelled", "OpenDocRouter processing was stopped.")
        )
        if isinstance(error, asyncio.CancelledError) and accounting is None:
            exc.post_egress_ambiguous = True
        if exc.post_egress_ambiguous and accounting is None:
            accounting = _accounting(
                {}, engine, capability, credential_source, fact_id=fact_id
            )
            try:
                notify_accounting()
            except HostedEngineError:
                # Preserve uncertainty even when the ledger is unavailable.
                pass
        exc.accounting = accounting
        exc.provider_job_accepted = accounting is not None and (
            exc.code == "accounting"
            or (job_url is not None and (remote_job or interrupted))
        )
        delete_results = exc.code == "cancelled"
        if remote_job and job_url and not delete_results:
            exc.message += " Results are retained by OpenDocRouter for up to 24 hours; check the existing job before submitting again."
        pending_error = exc
        raise exc
    finally:
        if remote_job and job_url and delete_results:
            cleanup_interrupted = False

            async def cleanup():
                await _request(client, "DELETE", job_url, headers=headers, timeout=10)
                if accounting and accounting["cost"] is None:
                    status = await _request(
                        client, "GET", job_url, headers=headers, timeout=10
                    )
                    record(status)

            try:
                cleanup_task = asyncio.create_task(cleanup())
                _, cleanup_interrupted = await _await_completed(cleanup_task)
            except HostedEngineError:
                if accounting:
                    accounting["model_calls"][0]["warnings"].append(
                        "Temporary OpenDocRouter results could not be deleted; the provider retains them for up to 24 hours."
                    )
            if cleanup_interrupted and pending_error is None:
                cancelled = HostedEngineError(
                    "cancelled", "OpenDocRouter processing was stopped."
                )
                cancelled.accounting = accounting
                cancelled.provider_job_accepted = accounting is not None
                raise cancelled
