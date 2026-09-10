"""RSS source INGEST tests (frisket.ingest) — no network.

feedparser runs on FIXTURE feed text; the SSRF-guarded fetch is monkeypatched so
the guarded path is proven to be the one used and fixtures stand in for network.
Covers: dedup across three overlapping polls, item-update revision surfacing, op
logging, cursor persistence, column creation on first ingest, and the SSRF guard.
"""

from __future__ import annotations

import json

import pytest

from frisket import ingest
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore


def _feed(items: list[dict], feed_title: str = "Test Feed") -> str:
    """Build an RSS 2.0 document from item dicts (guid/title/link/pubDate/desc)."""
    entries = []
    for it in items:
        parts = []
        if "title" in it:
            parts.append(f"<title>{it['title']}</title>")
        if "link" in it:
            parts.append(f"<link>{it['link']}</link>")
        if "guid" in it:
            parts.append(f"<guid isPermaLink='false'>{it['guid']}</guid>")
        if "pubDate" in it:
            parts.append(f"<pubDate>{it['pubDate']}</pubDate>")
        if "description" in it:
            parts.append(f"<description>{it['description']}</description>")
        if "enclosure" in it:
            enc = it["enclosure"]
            attrs = f"url='{enc['url']}'"
            if "length" in enc:
                attrs += f" length='{enc['length']}'"
            if "type" in enc:
                attrs += f" type='{enc['type']}'"
            parts.append(f"<enclosure {attrs} />")
        entries.append("<item>" + "".join(parts) + "</item>")
    return (
        "<?xml version='1.0'?><rss version='2.0'><channel>"
        f"<title>{feed_title}</title>" + "".join(entries) + "</channel></rss>"
    )


@pytest.fixture
def project(tmp_path):
    return Project.create(tmp_path / "proj", name="rss-test")


def _add_rss_source(project: Project, url="https://example.com/feed.xml", **cfg):
    sid = SourceStore(project).add_source(
        name="My Feed", kind="rss", url=url, config=cfg
    )
    return _src(project, sid)


def _src(project: Project, sid: int) -> dict:
    """Source row as the dict shape ingest_rss consumes (config parsed)."""
    row = dict(SourceStore(project).get_source(sid))
    row["config"] = json.loads(row.get("config") or "{}")
    return row


def _rows(project: Project, sheet_id: int) -> list[dict]:
    cols = {c["id"]: c["name"] for c in project.columns(sheet_id, include_hidden=True)}
    out = []
    for r in project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
    ):
        cells = project.db.execute(
            "SELECT column_id, value FROM cells WHERE row_id=?", (r["id"],)
        ).fetchall()
        out.append({cols[c["column_id"]]: json.loads(c["value"]) for c in cells})
    return out


# --------------------------------------------------------------------------
# dedup across three polls with overlapping items
# --------------------------------------------------------------------------
def test_dedup_three_overlapping_polls(project):
    src = _add_rss_source(project)

    poll1 = _feed(
        [
            {"guid": "a", "title": "Alpha", "link": "http://x/a"},
            {"guid": "b", "title": "Bravo", "link": "http://x/b"},
        ]
    )
    poll2 = _feed(
        [
            {"guid": "c", "title": "Charlie", "link": "http://x/c"},
            {"guid": "a", "title": "Alpha", "link": "http://x/a"},  # re-served
            {"guid": "b", "title": "Bravo", "link": "http://x/b"},  # re-served
        ]
    )
    poll3 = _feed(
        [
            {"guid": "d", "title": "Delta", "link": "http://x/d"},
            {"guid": "c", "title": "Charlie", "link": "http://x/c"},  # re-served
        ]
    )

    s1 = ingest.ingest_rss(project, src, fetch=lambda u: poll1)
    sheet_id = s1["sheet_id"]
    assert s1["new_rows"] == 2

    # re-read source (cursor advanced) before each subsequent poll
    src = _src(project, src["id"])
    s2 = ingest.ingest_rss(project, src, fetch=lambda u: poll2)
    assert s2["new_rows"] == 1  # only 'c' is new

    src = _src(project, src["id"])
    s3 = ingest.ingest_rss(project, src, fetch=lambda u: poll3)
    assert s3["new_rows"] == 1  # only 'd' is new

    rows = _rows(project, sheet_id)
    assert [r["guid"] for r in rows] == ["a", "b", "c", "d"]


# --------------------------------------------------------------------------
# item-update revision surfacing (correction, not overwrite)
# --------------------------------------------------------------------------
def test_item_update_surfaces_revision(project):
    src = _add_rss_source(project)
    poll1 = _feed([{"guid": "a", "title": "Mayor wins", "link": "http://x/a"}])
    # same guid, corrected title
    poll2 = _feed(
        [{"guid": "a", "title": "CORRECTION: Mayor loses", "link": "http://x/a"}]
    )

    s1 = ingest.ingest_rss(project, src, fetch=lambda u: poll1)
    sheet_id = s1["sheet_id"]
    assert s1["new_rows"] == 1 and s1["revisions"] == 0

    src = _src(project, src["id"])
    s2 = ingest.ingest_rss(project, src, fetch=lambda u: poll2)
    assert s2["new_rows"] == 0
    assert s2["revisions"] == 1

    rows = _rows(project, sheet_id)
    assert len(rows) == 2  # original KEPT, correction appended
    assert rows[0]["title"] == "Mayor wins"
    assert rows[0].get("_revises") is None  # original is not a revision
    assert rows[1]["title"] == "CORRECTION: Mayor loses"
    assert rows[1]["_revises"] == "a"
    assert rows[1]["_revision"] == 1

    # a third poll with a further correction bumps the revision counter
    poll3 = _feed([{"guid": "a", "title": "Re-correction", "link": "http://x/a"}])
    src = _src(project, src["id"])
    s3 = ingest.ingest_rss(project, src, fetch=lambda u: poll3)
    assert s3["revisions"] == 1
    rows = _rows(project, sheet_id)
    assert rows[2]["_revision"] == 2


# --------------------------------------------------------------------------
# op logging — each poll's inserts are one logged op
# --------------------------------------------------------------------------
def test_op_logged_per_poll(project):
    src = _add_rss_source(project)
    before = len(project.history())
    poll = _feed(
        [
            {"guid": "a", "title": "A", "link": "http://x/a"},
            {"guid": "b", "title": "B", "link": "http://x/b"},
        ]
    )
    s = ingest.ingest_rss(project, src, fetch=lambda u: poll)
    history = project.history()
    assert len(history) == before + 1
    op = history[-1]
    assert op["kind"] == "ingest"
    assert op["id"] == s["op_id"]
    spec = json.loads(op["spec"])
    assert spec["new_rows"] == 2
    assert spec["source_id"] == src["id"]

    # a poll that lands NOTHING new logs no op (but still a source_run)
    src = _src(project, src["id"])
    runs_before = len(SourceStore(project).source_runs(src["id"]))
    s2 = ingest.ingest_rss(project, src, fetch=lambda u: poll)
    assert s2["new_rows"] == 0 and s2["op_id"] is None
    assert len(project.history()) == before + 1  # unchanged
    assert len(SourceStore(project).source_runs(src["id"])) == runs_before + 1


# --------------------------------------------------------------------------
# cursor persistence — dedup state survives across calls via sources.cursor
# --------------------------------------------------------------------------
def test_cursor_persisted(project):
    src = _add_rss_source(project)
    poll = _feed([{"guid": "a", "title": "A", "link": "http://x/a"}])
    ingest.ingest_rss(project, src, fetch=lambda u: poll)

    row = SourceStore(project).get_source(src["id"])
    cursor = json.loads(row["cursor"])
    assert "a" in cursor  # dedup key persisted
    assert isinstance(cursor["a"], str)  # content fingerprint

    # a brand-new Project object on the same db must still dedup (no in-memory
    # state) — prove the cursor (not RAM) is the source of truth
    reopened = Project(project.path)
    src2 = _src(reopened, src["id"])
    s = ingest.ingest_rss(reopened, src2, fetch=lambda u: poll)
    assert s["new_rows"] == 0


# --------------------------------------------------------------------------
# columns created on first ingest
# --------------------------------------------------------------------------
def test_columns_created_on_first_ingest(project):
    src = _add_rss_source(project)
    poll = _feed(
        [
            {
                "guid": "a",
                "title": "Title A",
                "link": "http://x/a",
                "pubDate": "Mon, 01 Jun 2026 12:00:00 GMT",
                "description": "Some **summary** body",
            }
        ]
    )
    s = ingest.ingest_rss(project, src, fetch=lambda u: poll)
    cols = {c["name"]: c["type"] for c in project.columns(s["sheet_id"])}
    assert set(cols) >= {"guid", "title", "link", "published", "summary"}
    assert cols["link"] == "link"
    assert cols["summary"] == "text"

    rows = _rows(project, s["sheet_id"])
    assert rows[0]["title"] == "Title A"
    assert rows[0]["link"] == "http://x/a"
    assert "summary" in rows[0]["summary"]


# --------------------------------------------------------------------------
# SSRF guard is applied (monkeypatch to prove the guarded path is used)
# --------------------------------------------------------------------------
def test_ssrf_guard_blocks_private(project, monkeypatch):
    # point the guard at a verdict we control, proving fetch_feed_text routes
    # through netguard.url_is_safe before any I/O
    calls = {}

    def fake_url_is_safe(url):
        calls["url"] = url
        return False

    monkeypatch.setattr(ingest, "url_is_safe", fake_url_is_safe)
    with pytest.raises(ingest.IngestError, match="blocked URL"):
        ingest.fetch_feed_text("http://169.254.169.254/latest/meta-data/")
    assert calls["url"] == "http://169.254.169.254/latest/meta-data/"


def test_fetch_feed_text_rejects_oversized_response(monkeypatch):
    import httpx

    monkeypatch.setattr(ingest, "MAX_FEED_BYTES", 4)
    monkeypatch.setattr(ingest, "url_is_safe", lambda url: True)

    class FakeResponse:
        is_redirect = False
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_bytes(self):
            yield b"abc"
            yield b"def"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://example.com/feed.xml"
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    with pytest.raises(ingest.IngestError, match="exceeded 4 bytes"):
        ingest.fetch_feed_text("https://example.com/feed.xml")


def test_rss_feed_fetch_allows_podcast_sized_feed_bodies():
    assert ingest.MAX_FEED_BYTES == 50 * 1024 * 1024


def test_fetch_feed_text_rejects_http_error_response(monkeypatch):
    import httpx

    monkeypatch.setattr(ingest, "url_is_safe", lambda url: True)

    class FakeResponse:
        is_redirect = False
        status_code = 500

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_bytes(self):
            yield b"<rss />"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            assert method == "GET"
            assert url == "https://example.com/feed.xml"
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    with pytest.raises(ingest.IngestError, match="HTTP 500"):
        ingest.fetch_feed_text("https://example.com/feed.xml")


def test_ingest_uses_guarded_fetch_by_default(project, monkeypatch):
    """ingest_rss with no fetch override must call fetch_feed_text, which goes
    through the SSRF guard — prove the default path is the guarded one."""
    src = _add_rss_source(project, url="http://10.0.0.1/feed.xml")

    seen = {}

    def fake_url_is_safe(url):
        seen["url"] = url
        return False  # simulate a blocked private address

    monkeypatch.setattr(ingest, "url_is_safe", fake_url_is_safe)
    with pytest.raises(ingest.IngestError):
        ingest.ingest_rss(project, src)  # default fetch=fetch_feed_text
    assert seen["url"] == "http://10.0.0.1/feed.xml"
    # the failed poll is recorded, not silently lost
    runs = SourceStore(project).source_runs(src["id"])
    assert runs and runs[0]["status"] == "error"


def test_parse_error_records_failed_source_run_before_raising(project):
    src = _add_rss_source(project)

    with pytest.raises(ingest.IngestError, match="rss parse error"):
        ingest.ingest_rss(project, src, fetch=lambda u: "<not xml")

    runs = [dict(r) for r in SourceStore(project).source_runs(src["id"])]
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["new_rows"] == 0
    assert "rss parse error" in runs[0]["error"]
    row = SourceStore(project).get_source(src["id"])
    assert row["last_status"] == "error"
    assert row["new_rows_total"] == 0


def test_missing_url_records_failed_source_run_before_raising(project):
    """A malformed rss source is still a failed poll, not a lost exception."""
    src = _add_rss_source(project, url=None)

    with pytest.raises(ingest.IngestError, match="rss source has no url"):
        ingest.ingest_rss(project, src, fetch=lambda u: _feed([]))

    runs = [dict(r) for r in SourceStore(project).source_runs(src["id"])]
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert runs[0]["new_rows"] == 0
    assert runs[0]["error"] == "rss source has no url"
    row = SourceStore(project).get_source(src["id"])
    assert row["last_status"] == "error"
    assert row["new_rows_total"] == 0


# --------------------------------------------------------------------------
# merge_strategy: append-new implemented; sync raises a clear NotImplementedError
# --------------------------------------------------------------------------
def test_sync_merge_strategy_not_implemented(project):
    src = _add_rss_source(project, merge_strategy="sync", key_field="guid")
    with pytest.raises(NotImplementedError, match="sync"):
        ingest.ingest_rss(project, src, fetch=lambda u: _feed([]))


# --------------------------------------------------------------------------
# Media enclosures: ingest discovers pointers and metadata but never downloads
# their bytes during feed ingestion.
# Columns: enclosure_url(link) distinct from link(page), enclosure_mime(text),
# enclosure_size(integer), enclosures(json), media(audio|video|file, NULL at
# ingest), media_status(category='remote'). No media bytes fetched here.
# --------------------------------------------------------------------------
def test_audio_enclosure_captured_into_typed_media_columns(project):
    src = _add_rss_source(project)
    poll = _feed(
        [
            {
                "guid": "ep1",
                "title": "Episode 1",
                "link": "https://pod.example/ep1",  # show-notes page
                "enclosure": {
                    "url": "https://cdn.example/ep1.mp3",
                    "length": "12842257",
                    "type": "audio/mpeg",
                },
            }
        ]
    )
    s = ingest.ingest_rss(project, src, fetch=lambda u: poll)
    cols = {c["name"]: c["type"] for c in project.columns(s["sheet_id"])}
    # the media-pointer columns exist with the right types
    assert cols.get("enclosure_url") == "link"
    assert cols.get("enclosure_mime") == "text"
    assert cols.get("enclosure_size") == "integer"
    assert cols.get("enclosures") == "json"
    assert cols.get("media_status") == "category"
    # the (still empty) blob column is typed from the enclosure MIME
    assert cols.get("media") == "audio"

    row = _rows(project, s["sheet_id"])[0]
    # enclosure_url is the MEDIA asset, kept DISTINCT from link (the page)
    assert row["link"] == "https://pod.example/ep1"
    assert row["enclosure_url"] == "https://cdn.example/ep1.mp3"
    assert row["enclosure_mime"] == "audio/mpeg"
    assert row["enclosure_size"] == 12842257
    assert row["media_status"] == "remote"
    # the full list is kept for multi-enclosure feeds
    assert isinstance(row["enclosures"], list) and row["enclosures"]
    # ingest does NOT download — the media (blob) cell is empty
    assert row.get("media") in (None, "")


def test_video_enclosure_types_media_column_video(project):
    src = _add_rss_source(project)
    poll = _feed(
        [
            {
                "guid": "v1",
                "title": "Clip",
                "link": "https://vid.example/v1",
                "enclosure": {"url": "https://cdn.example/v1.mp4", "type": "video/mp4"},
            }
        ]
    )
    s = ingest.ingest_rss(project, src, fetch=lambda u: poll)
    cols = {c["name"]: c["type"] for c in project.columns(s["sheet_id"])}
    assert cols.get("media") == "video"
    row = _rows(project, s["sheet_id"])[0]
    assert row["enclosure_url"] == "https://cdn.example/v1.mp4"
    assert row["media_status"] == "remote"


def test_text_feed_without_enclosures_is_unaffected(project):
    """A plain news item adds no media columns (no regression for text feeds)."""
    src = _add_rss_source(project)
    poll = _feed([{"guid": "a", "title": "Plain news", "link": "http://x/a"}])
    s = ingest.ingest_rss(project, src, fetch=lambda u: poll)
    cols = {c["name"] for c in project.columns(s["sheet_id"])}
    assert "enclosure_url" not in cols
    assert "media" not in cols
    assert cols >= {"guid", "title", "link", "published", "summary"}


def test_link_pubdate_fallback_key_when_no_guid(project):
    """Items without <guid> dedup on link+pubdate."""
    src = _add_rss_source(project)
    item = {
        "title": "No guid here",
        "link": "http://x/noguid",
        "pubDate": "Mon, 01 Jun 2026 12:00:00 GMT",
    }
    poll = _feed([item])
    s1 = ingest.ingest_rss(project, src, fetch=lambda u: poll)
    assert s1["new_rows"] == 1
    src = _src(project, src["id"])
    s2 = ingest.ingest_rss(project, src, fetch=lambda u: poll)
    assert s2["new_rows"] == 0  # same link+pubdate => deduped
