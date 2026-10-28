from __future__ import annotations

import sys
from contextlib import closing
from types import SimpleNamespace

import pytest

from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.server.sources.runtime import (
    SourcePollContext,
    get_source_poller,
    register_source_poller,
)
from frisket.server.sources.tiktok import TikTokProfilePoller, list_tiktok_profile


def source(url="https://www.tiktok.com/@example", **config):
    return {
        "kind": "tiktok_profile",
        "name": "TikTok profile: tiktok.com/@example",
        "url": url,
        "config": config,
    }


def entry(id="1234567890", **extra):
    return {
        "id": id,
        "title": "A report",
        "description": "The report",
        "timestamp": 1781913600,
        "channel": "Example newsroom",
        "uploader": "example",
        "duration": 12,
        **extra,
    }


@pytest.mark.parametrize(
    "url",
    [
        "@example",
        "https://tiktok.com/@example/",
        "https://www.tiktok.com/@example?lang=en",
    ],
)
def test_profile_references_normalize_and_limits_reach_provider(url):
    calls = []

    def provider(url, *, item_cap):
        calls.append((url, item_cap))
        return {"title": "example", "entries": [entry(), entry("1234567891")]}

    result = TikTokProfilePoller(provider).poll(
        SourcePollContext(source(url, max_items_per_poll=1), None)
    )
    assert calls == [("https://www.tiktok.com/@example", 1)]
    assert len(result.items) == 1
    assert result.items[0].url == "https://www.tiktok.com/@example/video/1234567890"
    assert result.items[0].row["published_at"] == "2026-06-20T00:00:00Z"
    assert result.summary["truncated"]
    assert result.provider_use[0]["download"] is False


@pytest.mark.parametrize(
    "url",
    [
        "https://tiktok.com/@example/video/123",
        "https://tiktok.com.evil.test/@example",
        "https://user:secret@tiktok.com/@example",
        "https://tiktok.com:443/@example",
        "https://vm.tiktok.com/short",
        "file:///tmp/file",
        "",
    ],
)
def test_non_profiles_are_rejected(url):
    assert TikTokProfilePoller().validate_config(source(url))


@pytest.mark.parametrize("limit", [True, 0, -1, "5"])
def test_invalid_limits_rejected(limit):
    assert TikTokProfilePoller().validate_config(source(max_items_per_poll=limit))


def test_provider_requests_flat_metadata_without_downloading(monkeypatch):
    options = []

    class YDL:
        def __init__(self, opts):
            options.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, *, download):
            assert download is False

            def entries():
                yield entry()
                raise AssertionError("must not enumerate beyond requested item limit")

            return {"entries": entries()}

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=YDL))
    result = list_tiktok_profile("https://www.tiktok.com/@example", item_cap=1)
    assert len(result["entries"]) == 1
    assert options[0]["extract_flat"] is True
    assert options[0]["skip_download"] is True
    assert options[0]["ignoreerrors"] is False
    assert options[0]["playlistend"] == 1


@pytest.mark.parametrize("listing", [None, {}, {"entries": None}])
def test_failed_listing_is_not_an_empty_success(listing):
    with pytest.raises(RuntimeError, match="did not return a profile listing"):
        TikTokProfilePoller(lambda *a, **kw: listing).poll(
            SourcePollContext(source(), None)
        )


def test_provider_errors_are_redacted_and_empty_profile_is_valid():
    def fail(*args, **kwargs):
        raise RuntimeError("cookie=secret token=hidden unavailable")

    with pytest.raises(RuntimeError) as error:
        TikTokProfilePoller(fail).poll(SourcePollContext(source(), None))
    assert "secret" not in str(error.value)
    assert "hidden" not in str(error.value)
    result = TikTokProfilePoller(lambda *a, **kw: {"entries": []}).poll(
        SourcePollContext(source(), None)
    )
    assert result.items == []


def test_poll_materializes_and_deduplicates_through_normal_source_host(tmp_path):
    previous = get_source_poller("tiktok_profile")
    listings = iter(
        [
            {"title": "Example newsroom", "entries": [entry(), entry()]},
            {"title": "Example newsroom", "entries": [entry(), entry("1234567891")]},
        ]
    )
    register_source_poller(
        TikTokProfilePoller(lambda *a, **kw: next(listings)), replace=True
    )
    try:
        with closing(Project.create(tmp_path / "tiktok")) as project:

            def poll(ref, key):
                result = run_action_spec(
                    project,
                    {
                        "action_id": "source.poll",
                        "scope": {"kind": "project"},
                        "params": {"source": ref},
                        "idempotency_key": key,
                    },
                    project_id="tiktok-test",
                )
                assert result.status == "completed", result
                return result

            first = poll(source(), "tiktok-first")
            source_id = next(
                output.ref["source_id"]
                for output in first.outputs
                if output.kind == "source"
            )
            sheet = next(
                output.sheet_id for output in first.outputs if output.kind == "sheet"
            )
            assert len(project.visible_row_ids(sheet)) == 1
            second = poll(source_id, "tiktok-second")
            assert (
                next(
                    output.sheet_id
                    for output in second.outputs
                    if output.kind == "sheet"
                )
                == sheet
            )
            assert len(project.visible_row_ids(sheet)) == 2
            columns = {c["name"]: c for c in project.columns(sheet)}
            assert list(
                project.get_values(sheet, columns["video_id"]["id"]).values()
            ) == ["1234567890", "1234567891"]
            assert columns["source_url"]["default_hidden"] == 0
            assert columns["video_id"]["default_hidden"] == 1
            assert (
                project.db.execute(
                    "SELECT name FROM sources WHERE id=?", (source_id,)
                ).fetchone()[0]
                == "Example newsroom"
            )
    finally:
        assert previous is not None
        register_source_poller(previous, replace=True)


def test_flat_listing_reports_actual_item_progress_and_honors_stop(monkeypatch):
    import yt_dlp

    def extract_info(ydl, url, *, download):
        # Exercise yt-dlp's real flat playlist processing with an offline listing.
        return ydl.process_ie_result(
            {
                "_type": "playlist",
                "id": "profile",
                "title": "Example",
                "extractor": "TikTokUser",
                "extractor_key": "TikTokUser",
                "webpage_url": url,
                "entries": [
                    {
                        **entry(str(i)),
                        "_type": "url",
                        "ie_key": "TikTok",
                        "url": f"https://www.tiktok.com/@example/video/{i}",
                    }
                    for i in range(100, 104)
                ],
            },
            download=download,
        )

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    progress = []
    poller = TikTokProfilePoller()
    result = poller.poll(
        SourcePollContext(
            source(max_items_per_poll=2),
            None,
            on_progress=lambda current, total: progress.append((current, total)),
        )
    )
    assert progress == [(1, 2), (2, 2)]
    assert len(result.items) == 2
    with pytest.raises(RuntimeError, match="cancelled"):
        poller.poll(SourcePollContext(source(), None, should_cancel=lambda: True))
