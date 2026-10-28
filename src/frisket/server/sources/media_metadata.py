"""Shared metadata normalization for yt-dlp-backed sources."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|token|password|secret)"
    r"(\s*[:=]\s*(?:bearer\s+)?)?[^\s,;]+"
)


def entry_published_at(entry: dict[str, Any]) -> str | None:
    for key in ("timestamp", "release_timestamp", "modified_timestamp"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _iso_from_timestamp(float(value))
    for key in ("upload_date", "release_date", "published_at", "timestamp"):
        normalized = _date_or_iso(entry.get(key))
        if normalized:
            return normalized
    return None


def _date_or_iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"\d{8}", text):
        return (
            datetime(
                int(text[0:4]),
                int(text[4:6]),
                int(text[6:8]),
                tzinfo=UTC,
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
    if "T" in text:
        return text
    return None


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def thumbnail_url(entry: dict[str, Any]) -> str | None:
    direct = string_value(entry.get("thumbnail"))
    if direct:
        return direct
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict):
                url = string_value(item.get("url"))
                if url:
                    return url
    return None


def string_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def redact_provider_message(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    return _SENSITIVE_RE.sub(r"\1=<redacted>", message)


def redact_warning(warning: Any) -> str:
    return _SENSITIVE_RE.sub(r"\1=<redacted>", str(warning))
