"""The shared frisket-models sidecar client.

URL + bearer + multipart + 429-retry was copied in
sdk/ops/ocr.py and sdk/ops/to_markdown.py (2 copies); sdk/ops/transcribe_engines.py is
the THIRD consumer, so the design's trigger fired and the client lives here
now — all three ops import ``sidecar_post`` from this module.

The contract lives in sidecar/README.md and sidecar/src/frisket_models/app.py:

- discovery via ``FRISKET_MODELS_URL`` (compose profile 'models'); auth is the
  shared bearer secret ``FRISKET_MODELS_TOKEN`` (the sidecar is never
  anonymous — it refuses to start without one). Dispatch
  reads NEITHER env name here: the values reach ``sidecar_post`` as a
  ``ConnectionConfig`` — from the route binding (routed runs) or the
  ephemeral-binding deref of the models-gateway target (unrouted calls);
  the env helpers below remain for the status/doctor surfaces only
- blob routes are multipart *bytes* (the sidecar may be another machine and
  never reads the app's disk); ``engine`` rides as a form field
- backpressure, not queueing: a full sidecar answers 429 + Retry-After and
  this client sleeps-and-retries politely (the job queue lives app-side;
  the SIDECAR GETS NO QUEUE)
"""

from __future__ import annotations

import asyncio
import math
import os
from typing import Any, Callable

from frisket.execution.dispatch_control import DispatchControl

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 1.5  # 429 backoff: 1.5s, 3.0s, 4.5s between the 4 attempts
MAX_RETRY_AFTER_SECONDS = 60.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 3600.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0


class SidecarHTTPError(RuntimeError):
    """HTTP failure with machine-readable status and no private response body."""

    def __init__(self, op: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"sidecar {op} failed ({status_code})")


def sidecar_base_url() -> str | None:
    """Where the frisket-models sidecar lives, if configured. STATUS/DOCTOR
    surface only (operability/diagnostics.py, server/app.py's status probe):
    dispatch never consults env — ``sidecar_post`` takes its ``ConnectionConfig``
    from a route binding or the ephemeral-binding helper."""
    return os.environ.get("FRISKET_MODELS_URL") or None


def sidecar_headers() -> dict[str, str]:
    """Bearer auth from the shared secret; empty when unset (status/doctor
    surface only, like ``sidecar_base_url``)."""
    token = sidecar_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def sidecar_token() -> str | None:
    """Configured bearer token, treating blank/whitespace env as missing
    (status/doctor surface only, like ``sidecar_base_url``)."""
    token = (os.environ.get("FRISKET_MODELS_TOKEN") or "").strip()
    return token or None


def probe_sidecar_capabilities(
    *,
    base: str | None,
    token: str | None,
    timeout: Any,
    format_error: Callable[[Exception], str] = lambda e: f"{type(e).__name__}: {e}",
) -> dict[str, Any]:
    """Shared GET /capabilities probe for the frisket-models sidecar
    (status/doctor surface only — like ``sidecar_base_url`` et al., never
    consulted by dispatch).

    Was independently reimplemented in server/app.py's action-catalog probe
    and operability/diagnostics.py's ``models_sidecar_report`` (same
    base-url lookup, same GET, same ``{configured, available, engines,
    error}`` shape); consolidated here alongside ``sidecar_post`` once the
    duplication crossed the rule-of-three line.

    Returns ``{"configured": bool, "available": bool, "engines": list,
    "error": str | None}``. ``configured`` is False only when
    ``FRISKET_MODELS_URL`` itself is unset; a configured-but-tokenless
    sidecar is reported as ``configured=True, available=False`` with an
    explicit "token missing" error — callers that don't want that
    distinction (e.g. a summary line that just says "unavailable") fold it
    at their own wrapper rather than losing it here.

    Two real differences between the former call sites are kept as
    parameters instead of being silently normalized away:

    - ``timeout``: the app's action-catalog probe races an interactive
      request and wants the tight connect/read budget in
      ``server/app.py``'s ``SIDECAR_CAPABILITIES_*_TIMEOUT_SECONDS``
      constants; the doctor/diagnostics probe runs once at CLI/startup time
      and used a flat ``timeout=5.0``. Pass whatever your call site needs
      (an ``httpx.Timeout`` or a bare float — both are valid ``httpx.get``
      timeout values).
    - ``format_error``: the app's probe formatted exceptions as
      ``f"{type(e).__name__}: {e}"``; diagnostics used bare ``str(e)``.
      Both are preserved via this callable rather than picking one.
    """
    if not base:
        return {
            "configured": False,
            "available": False,
            "engines": [],
            "error": None,
        }
    if not token:
        return {
            "configured": True,
            "available": False,
            "engines": [],
            "error": (
                "FRISKET_MODELS_URL is configured but "
                "FRISKET_MODELS_TOKEN is missing or empty"
            ),
        }
    import httpx

    try:
        resp = httpx.get(
            f"{base.rstrip('/')}/capabilities",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        body = resp.json()
        engines = body.get("engines", []) if isinstance(body, dict) else []
        return {
            "configured": True,
            "available": True,
            "engines": engines if isinstance(engines, list) else [],
            "error": None,
            # Additive: not part of app.py's original probe shape, but
            # models_sidecar_report's summary line wants the sidecar's
            # advertised version and shouldn't re-fetch to get it.
            "version": body.get("version", "?") if isinstance(body, dict) else "?",
        }
    except httpx.ReadTimeout as e:
        return {
            "configured": True,
            "available": False,
            "engines": [],
            "error": format_error(e),
            # A configured execution composition can still treat its exact
            # declared engines as selectable while scale-to-zero compute
            # wakes. HTTP/auth failures and explicit engine failures are not
            # transient and continue to fail closed below.
            "transient_failure": True,
        }
    except Exception as e:  # noqa: BLE001 -- report, never raise
        return {
            "configured": True,
            "available": False,
            "engines": [],
            "error": format_error(e),
        }


def ephemeral_gateway_connection(
    *,
    op: str,
    data: dict[str, str] | None = None,
    engine: str | None = None,
    light_engine: str | None = None,
) -> Any:
    """Persistence-free gateway ``ConnectionConfig`` for an UNROUTED call
    (the env fallback was removed). Previews and the non-routed callers
    (ocr/convert/ner) deref the models-gateway target through the same
    static-provider + ``candidate_binding`` machinery routed dispatch uses;
    an unconfigured gateway raises the target's configuration remedy plus
    the local-engine pointer — never a silent fallback."""
    from frisket.execution.runtime_binding import (
        RouteBindingUnavailable,
        ephemeral_target_binding,
    )

    engine = engine or (data or {}).get("engine") or op
    try:
        binding = ephemeral_target_binding("models-gateway", engine)
    except RouteBindingUnavailable as exc:
        hint = f" Or use engine='{light_engine}'." if light_engine else ""
        raise RuntimeError(
            f"engine '{engine}' needs the frisket-models sidecar — {exc.remedy}{hint}"
        ) from None
    return binding.connection


async def sidecar_post(
    ctx: Any,
    route: str,
    *,
    files: list[tuple] | None = None,
    data: dict[str, str] | None = None,
    json: Any = None,
    op: str = "request",
    light_engine: str | None = None,
    timeout: Any | None = None,
    structured_errors: bool = False,
    connection: Any | None = None,
    dispatch: DispatchControl,
    quantity: dict[str, float | None] | None = None,
) -> dict:
    """Determine accounting and own one logical model-server operation."""
    from hashlib import sha256
    from frisket.execution.purchases import (
        PurchaseObservation,
        PurchaseRequest,
        execute_purchase,
        policy_for_context,
    )

    capabilities = {
        "/ocr": "ocr",
        "/to-markdown": "document.convert",
        "/v1/transcribe": "transcribe",
        "/ner": "ner",
        "/classify": "classify",
    }
    if route not in capabilities:
        raise ValueError(f"No purchase contract for model-server route {route!r}")
    engine = (data or {}).get("engine") or (json or {}).get("engine")
    if not engine:
        raise ValueError("Model-server purchase requires an engine")
    measured = dict(quantity or {})
    if route == "/ocr":
        measured["pages"] = len(files or ())
    request = PurchaseRequest(
        provider="frisket-models",
        capability=capabilities[route],
        engine=engine,
        credential_source="none",
        context=ctx,
        quantity=measured,
        prepared={
            "route": route,
            "data": data,
            "json": json,
            "files": [sha256(part[1][1]).hexdigest() for part in (files or ())],
        },
    )

    def observe(body):
        usage: dict[str, Any] = {}
        if isinstance(body, dict):
            if route == "/ocr" and isinstance(body.get("pages"), list):
                usage["pages"] = len(body["pages"])
            elif route == "/to-markdown":
                documents = body.get("documents", body.get("results"))
                documents = documents if isinstance(documents, list) else [body]
                pages = [
                    doc.get("ocr_used") for doc in documents if isinstance(doc, dict)
                ]
                if (
                    len(pages) == len(documents)
                    and pages
                    and all(isinstance(value, list) for value in pages)
                ):
                    usage["pages"] = sum(len(value) for value in pages)
            elif (
                route == "/v1/transcribe"
                and isinstance(body.get("results"), list)
                and body["results"]
            ):
                durations = [
                    item.get("duration")
                    for item in body["results"]
                    if isinstance(item, dict)
                ]
                if len(durations) == len(body["results"]) and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value >= 0
                    for value in durations
                ):
                    usage["audio_seconds"] = sum(durations)
                elif measured.get("audio_seconds") is not None:
                    usage["audio_seconds"] = measured["audio_seconds"]
            elif route in {"/ner", "/classify"}:
                usage["requests"] = 1
        return PurchaseObservation(body, usage)

    async def invoke():
        return await _sidecar_post_transport(
            ctx,
            route,
            files=files,
            data=data,
            json=json,
            op=op,
            light_engine=light_engine,
            timeout=timeout,
            structured_errors=structured_errors,
            connection=connection,
            dispatch=dispatch,
        )

    return await execute_purchase(
        request,
        policy=policy_for_context(ctx),
        dispatch=dispatch,
        invoke=invoke,
        observe=observe,
    )


async def _sidecar_post_transport(
    ctx: Any,
    route: str,
    *,
    files: list[tuple] | None = None,
    data: dict[str, str] | None = None,
    json: Any = None,
    op: str = "request",
    light_engine: str | None = None,
    timeout: Any | None = None,
    structured_errors: bool = False,
    connection: Any | None = None,
    dispatch: DispatchControl,
) -> dict:
    """POST a route on the frisket-models sidecar and return the JSON body.

    Blob routes pass ``files`` (multipart bytes) + ``data`` (form fields, e.g.
    ``engine``); the JSON text routes (/ner, /rerank) pass ``json``. ``op``
    labels error messages ("sidecar ocr failed ..."); ``light_engine`` names
    the local fallback in the not-configured pointer. Versioned callers set
    ``structured_errors`` so a non-200 JSON error envelope reaches their strict
    parser instead of being flattened into the legacy generic error string.

    Raises RuntimeError with a pointer (never a crash, never a silent
    fallback) when the sidecar is unconfigured, has no http client, or is
    still 429 after MAX_ATTEMPTS. A caller may set a route-specific timeout;
    otherwise the connection's request/connect budgets cover every HTTP phase.
    On backpressure the server's numeric ``Retry-After`` wins; missing or
    malformed values use bounded local backoff, and the terminal attempt never
    sleeps after failure.

    ``connection``: a routed call passes the route
    binding's ``ConnectionConfig`` (execution/provider.py) and the gateway
    URL + bearer come from IT. An unrouted call (previews; the non-routed
    ocr/convert/ner callers) passes no connection and one is obtained here
    via the ephemeral-binding helper — env is NEVER consulted for dispatch
    (the pre-route env fallback was removed). A connection lacking
    either value is a defect (the target deref that produced it requires
    both) and fails loudly."""
    if connection is None:
        connection = ephemeral_gateway_connection(
            op=op, data=data, light_engine=light_engine
        )
    base = connection.base_url
    token = connection.token
    if not base or not token:
        raise RuntimeError(
            f"route-bound sidecar {op} has no gateway URL/token on its "
            "connection config — the target deref is defective"
        )
    if ctx is None or ctx.http is None:
        raise RuntimeError(f"no http client available for sidecar {op}")
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{base.rstrip('/')}{route}"
    if timeout is None:
        import httpx

        request_timeout = float(
            connection.timeout_seconds or DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
        timeout = httpx.Timeout(
            request_timeout,
            connect=float(
                connection.connect_timeout_seconds or DEFAULT_CONNECT_TIMEOUT_SECONDS
            ),
        )
    # Pass only the kwargs the route actually uses: blob routes get
    # files+data, the JSON text routes get json. This keeps the multipart
    # call byte-for-byte what ocr/convert hand-rolled (post(url, files=,
    # data=, headers=)) so their existing fakes/transports still match.
    # Modal returns a same-function 303 result URL after 150 seconds. Following
    # it lets cold model loads finish without changing any unrelated client.
    kwargs: dict[str, Any] = {
        "headers": headers,
        "follow_redirects": True,
        "timeout": timeout,
    }
    if json is not None:
        kwargs["json"] = json
    else:
        kwargs["files"] = files
        kwargs["data"] = data
    for attempt in range(MAX_ATTEMPTS):
        dispatch.check()
        # A dispatched request can finish after cancellation. Return its known
        # response for accounting; publication has its own authority check.
        resp = await ctx.http.post(url, **kwargs)
        if resp.status_code != 429:  # sidecar backpressure: retry politely
            break
        if attempt == MAX_ATTEMPTS - 1:
            break
        retry_after = resp.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after is not None else None
            if delay is not None and (
                not math.isfinite(delay) or delay < 0 or delay > MAX_RETRY_AFTER_SECONDS
            ):
                delay = None
        except (TypeError, ValueError):
            delay = None
        dispatch.check()
        await asyncio.sleep(
            delay if delay is not None else BACKOFF_SECONDS * (attempt + 1)
        )
    if resp.status_code != 200 and structured_errors:
        try:
            body = resp.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if (
            isinstance(body, dict)
            and isinstance(body.get("contract_version"), str)
            and isinstance(error, dict)
            and isinstance(error.get("code"), str)
            and isinstance(error.get("message"), str)
            and isinstance(error.get("retryable"), bool)
        ):
            return body
    if resp.status_code != 200:
        raise SidecarHTTPError(op, resp.status_code)
    return resp.json()
