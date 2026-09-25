"""Bounded public-web reads for an explicitly web-enabled Project Ask turn."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from frisket.ai.research.row_answer import FETCH_CHARS, FETCH_TIMEOUT_SECONDS
from frisket.redaction import redact_text


MAX_WEB_RESULTS = 6
MAX_WEB_SNIPPET_CHARS = 600


def safe_web_url(value: object) -> str | None:
    """Return a display-safe public URL without changing its source identity.

    A URL carrying credentials or a sensitive query value is deliberately not
    emitted at all.  Removing that value could point at a different document,
    so presenting a shortened URL as the fetched source would be misleading.
    Ordinary query parameters remain intact.
    """

    if not isinstance(value, str) or not value or len(value) > 4_096:
        return None
    try:
        parsed = urlsplit(value)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    # Reuse the shared redactor's sensitive query vocabulary.  If it changes
    # a URL, preserve no link rather than claim a modified location was read.
    if redact_text(value, max_chars=4_096, one_line=True) != value:
        return None
    host = hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def safe_web_text(value: object, *, limit: int) -> str:
    """Bound model-visible and report-visible web text after shared redaction."""

    text = redact_text(str(value or ""), max_chars=limit, one_line=True)
    return text[:limit]


def _retrieved_at() -> str:
    return datetime.now(timezone.utc).isoformat()


async def search_web(
    query: str,
    *,
    search: Callable[..., Awaitable[tuple[str, list[str]]]],
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return structured, bounded public search results with safe URLs only."""

    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("query must be a non-empty string up to 500 characters")
    observation, urls = await asyncio.wait_for(search(query, timeout=timeout), timeout)
    parsed: dict[str, tuple[str, str]] = {}
    for title, url, snippet in re.findall(
        r"(?m)^-\s*(.*?)\s*\|\s*(\S+)\s*\n\s*(.*)$", observation or ""
    ):
        parsed[url] = (title, snippet)
    results: list[dict[str, str]] = []
    for raw_url in urls[:MAX_WEB_RESULTS]:
        url = safe_web_url(raw_url)
        if url is None or any(item["url"] == url for item in results):
            continue
        title, snippet = parsed.get(raw_url, (urlsplit(url).hostname or "Web result", ""))
        results.append(
            {
                "title": safe_web_text(title, limit=160) or (urlsplit(url).hostname or "Web result"),
                "url": url,
                "snippet": safe_web_text(snippet, limit=MAX_WEB_SNIPPET_CHARS),
            }
        )
    return {"query": query, "results": results, "retrieved_at": _retrieved_at()}


async def fetch_web_page(
    url: str,
    *,
    http: Any,
    fetch: Callable[[str, Any], Awaitable[str]],
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch one guarded page and retain only a bounded, safe receipt."""

    safe_url = safe_web_url(url)
    if safe_url is None:
        raise ValueError("Use a public result URL without credentials or sensitive query values")
    text = await asyncio.wait_for(fetch(url, http), timeout)
    if text.startswith(("fetch failed:", "invalid url")):
        raise ValueError("The page could not be retrieved safely")
    bounded = safe_web_text(text, limit=FETCH_CHARS)
    return {
        "url": safe_url,
        "text": bounded,
        "truncated": len(str(text)) > len(bounded),
        "retrieved_at": _retrieved_at(),
    }
