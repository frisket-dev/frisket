from __future__ import annotations

from collections.abc import Collection
from typing import Any

from frisket.features.url_classification import YTDLP_DOWNLOAD_ACTION_KIND, classify_url

# A sample, not the whole column — cheap and matches "applied to a sample of
# the imported column's values".
_SAMPLE_LIMIT = 10

# Recognized single-media matchers share the generic yt-dlp action; other URLs
# remain direct fetch candidates.

MediaDownloadCandidate = str  # 'media.ytdlp_download' | 'media.fetch_url'

_CANDIDATE_COLUMN_TYPES = ("link", "text")
_EXISTING_MEDIA_COLUMN_TYPES = ("audio", "video", "file")


def compute_media_download_candidates(
    columns: list[Any],
    data: dict[int, dict[str, Any]],
    row_ids: list[int],
    *,
    enabled_plugin_ids: Collection[str] = (),
) -> dict[int, MediaDownloadCandidate]:
    """``column_id -> action_id`` for link/text columns whose sampled
    values classify as downloadable single media (kind == "media").

    Gated sheet-wide on "no corresponding audio/video/file media column yet"
    (SS3.2): once a download step has produced a real media column, this
    channel goes quiet — the transcribe hint (SS1/SS2/SS3.1's
    ``transcript_status``) takes over from there. ``data``/``row_ids`` are the
    SAME per-row cell values `_sheet_data_payload` already loaded for this
    page (no extra query) — a sample of the first page's rows is exactly "a
    sample of the imported column's values."
    """
    if any(c["type"] in _EXISTING_MEDIA_COLUMN_TYPES for c in columns):
        return {}

    result: dict[int, MediaDownloadCandidate] = {}
    for c in columns:
        if c["type"] not in _CANDIDATE_COLUMN_TYPES:
            continue
        sampled = 0
        launcher: MediaDownloadCandidate | None = None
        for rid in row_ids:
            if sampled >= _SAMPLE_LIMIT:
                break
            value = data.get(rid, {}).get(str(c["id"]))
            if not isinstance(value, str) or not value.strip():
                continue
            sampled += 1
            classification = classify_url(
                value.strip(), enabled_plugin_ids=enabled_plugin_ids
            )
            if classification is None or classification.kind != "media":
                continue
            if launcher is None:
                launcher = (
                    YTDLP_DOWNLOAD_ACTION_KIND
                    if classification.handler.action_kind == YTDLP_DOWNLOAD_ACTION_KIND
                    else "media.fetch_url"
                )
        if launcher is not None:
            result[c["id"]] = launcher
    return result
