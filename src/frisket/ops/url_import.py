"""URL transport acquisition using the shared classifier and downloaders."""

from __future__ import annotations

import hashlib
from typing import Any

from frisket.features.url_classification import (
    classify_url,
    is_supported_ytdlp_media_url,
)
from frisket.ops import ytdlp
from frisket.ops.enclosures import download_url
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document


def _file_import_media_kind(mime: str | None) -> str:
    if mime and mime.startswith("audio/"):
        return "audio"
    if mime and mime.startswith("video/"):
        return "video"
    if mime and mime.startswith("image/"):
        return "image"
    return "file"


def acquire_url_records(
    urls: list[str],
    *,
    enabled_plugin_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[
    list[dict[str, Any]],
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    records: list[dict[str, Any]] = []
    media_kinds: list[str] = []
    blob_records: list[dict[str, Any]] = []
    blob_refs: list[dict[str, Any]] = []
    url_refs: list[dict[str, Any]] = []
    error_refs: list[dict[str, Any]] = []

    for idx, raw_url in enumerate(urls, start=1):
        url = str(raw_url or "").strip()
        record: dict[str, Any] = {"url": url}
        source_ref: dict[str, Any] = {
            "kind": "import_url_source",
            "row_index": idx,
            "url": url,
        }
        if not url.startswith(("http://", "https://")):
            message = "URL must start with http:// or https://"
            record["error"] = message
            record["size"] = 0
            error_ref = {
                "kind": "import_url_error",
                "row_index": idx,
                "url": url,
                "error": message,
            }
            source_ref["status"] = "error"
            source_ref["error"] = message
            error_refs.append(error_ref)
        elif is_supported_ytdlp_media_url(url, enabled_plugin_ids=enabled_plugin_ids):
            classification = classify_url(url, enabled_plugin_ids=enabled_plugin_ids)
            provider = classification.provider if classification else "yt_dlp"
            try:
                download = ytdlp.download_media(url)
                acquisition = dict(download.metadata or {})
                if download.duration_seconds is not None:
                    acquisition.setdefault(
                        "duration_seconds", download.duration_seconds
                    )
                digest = hashlib.sha256(download.data).hexdigest()
                cell = media_cell(
                    digest,
                    mime=download.mime,
                    filename=download.filename,
                )
                record["media"] = cell
                record["size"] = len(download.data)
                media_kinds.append(_file_import_media_kind(download.mime))
                blob_ref = {
                    "kind": "imported_blob",
                    "hash": digest,
                    "filename": download.filename,
                    "mime": download.mime,
                    "size": len(download.data),
                    "row_index": idx,
                    "source_url": url,
                    "provider": provider,
                }
                blob_refs.append(blob_ref)
                blob_records.append(
                    {
                        "data": download.data,
                        "hash": digest,
                        "filename": download.filename,
                        "mime": download.mime,
                        "source_url": url,
                        "metadata": owned_media_metadata_document(
                            acquisition=acquisition
                        ),
                    }
                )
                source_ref["status"] = "downloaded"
                source_ref["provider"] = provider
            except Exception as exc:  # noqa: BLE001 - partial imports preserve errors
                message = str(exc)
                record["error"] = message
                record["size"] = 0
                source_ref["status"] = "error"
                source_ref["provider"] = provider
                source_ref["error"] = message
                error_refs.append(
                    {
                        "kind": "import_url_error",
                        "row_index": idx,
                        "url": url,
                        "provider": provider,
                        "error": message,
                    }
                )
        else:
            data, mime, filename, err = download_url(url)
            if err is not None:
                record["error"] = err
                record["size"] = 0
                source_ref["status"] = "error"
                source_ref["provider"] = "direct"
                source_ref["error"] = err
                error_refs.append(
                    {
                        "kind": "import_url_error",
                        "row_index": idx,
                        "url": url,
                        "provider": "direct",
                        "error": err,
                    }
                )
            else:
                digest = hashlib.sha256(data).hexdigest()
                metadata: dict[str, Any] = {}
                cell = media_cell(digest, mime=mime, filename=filename)
                record["media"] = cell
                record["size"] = len(data)
                media_kinds.append(_file_import_media_kind(mime))
                blob_ref = {
                    "kind": "imported_blob",
                    "hash": digest,
                    "filename": filename,
                    "mime": mime,
                    "size": len(data),
                    "row_index": idx,
                    "source_url": url,
                    "provider": "direct",
                }
                blob_refs.append(blob_ref)
                blob_records.append(
                    {
                        "data": data,
                        "hash": digest,
                        "filename": filename,
                        "mime": mime,
                        "source_url": url,
                        "metadata": metadata,
                    }
                )
                source_ref["status"] = "downloaded"
                source_ref["provider"] = "direct"
        url_refs.append(source_ref)
        records.append(record)
    return records, media_kinds, blob_records, blob_refs, url_refs, error_refs
