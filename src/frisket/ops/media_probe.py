"""Compact media facts for download, import, and backfill paths."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from frisket.ops.media_metadata import (
    MEDIA_METADATA_FORMAT_REGISTRY,
    extract_media_metadata,
)


def _ingest_exts_by_role() -> dict[str, frozenset[str]]:
    """Derive ingest extension roles from the metadata format registry."""

    tool_aliases: dict[str, tuple[str, str, str]] = MEDIA_METADATA_FORMAT_REGISTRY[
        "tool_aliases"
    ]
    canonical_roles = {
        canonical: role for role, canonical, _mime in tool_aliases.values()
    }
    by_role: dict[str, set[str]] = {}
    aliases: dict[str, str] = MEDIA_METADATA_FORMAT_REGISTRY["extension_aliases"]
    for ext, canonical in aliases.items():
        entry = tool_aliases.get(ext)
        role = entry[0] if entry is not None else canonical_roles.get(canonical)
        if role is not None:
            by_role.setdefault(role, set()).add(f".{ext}")
    return {role: frozenset(exts) for role, exts in by_role.items()}


_INGEST_EXTS_BY_ROLE = _ingest_exts_by_role()
_INGEST_IMAGE_EXTS = _INGEST_EXTS_BY_ROLE["image"]
_INGEST_AUDIO_EXTS = _INGEST_EXTS_BY_ROLE["audio"]
_INGEST_VIDEO_EXTS = _INGEST_EXTS_BY_ROLE["video"]
_INGEST_HTML_EXTS = frozenset({".htm", ".html"})
_INGEST_TEXT_EXTS = frozenset(
    {".csv", ".json", ".log", ".md", ".text", ".txt", ".xml", ".yaml", ".yml"}
)
_INGEST_FEED_MIMES = frozenset(
    {"application/atom+xml", "application/rss+xml", "application/feed+json"}
)
_INGEST_PROBED_KINDS = frozenset({"audio", "video", "image", "pdf"})

# Probe key <- normalized v1 role. These compact facts live inside the blob's
# `_media_probe_v1` namespace.
_INGEST_PROJECTED_ROLES: tuple[tuple[str, str], ...] = (
    ("format", "format"),
    ("duration_seconds", "duration_seconds"),
    ("width", "width_pixels"),
    ("height", "height_pixels"),
    ("pages", "page_count"),
    ("audio_codec", "audio_codec"),
    ("video_codec", "video_codec"),
    ("sample_rate", "sample_rate_hz"),
    ("channels", "channel_count"),
)


def _clean_mime(mime: str | None) -> str | None:
    if not mime:
        return None
    return mime.split(";", 1)[0].strip().lower() or None


def _ingest_kind(mime: str | None, filename: str | None) -> str:
    suffix = PurePosixPath(filename or "").suffix.lower()
    mime = mime or ""
    if mime in _INGEST_FEED_MIMES or mime.endswith("+rss") or "rss+xml" in mime:
        return "feed"
    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if mime.startswith("audio/") or suffix in _INGEST_AUDIO_EXTS:
        return "audio"
    if mime.startswith("video/") or suffix in _INGEST_VIDEO_EXTS:
        return "video"
    if mime.startswith("image/") or suffix in _INGEST_IMAGE_EXTS:
        return "image"
    if mime == "text/html" or suffix in _INGEST_HTML_EXTS:
        return "html"
    if mime.startswith("text/") or suffix in _INGEST_TEXT_EXTS:
        return "text"
    if mime in {"application/json", "application/xml", "text/xml"}:
        return "text"
    return "file"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_format(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if candidate in MEDIA_METADATA_FORMAT_REGISTRY["canonical_formats"]:
        return candidate
    return MEDIA_METADATA_FORMAT_REGISTRY["extension_aliases"].get(candidate)


def probe_for_ingest(
    path: str | Path,
    *,
    mime: str | None = None,
    filename: str | None = None,
    digest: str | None = None,
) -> dict[str, Any]:
    """Return compact, non-throwing metadata for an ingest-owned blob."""

    source_path = Path(path)
    guessed_mime = _clean_mime(mime) or _clean_mime(
        mimetypes.guess_type(filename or source_path.name)[0]
    )
    kind = _ingest_kind(guessed_mime, filename or source_path.name)
    meta: dict[str, Any] = {"kind": kind}
    try:
        if not source_path.exists():
            meta["probe_error"] = "file missing"
            return _compact_ingest(meta)
        size_bytes = source_path.stat().st_size
        meta["size_bytes"] = size_bytes
        if kind in _INGEST_PROBED_KINDS:
            envelope = extract_media_metadata(
                source_path,
                digest=digest if digest is not None else _file_sha256(source_path),
                size_bytes=size_bytes,
                filename=filename or source_path.name,
                claimed_mime=guessed_mime,
            )
            normalized = envelope.get("normalized")
            if isinstance(normalized, Mapping):
                detected = normalized.get("kind")
                if detected in _INGEST_PROBED_KINDS:
                    meta["kind"] = detected
                for key, role in _INGEST_PROJECTED_ROLES:
                    value = normalized.get(role)
                    if key == "format":
                        value = _canonical_format(value)
                    meta[key] = value
    except Exception as exc:  # noqa: BLE001 - metadata must not block import
        meta["probe_error"] = str(exc)[:300]
    return _compact_ingest(meta)


def _compact_ingest(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in meta.items() if value not in (None, "")}


__all__ = ["probe_for_ingest"]
