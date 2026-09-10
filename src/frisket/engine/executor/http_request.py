"""Host-owned HTTP request execution: credentials, pacing, netguard and redaction."""

from __future__ import annotations
import asyncio
import json
import time
from typing import Any
import httpx
from pydantic import JsonValue
from frisket.actions.http_types import (
    HttpRequest,
    _pairs,
    _request_templates,
)
from frisket.actions.types import Row, RowError
from frisket.authoring.request_templates import (
    MissingSecret,
    render_request_field,
    scan_request_tokens,
)
from frisket.contracts.actions.schemas._validators import validate_positive_finite_rate
from frisket.credentials import resolve_credential
from frisket.ops.api_call_request import (
    CookieInjection,
    HeaderInjection,
    InvalidJsonBodyTemplate,
    build_request_kwargs,
    strict_json_loads,
)
from frisket.ops.base import OpContext
from frisket.ops.netguard import (
    EgressRefused,
    ResponseTooLarge,
    TooManyRedirects,
    safe_request,
)

_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_RATE_LIMIT_SLEEP_SLICE_SECONDS = 0.25


class HttpRequestCancelled(RowError):
    """Host-observed cooperative cancellation before egress."""


async def _pace_request(ctx: OpContext, value: Any) -> bool:
    """Apply one fixed-interval limiter shared by every row in this run.

    Returns True once this request may proceed, or False if a cooperative
    cancellation was observed while waiting for the shared bucket — the caller
    then skips egress and returns a normal row result. We must NOT raise
    ``asyncio.CancelledError`` here: on 3.11 it is a ``BaseException`` that would
    escape ``execute``'s ``except Exception`` and MapRunner's finalization, so a
    manual cancel would strand run/receipt settlement. (A *real* task
    cancellation still propagates naturally from ``asyncio.sleep``.)"""

    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    rate = validate_positive_finite_rate(value)

    run_state = ctx.extras.setdefault("run_state", {})
    limiter = run_state.setdefault(
        "api_call_rate_limiter",
        {"lock": asyncio.Lock(), "next_request_at": 0.0},
    )
    async with limiter["lock"]:
        cancelled = ctx.extras.get("cancelled")
        target = float(limiter["next_request_at"])
        while True:
            if callable(cancelled) and cancelled():
                return False
            delay = target - time.monotonic()
            if delay <= 0:
                break
            await asyncio.sleep(min(delay, _RATE_LIMIT_SLEEP_SLICE_SECONDS))
        limiter["next_request_at"] = time.monotonic() + (1.0 / rate)
    return True


class AdmittedHttpRequester:
    def __init__(self, ctx: OpContext):
        self._ctx = ctx
        self._closed = False

    async def aclose(self) -> None:
        # The router owns the shared client; only revoke this invocation's handle.
        self._closed = True

    def _check_active(self) -> None:
        if self._closed:
            raise RowError("request_failed", "request_failed: HTTP requester is closed")
        cancelled = self._ctx.extras.get("cancelled")
        if callable(cancelled) and cancelled():
            raise HttpRequestCancelled("cancelled", "cancelled: HTTP request")

    async def request_json(
        self, request: HttpRequest, row: Row
    ) -> dict[str, JsonValue]:
        self._check_active()
        try:
            request = HttpRequest.model_validate(request.model_dump(warnings=False))
        except (AttributeError, TypeError, ValueError):
            # Derived arguments may contain private row values. Pydantic's
            # diagnostic echoes input values, so never persist that exception.
            raise RowError(
                "invalid_params", "invalid_params: Invalid HTTP request arguments"
            ) from None
        spec = request.model_dump()
        row_values = dict(row.values)
        ctx = self._ctx
        timeout, max_requests_per_second = (
            request.timeout,
            request.max_requests_per_second,
        )
        method = str(spec.get("method") or "GET").upper()
        url_template = str(spec.get("url") or "")
        templates = _request_templates(spec)
        tokens = scan_request_tokens(templates)

        # A handler may derive another request from its admitted inputs. Recheck
        # the actual references before egress; an absent column is not a blank.
        missing_columns = [name for name in tokens.columns if name not in row_values]
        if missing_columns:
            return self._error("missing_column", method)

        # Resolve secrets before any egress; a missing one is a row error, not a
        # live request with a blank credential.
        secret_values: dict[str, str] = {}
        for secret_name in tokens.secrets:
            value = resolve_credential(ctx.project, secret_name)
            if value is None:
                return self._error("missing_secret", method)
            secret_values[secret_name] = value

        def render(template: str) -> str:
            return render_request_field(template, row_values, secret_values)

        try:
            kwargs = build_request_kwargs(
                {
                    "method": method,
                    "url": url_template,
                    "headers": _pairs(spec.get("headers")),
                    "query_params": _pairs(spec.get("query_params")),
                    "body_mode": spec.get("body_mode", "none"),
                    "body": spec.get("body", ""),
                    "form_body": _pairs(spec.get("form_body")),
                    "cookies": _pairs(spec.get("cookies")),
                    "content_type": spec.get("content_type"),
                },
                render,
            )
            if not await _pace_request(ctx, max_requests_per_second):
                # Cooperative cancel while waiting for the shared limiter: skip
                # egress and return a normal row result so MapRunner's
                # cancellation fence can finalize the run cleanly.
                return self._error("cancelled", method)
            self._check_active()
            resp = await safe_request(
                ctx.http,
                timeout=timeout,
                follow_redirects=bool(spec.get("follow_redirects", True)),
                max_bytes=_MAX_RESPONSE_BYTES,
                **kwargs,
            )
        except HttpRequestCancelled:
            raise
        except MissingSecret:
            return self._error("missing_secret", method)
        except EgressRefused:
            return self._error("blocked_url", method)
        except TooManyRedirects:
            return self._error("too_many_redirects", method)
        except ResponseTooLarge:
            return self._error("response_too_large", method)
        except HeaderInjection:
            return self._error("header_injection", method)
        except CookieInjection:
            return self._error("cookie_injection", method)
        except InvalidJsonBodyTemplate:
            return self._error("invalid_json_body", method)
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException):
            return self._error("timeout", method)
        except httpx.InvalidURL:
            # InvalidURL is NOT an httpx.HTTPError; without this it would escape.
            return self._error("invalid_url", method)
        except httpx.HTTPError:
            return self._error("connection_error", method)
        except Exception:  # noqa: BLE001 — a row-local failure must never halt the run
            return self._error("request_failed", method)

        status = resp.status_code
        if not (200 <= status < 300):
            return self._error("http_error", method, status=status)
        try:
            data = strict_json_loads(resp.content)
        except (json.JSONDecodeError, ValueError):
            return self._error("invalid_json", method, status=status)
        if not isinstance(data, dict):
            return self._error("invalid_json_shape", method, status=status)
        return data

    @staticmethod
    def _error(
        code: str,
        method: str,
        *,
        status: int | None = None,
    ) -> dict[str, Any]:
        """Never echo actual arguments: custom handlers may derive any URL text."""
        text = f"{code}: {method}"
        if status is not None:
            text = f"{text} (HTTP {status})"
        if code == "cancelled":
            raise HttpRequestCancelled(code, text)
        raise RowError(code, text[:500])
