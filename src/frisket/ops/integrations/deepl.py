"""DeepL API v2 translation client.

A thin async wrapper over the shared ``httpx.AsyncClient`` the MapRunner already
owns (``OpContext.http``), mirroring ``ops/_sidecar.py``'s injectable-client
shape so tests drive it with ``httpx.MockTransport`` and NEVER touch the network
(tests never touch the live network by default). The key resolves through ``credentials.resolve_credential``
(env → project secrets) at the call site; this module only consumes it.

DeepL routes free-tier keys (``:fx`` suffix) to ``api-free.deepl.com`` and
paid keys to ``api.deepl.com`` — the ONLY thing the suffix decides.
"""

from __future__ import annotations

from typing import Any

import httpx

from frisket.ops.integrations.hosted_error import call_hosted_json
from frisket.ops.integrations.translate_common import (
    TranslateEngineError,
    hosted_translate_response_accounting,
)

DEEPL_FREE_HOST = "https://api-free.deepl.com"
DEEPL_PRO_HOST = "https://api.deepl.com"


def deepl_base_url(api_key: str) -> str:
    """Free vs paid endpoint, decided solely by the ``:fx`` key suffix."""
    return DEEPL_FREE_HOST if api_key.strip().endswith(":fx") else DEEPL_PRO_HOST


def _deepl_message(body: dict[str, Any] | None) -> str:
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    return ""


def _raise_for_status(status: int, body: dict[str, Any] | None = None) -> None:
    # Classify from status AND the structured body so a 400 naming
    # target_lang/source_lang becomes a language error, not a generic
    # bad_request. Retryable transient failures are flagged so a retry can
    # tell them from a bad key / bad language.
    if status == 403:
        raise TranslateEngineError(
            code="auth",
            message="DeepL rejected the API key (403). Check DEEPL_API_KEY in Settings → Secrets.",
        )
    if status in (429, 456):
        raise TranslateEngineError(
            code="quota",
            message=(
                "DeepL quota exceeded or rate-limited "
                f"({status}). The character limit for this key is reached."
            ),
            retryable=status == 429,
        )
    if status == 400:
        message = _deepl_message(body)
        low = message.lower()
        if "target_lang" in low:
            raise TranslateEngineError(
                code="invalid_target",
                message=f"DeepL does not support that target language ({message}).",
            )
        if "source_lang" in low:
            raise TranslateEngineError(
                code="invalid_source",
                message=f"DeepL does not support that source language ({message}).",
            )
        raise TranslateEngineError(
            code="bad_request",
            message=f"DeepL rejected the request (400){f': {message}' if message else ''}.",
        )
    if status >= 500:
        raise TranslateEngineError(
            code="http",
            message=f"DeepL service error ({status}); try again shortly.",
            retryable=True,
        )
    if status >= 400:
        raise TranslateEngineError(
            code="http", message=f"DeepL request failed ({status})."
        )


async def deepl_translate(
    http: httpx.AsyncClient,
    api_key: str,
    texts: list[str],
    target_lang: str,
    source_lang: str | None = None,
    *,
    credential_source: str = "none",
) -> list[dict[str, Any]]:
    """Translate ``texts`` into ``target_lang`` (a DeepL code, e.g. ``ES``).

    Returns one ``{"text", "detected_source_language", "billed_characters"}``
    dict per input, in order. ``billed_characters`` is DeepL's own usage count
    (requested via ``show_billed_characters`` and verified against the live
    API) so recorded spend reflects real billed characters, not a
    client-side length guess; it is ``None`` if DeepL omits it. Raises
    :class:`TranslateEngineError` on any provider failure.
    """
    if not texts:
        return []
    payload: dict[str, Any] = {
        "text": texts,
        "target_lang": target_lang,
        "show_billed_characters": True,
    }
    if source_lang:
        payload["source_lang"] = source_lang
    response, body = await call_hosted_json(
        lambda: http.post(
            f"{deepl_base_url(api_key)}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            json=payload,
        ),
        error_cls=TranslateEngineError,
        provider_name="DeepL",
    )
    try:
        _raise_for_status(
            response.status_code, body if isinstance(body, dict) else None
        )
        if not isinstance(body, dict):
            raise TranslateEngineError(
                code="http", message="DeepL returned a non-JSON response."
            )
        translations = body.get("translations")
        if not isinstance(translations, list) or len(translations) != len(texts):
            raise TranslateEngineError(
                code="http",
                message=(
                    "DeepL response shape was unexpected (translation count mismatch)."
                ),
            )
    except TranslateEngineError as exc:
        if exc.accounting is None:
            exc.accounting = hosted_translate_response_accounting(
                engine="deepl",
                provider="deepl",
                credential_source=credential_source,
                status_code=response.status_code,
                request_id=(
                    response.headers.get("x-request-id")
                    or response.headers.get("request-id")
                ),
            )
        raise
    return [
        {
            "text": str(item.get("text", "")),
            "detected_source_language": item.get("detected_source_language"),
            "billed_characters": item.get("billed_characters"),
        }
        for item in translations
    ]
