"""Shared shape for a hosted-API integration's per-call failure, plus the
request/JSON-parse boilerplate every hosted client (DeepL, Google Cloud
Translation, Datalab) repeats around it.

``deepl.py``, ``google_translate.py``, and ``datalab.py`` each independently
defined a byte-for-byte identical error dataclass
(``code``/``message``/``retryable``,
``__str__`` returning ``message``) and, four times across the three files,
the same "await the call, catch ``httpx.HTTPError`` as a transport-coded
error, then best-effort ``response.json()``" shape. This module is the
neutral home for both: it isn't translation- or document-conversion-specific,
so it doesn't force a domain name (``Translate*``) onto an unrelated hosted
integration (OCR/to_markdown).

Back-compat: ``frisket.integrations.translate_common.TranslateEngineError``
and ``frisket.integrations.datalab.DatalabEngineError`` remain importable
under their original names — both are now literally this class (not a
subclass), so ``except TranslateEngineError`` / ``except DatalabEngineError``
/ ``isinstance`` checks at existing call sites (MapRunner's per-row boundary,
recipe dispatch) keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx


@dataclass
class HostedEngineError(Exception):
    """A hosted-engine failure with a stable, user-facing code + message.

    ``code`` is the taxonomy bucket (auth / permission / quota / invalid_target
    / invalid_source / bad_request / http / transport / ...); ``message`` is
    what the row (or the action-level preflight) shows. ``retryable`` marks
    transient failures (rate limits, 5xx, transport) so a retry can
    distinguish them from a bad key or an unsupported request.
    """

    code: str
    message: str
    retryable: bool = False
    # A provider can return a concrete response even when its status/body is
    # unusable or later output validation fails.  Carry the response-proven
    # accounting through the row-error boundary without inventing its cost.
    accounting: dict[str, Any] | None = None
    # True only after an asynchronous provider has returned a stable accepted
    # job identity.  Such an error is not an ordinary retryable row failure:
    # another submission would create a second paid job.
    provider_job_accepted: bool = False

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


async def call_hosted_json(
    make_request: Callable[[], Awaitable[httpx.Response]],
    *,
    error_cls: type[HostedEngineError],
    provider_name: str,
) -> tuple[httpx.Response, Any]:
    """Await ``make_request()``, translating a transport failure into
    ``error_cls``, then best-effort parse the response body as
    JSON (``None`` on a non-JSON body — the caller decides whether an absent
    body is itself an error, since that varies: datalab's ``_post``/``_poll``
    check ``status_code`` first, deepl/google funnel every status through
    their own ``_raise_for_status``).

    Returns ``(response, body)``; the caller still does its own status-code
    classification. Hosted clients deliberately retain this difference to
    avoid forcing a single status-check order onto integrations that need
    different ones.
    """
    try:
        response = await make_request()
    except httpx.HTTPError as exc:
        raise error_cls(
            code="transport",
            message=f"Could not reach {provider_name}: {exc}",
            retryable=True,
        ) from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    return response, body
