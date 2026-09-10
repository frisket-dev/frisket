"""Google Cloud Translation API v2 client.

Same injectable-async-client shape as ``deepl.py`` so tests replay
``httpx.MockTransport`` fixtures with no live calls (tests never touch the
live network by default). Uses
the API-key REST mode (``?key=``) rather than ADC, matching the Settings →
Secrets ``GOOGLE_TRANSLATE_API_KEY`` credential the catalog gate resolves.

NOTE: unlike the DeepL client, this module's fixtures are hand-authored from
the documented v2 response schema — the provided Google key's setup is
unverified, so this client is validated against schema fixtures, not a live
recording.
"""

from __future__ import annotations

import html
from typing import Any

import httpx

from frisket.ops.integrations.hosted_error import call_hosted_json
from frisket.ops.integrations.translate_common import (
    TranslateEngineError,
    hosted_translate_response_accounting,
)

GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"


def _raise_for_status(status: int, body: dict[str, Any] | None) -> None:
    detail = ""
    reasons: set[str] = set()
    gstatus = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            if err.get("message"):
                detail = f": {err['message']}"
            if isinstance(err.get("status"), str):
                gstatus = err["status"]
            for item in err.get("errors") or []:
                if isinstance(item, dict) and isinstance(item.get("reason"), str):
                    reasons.add(item["reason"])

    # A 401/403 is auth OR permission OR quota depending on the structured
    # `reason`/`status`, not the status code alone.
    quota_reasons = {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "dailyLimitExceeded",
    }
    permission_reasons = {"accessNotConfigured", "forbidden", "insufficientPermissions"}
    if reasons & quota_reasons or gstatus == "RESOURCE_EXHAUSTED":
        raise TranslateEngineError(
            code="quota",
            message=f"Google Cloud Translation quota/rate limit reached ({status}){detail}.",
            retryable=True,
        )
    if status in (401, 403):
        if reasons & permission_reasons or gstatus == "PERMISSION_DENIED":
            raise TranslateEngineError(
                code="permission",
                message=(
                    "Google Cloud Translation denied access "
                    f"({status}){detail}. Enable the Translation API for the "
                    "project and check the key's restrictions."
                ),
            )
        raise TranslateEngineError(
            code="auth",
            message=(
                "Google Cloud Translation rejected the API key "
                f"({status}){detail}. Check GOOGLE_TRANSLATE_API_KEY."
            ),
        )
    if status == 429:
        raise TranslateEngineError(
            code="quota",
            message=f"Google Cloud Translation quota exceeded (429){detail}.",
            retryable=True,
        )
    if status == 400:
        raise TranslateEngineError(
            code="bad_request",
            message=f"Google Cloud Translation rejected the request (400){detail}.",
        )
    if status >= 500:
        raise TranslateEngineError(
            code="http",
            message=f"Google Cloud Translation service error ({status}); try again shortly.",
            retryable=True,
        )
    if status >= 400:
        raise TranslateEngineError(
            code="http",
            message=f"Google Cloud Translation request failed ({status}){detail}.",
        )


async def google_translate(
    http: httpx.AsyncClient,
    api_key: str,
    texts: list[str],
    target_lang: str,
    source_lang: str | None = None,
    *,
    credential_source: str = "none",
) -> list[dict[str, Any]]:
    """Translate ``texts`` into ``target_lang`` (ISO-639-1, e.g. ``es``).

    Returns one ``{"text", "detected_source_language"}`` dict per input, in
    order. Raises :class:`TranslateEngineError` on any provider failure.
    """
    if not texts:
        return []
    payload: dict[str, Any] = {"q": texts, "target": target_lang, "format": "text"}
    if source_lang:
        payload["source"] = source_lang
    response, body = await call_hosted_json(
        lambda: http.post(
            GOOGLE_TRANSLATE_URL,
            params={"key": api_key},
            json=payload,
        ),
        error_cls=TranslateEngineError,
        provider_name="Google Cloud Translation",
    )
    try:
        _raise_for_status(
            response.status_code, body if isinstance(body, dict) else None
        )
        translations = (
            (body or {}).get("data", {}).get("translations")
            if isinstance(body, dict)
            else None
        )
        if not isinstance(translations, list) or len(translations) != len(texts):
            raise TranslateEngineError(
                code="http",
                message="Google Cloud Translation response shape was unexpected.",
            )
    except TranslateEngineError as exc:
        if exc.accounting is None:
            exc.accounting = hosted_translate_response_accounting(
                engine="google_translate",
                provider="google",
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
            # v2 returns HTML-escaped text even for format=text in some paths;
            # unescape defensively so downstream cells are plain text.
            "text": html.unescape(str(item.get("translatedText", ""))),
            "detected_source_language": item.get("detectedSourceLanguage"),
        }
        for item in translations
    ]
