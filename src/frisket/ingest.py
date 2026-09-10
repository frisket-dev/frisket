from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Callable

from frisket.ops.netguard import url_is_safe
from frisket.engine.store.cell_writes import create_base_cell_producer
from frisket.engine.store.sources import SourceStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from frisket.engine.store.project import Project

# Feed item -> sheet column mapping. Order is the column order created on first
# ingest. `_revision` / `_revises` are provenance columns added lazily the first
# time an item correction is observed.
FEED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("guid", "text"),
    ("title", "text"),
    ("link", "link"),
    ("published", "text"),
    ("summary", "text"),
)
ENCLOSURE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("enclosure_url", "link"),
    ("enclosure_mime", "text"),
    ("enclosure_size", "integer"),
    ("enclosures", "json"),
    ("media_status", "category"),
)
REVISION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("_revision", "integer"),  # 1, 2, ... for each correction of an item
    ("_revises", "text"),  # the guid this row is a corrected re-issue of
)


class IngestError(Exception):
    """A fetch/parse failure recorded against the source run (row never lost)."""


MAX_FEED_BYTES = 50 * 1024 * 1024


def fetch_feed_text(url: str, *, timeout: float = 20.0) -> str:
    """Fetch a feed URL's text through the SSRF guard.

    The scheduler poll path is synchronous, so this cannot use the async
    ``netguard.safe_request``; it keeps the weaker entry-filter contract
    ``egress_policy`` documents (``url_is_safe`` on entry and on every redirect
    hop, but no connection pinning, so a name that rebinds between check and
    connect is not stopped here — container-level egress control remains the
    hard boundary for this path). Tests monkeypatch THIS function to prove the
    guarded path is the one used and to feed fixture text without network.
    """
    import httpx

    if not url_is_safe(url):
        raise IngestError(f"blocked URL (private/loopback/metadata): {url}")
    seen = 0
    current = url
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        while True:
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    seen += 1
                    if seen > 5:
                        raise IngestError("too many redirects")
                    nxt = str(resp.next_request.url)
                    if not url_is_safe(nxt):
                        raise IngestError(f"blocked redirect target: {nxt}")
                    current = nxt
                    continue
                if resp.status_code >= 400:
                    raise IngestError(f"HTTP {resp.status_code}")
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_FEED_BYTES:
                        raise IngestError(
                            f"feed response exceeded {MAX_FEED_BYTES} bytes"
                        )
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")


def _item_key(entry: Any) -> str:
    """A stable dedup key for a feed item: <guid> if present, else a hash of
    link + published when the guid is absent."""
    guid = getattr(entry, "id", None) or entry.get("id") or entry.get("guid")
    if guid:
        return str(guid)
    link = entry.get("link") or ""
    published = entry.get("published") or entry.get("updated") or ""
    return "lp:" + hashlib.sha256(f"{link}\n{published}".encode()).hexdigest()[:32]


def _content_fingerprint(rec: dict[str, Any]) -> str:
    """A hash of the item's user-visible content, to detect a correction (the
    same key re-served with edited title/summary/link)."""
    blob = "\n".join(
        str(rec.get(k, ""))
        for k in (
            "title",
            "link",
            "published",
            "summary",
            "enclosure_url",
            "enclosure_mime",
            "enclosure_size",
            "enclosures",
        )
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _map_entry(entry: Any, key: str) -> dict[str, Any]:
    """Map a feedparser entry to our column record."""
    # summary/content: prefer the richer content[] body, fall back to summary
    summary = entry.get("summary") or ""
    content = entry.get("content")
    if content:
        try:
            summary = content[0].get("value") or summary
        except (AttributeError, IndexError, KeyError):
            pass
    rec: dict[str, Any] = {
        "guid": key,
        "title": entry.get("title") or "",
        "link": entry.get("link") or "",
        "published": entry.get("published") or entry.get("updated") or "",
        "summary": summary,
    }
    enclosures = _entry_enclosures(entry)
    primary = _primary_enclosure(enclosures)
    if primary:
        rec.update(
            {
                "enclosure_url": primary["url"],
                "enclosure_mime": primary.get("mime") or "",
                "enclosure_size": primary.get("size"),
                "enclosures": enclosures,
                "media_status": "remote",
            }
        )
    return rec


def _entry_enclosures(entry: Any) -> list[dict[str, Any]]:
    """Extract enclosure/media links without downloading them.

    feedparser normalizes RSS ``<enclosure>`` tags into ``entry.enclosures``
    and also keeps link dictionaries in ``entry.links``. Read both, dedupe by
    URL, and keep only pointer metadata.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(entry.get("enclosures") or []) + [
        link for link in (entry.get("links") or []) if link.get("rel") == "enclosure"
    ]:
        href = raw.get("href") or raw.get("url")
        if not href or href in seen:
            continue
        seen.add(href)
        item: dict[str, Any] = {"url": str(href)}
        mime = raw.get("type") or raw.get("mime")
        if mime:
            item["mime"] = str(mime)
        size = _int_or_none(raw.get("length") or raw.get("size"))
        if size is not None:
            item["size"] = size
        title = raw.get("title")
        if title:
            item["title"] = str(title)
        rel = raw.get("rel")
        if rel:
            item["rel"] = str(rel)
        out.append(item)
    return out


def _primary_enclosure(enclosures: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not enclosures:
        return None
    for enc in enclosures:
        if _media_kind(enc.get("mime")) in {"audio", "video"}:
            return enc
    return enclosures[0]


def _media_kind(mime: Any) -> str:
    mime_s = str(mime or "").lower()
    if mime_s.startswith("audio/"):
        return "audio"
    if mime_s.startswith("video/"):
        return "video"
    return "file"


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_cursor(source: Any) -> dict[str, str]:
    """sources.cursor holds {dedup_key: content_fingerprint} as JSON."""
    raw = source["cursor"]
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_columns(
    project: Project, sheet_id: int, specs: tuple[tuple[str, str], ...]
) -> dict[str, int]:
    """Create any missing columns (first-ingest behaviour, mirroring import)
    and return name -> column_id for the requested specs."""
    col_ids = {c["name"]: c["id"] for c in project.columns(sheet_id)}
    for name, ctype in specs:
        if name not in col_ids:
            col_ids[name] = project.add_column(sheet_id, name, type=ctype)
    return col_ids


def _ensure_enclosure_columns(
    project: Project,
    sheet_id: int,
    records: list[dict[str, Any]],
) -> dict[str, int]:
    col_ids = _ensure_columns(project, sheet_id, ENCLOSURE_COLUMNS)
    if "media" not in col_ids:
        kinds = {
            _media_kind(rec.get("enclosure_mime"))
            for rec in records
            if rec.get("enclosure_url")
        }
        media_type = next(iter(kinds)) if len(kinds) == 1 else "file"
        col_ids["media"] = project.add_column(sheet_id, "media", type=media_type)
    project.set_column_format(col_ids["enclosure_size"], "filesize")
    return col_ids


def ingest_rss(
    project: Project,
    source: Any,
    *,
    fetch: Callable[[str], str] = fetch_feed_text,
) -> dict[str, Any]:
    """Fetch, parse, dedup, and insert NEW items from an rss source.

    Returns a summary dict ``{new_rows, revisions, op_id, sheet_id, run_id}``.
    ``fetch`` is injectable (tests pass fixture text; default is the SSRF-guarded
    fetch). On any fetch/parse error a source_run is logged with status='error'
    and the error re-raised as ``IngestError`` — the poll is never silently lost.
    """
    import feedparser

    source_id = source["id"]
    config = source["config"]
    if isinstance(config, str):
        try:
            config = json.loads(config or "{}")
        except (TypeError, ValueError):
            config = {}
    merge = (config or {}).get("merge_strategy", "append-new")
    if merge == "sync":
        raise NotImplementedError(
            "merge_strategy='sync' (keyed upsert for snapshot sources) is not "
            "implemented yet — only append-new (stream sources like RSS) is. "
            "Snapshot sources (CSV/Sheet overwritten in place) are a later kind."
        )
    if merge != "append-new":
        raise ValueError(f"unknown merge_strategy: {merge!r}")

    url = source["url"]
    if not url:
        error = "rss source has no url"
        SourceStore(project).record_source_run(
            source_id, new_rows=0, status="error", error=error
        )
        raise IngestError(error)

    # --- fetch (SSRF-guarded) + parse ---
    try:
        text = fetch(url)
    except Exception as exc:  # noqa: BLE001 - recorded against the run
        SourceStore(project).record_source_run(
            source_id, new_rows=0, status="error", error=str(exc)
        )
        raise IngestError(str(exc)) from exc

    parsed = feedparser.parse(text)
    if getattr(parsed, "bozo", False):
        exc = getattr(parsed, "bozo_exception", None)
        error = f"rss parse error: {exc}" if exc else "rss parse error"
        SourceStore(project).record_source_run(
            source_id, new_rows=0, status="error", error=error
        )
        raise IngestError(error)

    # --- target sheet: create on first ingest if unset/missing ---
    sheet_id = source["sheet_id"]
    sheet_ids = {s["id"] for s in project.sheets()}
    if sheet_id is None or sheet_id not in sheet_ids:
        title = (getattr(parsed, "feed", {}) or {}).get("title") or source["name"]
        sheet_id = project.add_sheet(str(title)[:80] or "feed")
        SourceStore(project).update_source(source_id, sheet_id=sheet_id)

    seen = _load_cursor(source)

    new_records: list[dict[str, Any]] = []
    new_keys: list[str] = []
    revision_records: list[dict[str, Any]] = []
    revised_keys: list[str] = []

    for entry in parsed.entries:
        key = _item_key(entry)
        rec = _map_entry(entry, key)
        fp = _content_fingerprint(rec)
        prior = seen.get(key)
        if prior is None:
            # genuinely new item
            new_records.append(rec)
            new_keys.append(key)
            seen[key] = fp
        elif prior != fp:
            # same key, edited content => a CORRECTION. Surface as a revision
            # row (keep the original); never overwrite in place.
            rev = dict(rec)
            rev["_revises"] = key
            revision_records.append(rev)
            revised_keys.append(key)
            seen[key] = fp
        # else: unchanged re-serve — skip (append-NEW)

    total_new = len(new_records) + len(revision_records)
    if total_new == 0:
        # still log the poll so monitoring/forecast see it
        run_id = SourceStore(project).record_source_run(
            source_id, new_rows=0, status="ok", cursor=json.dumps(seen)
        )
        return {
            "new_rows": 0,
            "revisions": 0,
            "op_id": None,
            "sheet_id": sheet_id,
            "run_id": run_id,
            "row_ids": [],
            "enclosure_row_ids": [],
        }

    # --- columns (create on first ingest, mirroring import) ---
    col_ids = _ensure_columns(project, sheet_id, FEED_COLUMNS)
    enclosure_records = [
        rec for rec in new_records + revision_records if rec.get("enclosure_url")
    ]
    if enclosure_records:
        col_ids |= _ensure_enclosure_columns(project, sheet_id, enclosure_records)
    if revision_records:
        col_ids |= _ensure_columns(project, sheet_id, REVISION_COLUMNS)
        for rec in revision_records:
            rec["_revision"] = _next_revision(project, sheet_id, col_ids, rec["guid"])

    # --- one logged op for the whole poll's inserts (undo/provenance) ---
    op_id = project.append_op(
        "ingest",
        {
            "source_id": source_id,
            "sheet": sheet_id,
            "new_rows": len(new_records),
            "revisions": len(revision_records),
            "url": url,
        },
        label=f"ingest {total_new} from {source['name']}",
        commit=False,
    )
    inserted_records = new_records + revision_records
    try:
        producer_id = create_base_cell_producer(
            project.db, stage_id=f"op:{op_id}", op_id=op_id
        )
        row_ids = project.add_rows(
            sheet_id,
            inserted_records,
            col_ids,
            producer_id=producer_id,
            commit=False,
        )
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise
    enclosure_row_ids = [
        row_id
        for row_id, rec in zip(row_ids, inserted_records, strict=True)
        if rec.get("enclosure_url")
    ]

    # --- advance bookkeeping + persist dedup cursor ---
    run_id = SourceStore(project).record_source_run(
        source_id,
        new_rows=total_new,
        status="ok",
        cursor=json.dumps(seen),
        op_id=op_id,
    )

    return {
        "new_rows": len(new_records),
        "revisions": len(revision_records),
        "op_id": op_id,
        "sheet_id": sheet_id,
        "run_id": run_id,
        "row_ids": row_ids,
        "enclosure_row_ids": enclosure_row_ids,
    }


def _next_revision(
    project: Project, sheet_id: int, col_ids: dict[str, int], guid: str
) -> int:
    """Revision number for the next correction of ``guid``: 1 + count of prior
    revision rows that revised this guid on the sheet."""
    revises_col = col_ids.get("_revises")
    if revises_col is None:
        return 1
    n = project.db.execute(
        "SELECT COUNT(*) FROM cells c JOIN rows r ON r.id = c.row_id "
        "WHERE r.sheet_id=? AND c.column_id=? AND c.value=?",
        (sheet_id, revises_col, json.dumps(guid)),
    ).fetchone()[0]
    return int(n) + 1
