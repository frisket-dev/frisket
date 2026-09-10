"""Row-local RSS enclosure materialization.

RSS ingest discovers enclosure pointers. This module is the separate
materialization path: direct media URL -> blob envelope on the same source row.
"""

from __future__ import annotations

import mimetypes
from functools import partial
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import httpx

from frisket.ops.media_probe import probe_for_ingest
from frisket.ops.egress_policy import (
    STRICT_POLICY,
    EgressRefused,
    MediaEgressPolicy,
    media_egress_policy,
)
from frisket.redaction import redact_text
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore, media_cell

MAX_ENCLOSURE_BYTES = 512 * 1024 * 1024
ENCLOSURE_DOWNLOAD_KIND = "enclosure.download"

DownloadFn = Callable[[str], tuple[bytes, str, str, str | None]]


def download_url(
    url: str,
    *,
    timeout: float = 30.0,
    max_bytes: int = MAX_ENCLOSURE_BYTES,
    policy: MediaEgressPolicy | None = None,
) -> tuple[bytes, str, str, str | None]:
    """Fetch a direct enclosure URL without following unsafe redirects.

    This is intentionally for direct audio/video/file URLs, not watch pages
    such as YouTube. ``httpx`` keeps the future proxy/env hook straightforward;
    provider-specific proxy policy belongs to the yt-dlp path, not here.

    ``policy`` decides whether private/loopback hosts are reachable (URLs
    that legitimately name a LAN service); callers with a project resolve it
    via ``media_egress_policy``, and the default refuses them. Link-local and
    instance-metadata addresses are refused under every policy, on the
    initial URL and on every redirect target.
    """
    if policy is None:
        policy = STRICT_POLICY
    check_hop = policy.hop_validator()
    try:
        policy.check_url(url)
    except EgressRefused as exc:
        return b"", "", "", str(exc)
    seen = 0
    current = url
    try:
        with httpx.Client(follow_redirects=False, timeout=timeout) as client:
            while True:
                with client.stream(
                    "GET",
                    current,
                    headers={"User-Agent": "frisket/enclosure-download"},
                ) as resp:
                    if resp.is_redirect:
                        seen += 1
                        if seen > 5:
                            return b"", "", "", "too many redirects"
                        nxt = str(resp.next_request.url)
                        try:
                            check_hop(nxt)
                        except EgressRefused as exc:
                            return b"", "", "", str(exc)
                        current = nxt
                        continue
                    if resp.status_code >= 400:
                        return b"", "", "", f"HTTP {resp.status_code}"
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            return b"", "", "", "file too large"
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    mime = (
                        resp.headers.get("content-type", "").split(";", 1)[0].strip()
                        or "application/octet-stream"
                    )
                    filename = _filename_for(resp, current, mime)
                    return data, mime, filename, None
    except Exception as exc:  # noqa: BLE001 - errors become row state
        return b"", "", "", f"{type(exc).__name__}: {exc}"


def materialize_enclosure_row(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    force: bool = False,
    fetch: DownloadFn | None = None,
    expected_url: str | None = None,
) -> dict[str, Any]:
    """Download one row's enclosure URL into that row's ``media`` cell.

    Network/download failures are persisted as row state and returned as
    ``status='error'``. Structural errors (unknown row, missing enclosure URL)
    raise ``ValueError`` because the caller addressed the wrong thing.
    """
    _require_row(project, sheet_id, row_id)
    cols = _column_ids(project, sheet_id)
    enclosure_col = cols.get("enclosure_url")
    if enclosure_col is None:
        raise ValueError("row has no enclosure_url column")
    url = project.get_values(sheet_id, enclosure_col, row_ids=[row_id]).get(row_id)
    if not url:
        raise ValueError("row has no enclosure_url")
    if expected_url is not None and url != expected_url:
        raise ValueError("enclosure URL changed after admission")

    media_col = _ensure_column(
        project,
        sheet_id,
        cols,
        "media",
        _media_kind(_declared_mime(project, sheet_id, cols, row_id)),
    )
    current_media = project.get_values(sheet_id, media_col, row_ids=[row_id]).get(
        row_id
    )
    if isinstance(current_media, dict) and current_media.get("blob") and not force:
        return {
            "status": "already_downloaded",
            "row_id": row_id,
            "media": current_media,
        }

    if fetch is None:
        # The enclosure job path fetches under the same effective policy as
        # every other media download — project setting plus server floor.
        fetch = partial(download_url, policy=media_egress_policy(project))
    data, mime, filename, err = fetch(str(url))
    _require_row(project, sheet_id, row_id)
    current_url = project.get_values(sheet_id, enclosure_col, row_ids=[row_id]).get(
        row_id
    )
    if current_url != url:
        raise ValueError("enclosure URL changed during download")
    if err is not None:
        safe_error = redact_text(err)
        op_id = _apply_row_state(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            status="error",
            error=safe_error,
            expected_url=str(url),
        )
        return {
            "status": "error",
            "row_id": row_id,
            "error": safe_error,
            "op_id": op_id,
        }

    declared_mime = _declared_mime(project, sheet_id, cols, row_id)
    if _generic_mime(mime) and declared_mime:
        mime = declared_mime
    digest = project.add_blob(
        data,
        filename=filename,
        mime=mime,
        source_url=str(url),
    )
    with project.materialize_blob(digest) as path:
        metadata = probe_for_ingest(
            Path(path),
            mime=mime,
            filename=filename,
            digest=digest,
        )
    MediaBlobStore(project).replace_probe_metadata(digest, metadata)
    cell = media_cell(digest, mime=mime, filename=filename)
    op_id = _apply_row_state(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        media=cell,
        status="downloaded",
        error=None,
        expected_url=str(url),
    )
    return {"status": "downloaded", "row_id": row_id, "media": cell, "op_id": op_id}


def mark_enclosure_queued(project: Project, *, sheet_id: int, row_id: int) -> int:
    _require_row(project, sheet_id, row_id)
    return _apply_row_state(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        status="queued",
        error=None,
    )


def _apply_row_state(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    media: dict[str, Any] | None = None,
    status: str,
    error: str | None,
    expected_url: str | None = None,
) -> int:
    cols = _column_ids(project, sheet_id)
    edits: list[dict[str, Any]] = []
    if media is not None:
        media_type = _media_kind(media.get("mime"))
        media_col = _ensure_column(project, sheet_id, cols, "media", media_type)
        edits.append({"row_id": row_id, "column_id": media_col, "value": media})
    status_col = _ensure_column(project, sheet_id, cols, "media_status", "category")
    edits.append({"row_id": row_id, "column_id": status_col, "value": status})
    if error is not None or "media_error" in cols:
        error_col = _ensure_column(project, sheet_id, cols, "media_error", "text")
        edits.append({"row_id": row_id, "column_id": error_col, "value": error})
    if expected_url is not None:
        _require_row(project, sheet_id, row_id)
        url_column = cols.get("enclosure_url")
        current = (
            project.get_values(sheet_id, url_column, row_ids=[row_id]).get(row_id)
            if url_column is not None
            else None
        )
        if current != expected_url:
            raise ValueError("enclosure URL changed before publication")
    return project.apply_edits(edits, label=f"enclosure {status}")


def _require_row(project: Project, sheet_id: int, row_id: int) -> None:
    row = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? AND id=? AND hidden=0",
        (sheet_id, row_id),
    ).fetchone()
    if row is None:
        raise ValueError("row not found on sheet")


def _column_ids(project: Project, sheet_id: int) -> dict[str, int]:
    return {c["name"]: c["id"] for c in project.columns(sheet_id)}


def _ensure_column(
    project: Project,
    sheet_id: int,
    cols: dict[str, int],
    name: str,
    ctype: str,
) -> int:
    if name not in cols:
        cols[name] = project.add_column(sheet_id, name, type=ctype)
    return cols[name]


def _declared_mime(
    project: Project,
    sheet_id: int,
    cols: dict[str, int],
    row_id: int,
) -> str | None:
    col_id = cols.get("enclosure_mime")
    if col_id is None:
        return None
    value = project.get_values(sheet_id, col_id, row_ids=[row_id]).get(row_id)
    return str(value) if value else None


def _media_kind(mime: Any) -> str:
    mime_s = str(mime or "").lower()
    if mime_s.startswith("audio/"):
        return "audio"
    if mime_s.startswith("video/"):
        return "video"
    return "file"


def _generic_mime(mime: str | None) -> bool:
    return (
        not mime or mime == "application/octet-stream" or mime == "binary/octet-stream"
    )


def _filename_for(resp: httpx.Response, url: str, mime: str) -> str:
    disposition = resp.headers.get("content-disposition", "")
    parts = [part.strip() for part in disposition.split(";")]
    for part in parts:
        if part.lower().startswith("filename*="):
            raw = part.split("=", 1)[1].strip("\"'")
            if "''" in raw:
                raw = raw.split("''", 1)[1]
            return unquote(raw) or "download"
    for part in parts:
        part = part.strip()
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip("\"'") or "download"
    path = unquote(urlparse(url).path)
    filename = path.rsplit("/", 1)[-1] or urlparse(url).netloc or "download"
    if "." not in Path(filename).name:
        ext = mimetypes.guess_extension(mime)
        if ext:
            filename = f"{filename}{ext}"
    return filename
