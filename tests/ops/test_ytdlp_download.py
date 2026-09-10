from __future__ import annotations

import io
import asyncio
import socket
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import make_client as _client
import frisket.ops.ytdlp as ytdlp
import frisket.ops.ytdlp_outputs as ytdlp_outputs
from frisket.ops import egress_policy
from frisket.ops.base import OpContext, materialize_media_path
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.ops.ytdlp import DownloadedMedia, download_media
from http_test_helpers import drain_queue, post_v1_action_with_exact_confirmation


YOUTUBE_WATCH = "https://www.youtube.com/watch?v=fixture123"
TIKTOK_VIDEO = "https://www.tiktok.com/@creator/video/7123456789"

# Runtime receipt evidence the injected extractor stamps on its handoff so
# DownloadedMedia carries the content hash.
_RUNTIME_EVIDENCE = {
    "runtime_name": "yt-dlp",
    "resolved_version": "2099.12.31",
    "artifact_sha256": "c" * 64,
}


@pytest.fixture(autouse=True)
def _stable_public_dns_for_fixture_hosts(monkeypatch):
    """Keep transport-contract tests independent of the outer test sandbox.

    The outer test sandbox resolves public names to a link-local sink, which trips the
    production egress guard but prevents these injected/offline extractor tests
    from reaching the contract they are meant to exercise.  Preserve real
    resolution for literals and metadata names while giving only the fixture's
    public hostnames a deterministic globally routable answer.
    """

    real_getaddrinfo = socket.getaddrinfo
    fixture_hosts = {"example.com", "kick.com", "www.tiktok.com", "www.youtube.com"}

    def fixture_getaddrinfo(host, port, *args, **kwargs):
        if host in fixture_hosts:
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("8.8.8.8", port or 0),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(egress_policy.socket, "getaddrinfo", fixture_getaddrinfo)


def _make_cli_extractor(
    *,
    files: dict[str, bytes],
    info: dict[str, Any],
    recorder: dict[str, Any] | None = None,
):
    """Build a frisket.plugins.media MediaExtractor that WRITES ``files`` into the
    host scratch dir and returns their names + ``info`` metadata + runtime
    evidence — the offline stand-in for the managed-runtime CLI transport."""

    def _extract(request: dict[str, Any], work_dir: Path) -> dict[str, Any]:
        if recorder is not None:
            recorder["request"] = request
            recorder["work_dir"] = Path(work_dir)
        media_name = ""
        sidecars: list[str] = []
        for name, data in files.items():
            (Path(work_dir) / name).write_bytes(data)
            is_sidecar = name.endswith(
                (
                    ".vtt",
                    ".srt",
                    ".srv2",
                    ".lrc",
                    ".json",
                    ".webp",
                    ".jpg",
                    ".png",
                    ".mhtml",
                    ".info.json",
                )
            )
            if is_sidecar:
                sidecars.append(name)
            elif not media_name:
                media_name = name
        return {
            "media_filename": media_name,
            "sidecar_filenames": sidecars,
            "metadata": {"webpage_url": request.get("url"), **info},
            "runtime_evidence": dict(_RUNTIME_EVIDENCE),
        }

    return _extract


def _project(client: TestClient):
    pid = client.post("/api/projects", json={"name": "YouTube"}).json()["id"]
    return pid, client.app.state.workspace.get(pid)


def _wav_bytes(seconds: float = 1.0, sample_rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    frames = int(seconds * sample_rate)
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def _fake_downloaded_media() -> DownloadedMedia:
    return DownloadedMedia(
        data=_wav_bytes(seconds=1.25),
        mime="audio/wav",
        filename="fixture.wav",
        duration_seconds=1.25,
        metadata={"title": "Fixture video", "duration_seconds": 1.25},
    )


def _cell(client: TestClient, pid: str, sheet_id: int, row_id: int, column: str):
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    col = next(c for c in data["columns"] if c["name"] == column)
    row = next(r for r in data["rows"] if r["id"] == row_id)
    return row["cells"][str(col["id"])]


def test_ytdlp_contract_downloads_one_watch_url_through_managed_runtime():
    recorder: dict[str, Any] = {}
    extractor = _make_cli_extractor(
        files={"fixture123.webm": b"fake webm video"},
        info={
            "yt_dlp_id": "fixture123",
            "title": "Fixture video",
            "duration_seconds": 12.5,
            "extractor": "Youtube",
        },
        recorder=recorder,
    )

    result = download_media(YOUTUBE_WATCH, extractor=extractor)

    # Generic media acquisition defaults to video, matching the action contract.
    assert recorder["request"]["url"] == YOUTUBE_WATCH
    assert recorder["request"]["format_selector"] == "bestvideo*+bestaudio/best"
    assert isinstance(recorder["work_dir"], Path)
    # The media bytes were read back from the scratch file the transport wrote.
    assert result.data == b"fake webm video"
    assert result.mime == "video/webm"
    assert result.filename == "fixture123.webm"
    assert result.duration_seconds == 12.5
    assert result.metadata["title"] == "Fixture video"
    # The resolved runtime's content-hash receipt evidence rides with the media.
    assert result.runtime_evidence == _RUNTIME_EVIDENCE
    assert result.metadata["managed_runtime"] == _RUNTIME_EVIDENCE


def test_ytdlp_contract_accepts_recognized_tiktok_video_url():
    extractor = _make_cli_extractor(
        files={"7123456789.mp4": b"fake tiktok video"},
        info={
            "yt_dlp_id": "7123456789",
            "title": "Fixture TikTok",
            "extractor": "TikTok",
        },
    )

    result = download_media(TIKTOK_VIDEO, media_type="video", extractor=extractor)

    assert result.data == b"fake tiktok video"
    assert result.metadata["provider"] == "tiktok"
    assert result.metadata["extractor"] == "TikTok"


def test_ytdlp_contract_accepts_recognized_kick_vod_url():
    extractor = _make_cli_extractor(
        files={"fixture.mp4": b"generic media"},
        info={
            "yt_dlp_id": "fixture",
            "title": "Generic fixture",
            "extractor": "Kick",
        },
    )

    result = download_media(
        "https://kick.com/xqc/videos/5c697a87-afce-4256-b01f-3c8fe71ef5cb",
        media_type="video",
        extractor=extractor,
    )

    assert result.data == b"generic media"
    assert result.metadata["provider"] == "kick"


@pytest.mark.parametrize(
    "url",
    [
        "not a URL",
        "ftp://example.com/video.mp4",
        "",
    ],
)
def test_ytdlp_contract_rejects_non_http_urls(url: str):
    with pytest.raises(ValueError, match=r"HTTP\(S\) URL"):
        download_media(url, extractor=lambda *_a: {})


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/video/123",
        "http://127.0.0.1:8080/video",
        "https://user:secret@example.com/private/video",
    ],
)
def test_ytdlp_contract_passes_arbitrary_http_urls_to_extractor(url: str):
    recorder: dict[str, Any] = {}
    extractor = _make_cli_extractor(
        files={"fixture.mp4": b"generic media"},
        info={"yt_dlp_id": "fixture", "extractor": "Generic"},
        recorder=recorder,
    )

    result = download_media(url, media_type="video", extractor=extractor)

    assert recorder["request"]["url"] == url
    assert result.data == b"generic media"


def test_download_media_defaults_generic_callers_to_video():
    recorder: dict[str, Any] = {}
    extractor = _make_cli_extractor(
        files={"fixture.mp4": b"generic media"},
        info={"yt_dlp_id": "fixture", "extractor": "Generic"},
        recorder=recorder,
    )

    download_media("https://example.com/video/123", extractor=extractor)

    assert recorder["request"]["media_type"] == "video"


def test_default_extractor_path_allows_loopback_and_private_hosts(monkeypatch):
    recorder: dict[str, Any] = {}
    extractor = _make_cli_extractor(
        files={"fixture.mp4": b"local media"},
        info={"yt_dlp_id": "fixture", "extractor": "Generic"},
        recorder=recorder,
    )
    monkeypatch.setattr(ytdlp, "_build_default_extractor", lambda *_a: extractor)

    result = download_media("http://127.0.0.1:8080/video", media_type="video")

    assert recorder["request"]["url"] == "http://127.0.0.1:8080/video"
    assert result.data == b"local media"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_default_extractor_path_refuses_metadata_hosts(monkeypatch, url):
    extractor = _make_cli_extractor(
        files={"fixture.mp4": b"local media"},
        info={"yt_dlp_id": "fixture", "extractor": "Generic"},
        recorder={},
    )
    monkeypatch.setattr(ytdlp, "_build_default_extractor", lambda *_a: extractor)

    # yt-dlp's domain reach is open-ended, so the gate is an address-range
    # refusal rather than an allowlist; these ranges serve no media.
    with pytest.raises(ValueError, match="link-local/metadata"):
        download_media(url, media_type="video")


def test_cli_output_partition_rejects_multiple_media_files(tmp_path: Path):
    (tmp_path / "one.mp4").write_bytes(b"one")
    (tmp_path / "two.webm").write_bytes(b"two")

    with pytest.raises(RuntimeError, match="produced 2 media files"):
        ytdlp_outputs._partition_cli_outputs(tmp_path)


@pytest.mark.parametrize("extension", ["ape", "unknown_video"])
def test_cli_output_partition_accepts_ytdlp_media_extensions(
    tmp_path: Path, extension: str
):
    (tmp_path / f"fixture.{extension}").write_bytes(b"media")

    media, sidecars = ytdlp_outputs._partition_cli_outputs(tmp_path)

    assert media == f"fixture.{extension}"
    assert sidecars == []


def test_cli_output_partition_treats_unknown_non_media_outputs_as_sidecars(
    tmp_path: Path,
):
    (tmp_path / "fixture.mp4").write_bytes(b"media")
    for name in (
        "fixture.en.srv2",
        "fixture.en.lrc",
        "fixture.en.json",
        "fixture.mhtml",
    ):
        (tmp_path / name).write_text("sidecar")

    media, sidecars = ytdlp_outputs._partition_cli_outputs(tmp_path)

    assert media == "fixture.mp4"
    assert sidecars == [
        "fixture.en.json",
        "fixture.en.lrc",
        "fixture.en.srv2",
        "fixture.mhtml",
    ]


def test_cli_output_partition_requires_a_known_media_container(tmp_path: Path):
    (tmp_path / "fixture.en.srv2").write_text("subtitle")

    with pytest.raises(RuntimeError, match="did not produce a media file"):
        ytdlp_outputs._partition_cli_outputs(tmp_path)


def test_download_media_collects_an_allowed_srv2_subtitle_format():
    extractor = _make_cli_extractor(
        files={
            "fixture123.mp4": b"media",
            "fixture123.en.srv2": b"subtitle",
        },
        info={"yt_dlp_id": "fixture123", "extractor": "Generic"},
    )

    result = download_media(
        YOUTUBE_WATCH,
        media_type="video",
        extra_opts={"writesubtitles": True, "subtitlesformat": "srv2"},
        extractor=extractor,
    )

    assert [
        (sidecar.kind, sidecar.filename, sidecar.mime) for sidecar in result.sidecars
    ] == [("subtitles", "fixture123.en.srv2", "application/xml")]


def test_cancel_aware_wait_kills_the_child_and_raises_promptly():
    # A real long-lived child stands in for a slow yt-dlp download. Once the
    # cooperative-cancel probe flips True, the wait loop must kill the process
    # tree and raise MediaDownloadCancelled well inside a poll interval or two
    # — never blocking for the hour-long download deadline.
    import subprocess
    import sys
    import time

    proc = subprocess.Popen(  # noqa: S603
        # realtime: the sleep runs in the CHILD, not this test — it stands in
        # for a download that never finishes so cancellation has something
        # real to kill.
        [sys.executable, "-c", "import time; time.sleep(300)"],  # realtime: see above
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    # Flip the probe True almost immediately; the loop polls every ~2s.
    # realtime: cancelling a real OS process is inherently wall-clock work;
    # a virtual clock cannot make a spawned child exit.
    started = time.monotonic()  # realtime: see above

    def should_cancel() -> bool:
        return time.monotonic() - started > 0.1  # realtime: see above

    with pytest.raises(ytdlp.MediaDownloadCancelled):
        ytdlp._communicate_until_cancel_or_deadline(proc, should_cancel)
    # realtime: an upper liveness bound, not a window anything is asserted
    # NOT to happen inside — the positive proof is the raised exception.
    assert time.monotonic() - started < 30  # realtime: see above
    # The child is dead, not orphaned still holding a tunnel connection.
    assert proc.poll() is not None


def test_cancel_aware_wait_returns_normally_when_the_child_finishes():
    import subprocess
    import sys

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "print('done')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout, _stderr = ytdlp._communicate_until_cancel_or_deadline(
        proc, should_cancel=lambda: False
    )
    assert "done" in stdout
    assert proc.returncode == 0


def test_media_download_serializes_only_when_a_proxy_is_configured(monkeypatch):
    from frisket.engine.executor.media_download import download_max_concurrency

    monkeypatch.delenv("FRISKET_MEDIA_PROXY_MAX_CONCURRENCY", raising=False)

    import frisket.ops.media_proxy as media_proxy_mod

    monkeypatch.setattr(media_proxy_mod, "resolve_media_proxy", lambda: None)
    assert download_max_concurrency() is None

    monkeypatch.setattr(
        media_proxy_mod, "resolve_media_proxy", lambda: "socks5h://127.0.0.1:1080"
    )
    assert download_max_concurrency() == 1

    monkeypatch.setenv("FRISKET_MEDIA_PROXY_MAX_CONCURRENCY", "3")
    assert download_max_concurrency() == 3
    monkeypatch.setenv("FRISKET_MEDIA_PROXY_MAX_CONCURRENCY", "garbage")
    assert download_max_concurrency() == 1


def test_description_sidecar_option_is_rejected_until_it_has_an_output_contract():
    assert "writedescription" not in ytdlp.YTDLP_EXTRA_OPTS_ALLOWED_KEYS
    with pytest.raises(ValueError, match="writedescription"):
        download_media(
            YOUTUBE_WATCH,
            extra_opts={"writedescription": True},
            extractor=lambda *_a: {},
        )


def test_ytdlp_failure_diagnostic_is_bounded_and_has_fallbacks():
    assert (
        ytdlp._bounded_ytdlp_diagnostic(
            "prefix-" + "x" * 3_000,
            fallback="fallback",
        )
        == "x" * 2_000
    )
    assert (
        ytdlp._bounded_ytdlp_diagnostic(
            "",
            "stdout detail",
            fallback="fallback",
        )
        == "stdout detail"
    )
    assert ytdlp._bounded_ytdlp_diagnostic(None, fallback="exit code 9") == (
        "exit code 9"
    )


def test_safety_forced_extra_opts_keys_are_now_rejected():
    # Out of process, single-file safety is enforced by the runtime CLI args
    # (--no-playlist etc.), not by re-forcing caller opts; a caller can no longer
    # smuggle a reserved key past the strict choke point. max_filesize is no
    # longer a Frisket-forced key and,
    # being absent from the allowlist, is still rejected as a user extra_opt.
    for bad in ({"noplaylist": False}, {"max_filesize": 999}, {"outtmpl": "x"}):
        with pytest.raises(ValueError):
            download_media(YOUTUBE_WATCH, extra_opts=bad)


def _rejecting_extractor():
    def _extract(request, work_dir):  # noqa: ANN001, ARG001
        raise AssertionError("the transport must not run for rejected extra_opts")

    return _extract


def test_download_media_rejects_disallowed_extra_opts_keys():
    with pytest.raises(ValueError):
        download_media(
            YOUTUBE_WATCH,
            extra_opts={"postprocessors": [{"key": "FFmpegExtractAudio"}]},
            extractor=_rejecting_extractor(),
        )


def test_download_media_rejects_non_scalar_extra_opts_values():
    with pytest.raises(ValueError):
        download_media(
            YOUTUBE_WATCH,
            extra_opts={"subtitleslangs": [{"nested": 1}]},
            extractor=_rejecting_extractor(),
        )


def test_download_media_rejects_out_of_bounds_extra_opts_values():
    with pytest.raises(ValueError):
        download_media(
            YOUTUBE_WATCH,
            extra_opts={"sleep_interval": 10**7},
            extractor=_rejecting_extractor(),
        )


def test_action_catalog_uses_installed_ytdlp_availability_without_version_telemetry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        ytdlp,
        "managed_runtime_status",
        lambda: {"available": True, "error": None, "version": "2099.1.2"},
    )
    client = _client(tmp_path)

    entries = {
        entry["kind"]: entry
        for entry in client.get("/api/actions/v1/catalog").json()["actions"]
    }
    engine = entries["media.ytdlp_download"]["ui_hints"]["engines"][0]

    assert engine["id"] == "yt_dlp"
    assert engine["available"] is True
    assert "error" not in engine
    # The dedicated runtime-status endpoint owns installed/latest telemetry.
    assert "models" not in engine


def test_action_catalog_disables_ytdlp_when_installed_dependency_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        ytdlp,
        "managed_runtime_status",
        lambda: {
            "available": False,
            "error": "yt-dlp is not installed in Frisket's Python environment.",
            "version": None,
        },
    )
    client = _client(tmp_path)

    entries = {
        entry["kind"]: entry
        for entry in client.get("/api/actions/v1/catalog").json()["actions"]
    }
    engine = entries["media.ytdlp_download"]["ui_hints"]["engines"][0]

    assert engine["available"] is False
    assert "not installed" in engine["error"]


def test_import_urls_routes_youtube_watch_url_through_ytdlp(tmp_path, monkeypatch):
    from frisket.ops import url_import as import_family

    seen: list[str] = []

    def direct_download_should_not_run(url, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError(f"direct download called for watch URL: {url}")

    def fake_ytdlp(url, **kwargs):  # noqa: ANN001, ARG001
        seen.append(url)
        return _fake_downloaded_media()

    monkeypatch.setattr(import_family, "download_url", direct_download_should_not_run)
    monkeypatch.setattr(import_family.ytdlp, "download_media", fake_ytdlp)
    client = _client(tmp_path)
    pid, project = _project(client)

    response = client.post(
        f"/api/projects/{pid}/import/urls",
        json={"urls": [f"  {YOUTUBE_WATCH}  "], "column": "media"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["downloaded"] == 1
    assert seen == [YOUTUBE_WATCH]
    sheet_id = response.json()["sheet_id"]
    media = _cell(
        client,
        pid,
        sheet_id,
        client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()["rows"][0][
            "id"
        ],
        "media",
    )
    assert media["blob"]
    assert media["mime"] == "audio/wav"
    assert media["filename"] == "fixture.wav"
    assert set(media) == {"blob", "mime", "filename"}
    assert (
        MediaBlobStore(project).display_metadata(media["blob"])["duration_seconds"]
        == 1.25
    )
    blob = project.db.execute(
        "SELECT source_url, filename, mime FROM blobs WHERE hash=?", (media["blob"],)
    ).fetchone()
    assert dict(blob) == {
        "source_url": YOUTUBE_WATCH,
        "filename": "fixture.wav",
        "mime": "audio/wav",
    }


def test_download_media_recipe_runs_through_project_job_queue(tmp_path, monkeypatch):
    import frisket.ops.ytdlp as youtube_ops

    monkeypatch.setattr(
        youtube_ops,
        "download_media",
        lambda url, **kwargs: _fake_downloaded_media(),  # noqa: ARG005
    )
    client = _client(tmp_path)
    pid, project = _project(client)
    sheet_id = project.add_sheet("videos")
    cols = {"link": project.add_column(sheet_id, "link", type="link")}
    row_id = project.add_rows(sheet_id, [{"link": YOUTUBE_WATCH}], cols)[0]

    response = post_v1_action_with_exact_confirmation(
        client,
        pid,
        {
            "action_id": "media.ytdlp_download",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": "link", "media_type": "audio"},
            "output_names": {"audio": "audio"},
            "idempotency_key": "typed-download-queue",
        },
    )
    assert response.status_code == 200, response.text
    # media-acquisition-queued-placement-v1: media.ytdlp_download is now
    # QUEUED_PROJECT_RUN placement (was INLINE) — the request returns a queued
    # run handle immediately, and a worker executes it, exactly the "normal
    # job" behavior this test's name always described.
    assert response.json()["status"] == "queued"
    drain_queue(client)

    status_response = client.get(
        f"/api/projects/{pid}/actions/runs/{response.json()['run_id']}/status"
    )
    assert status_response.status_code == 200, status_response.text
    status = status_response.json()["run"]["public_status"]
    assert status["status"] == "completed"

    media = _cell(client, pid, sheet_id, row_id, "audio")
    assert media["blob"]
    assert media["mime"] == "audio/wav"
    assert set(media) == {"blob", "mime", "filename"}
    assert (
        MediaBlobStore(project).display_metadata(media["blob"])["duration_seconds"]
        == 1.25
    )
    with materialize_media_path(
        media, OpContext(project=project), op="transcribe"
    ) as path:
        assert path.exists()
        assert path.read_bytes() == _wav_bytes(seconds=1.25)


def test_download_media_uses_declared_input_column_and_defaults_to_video():
    from frisket.actions.media_download import MediaDownloadParams, download_media
    from frisket.actions.media_download_types import DownloadedFiles
    from frisket.actions.types import Row

    seen = []

    class Downloader:
        async def download(self, url, **kwargs):
            seen.append((url, kwargs["media_type"]))
            return DownloadedFiles(video=None)

    params = MediaDownloadParams(source="video_url")
    assert params.media_type == "video"
    asyncio.run(
        download_media(
            params,
            Row(
                {"article_link": "https://example.com/story", "video_url": TIKTOK_VIDEO}
            ),
            Downloader(),
        )
    )
    assert seen == [(TIKTOK_VIDEO, "video")]


def test_download_media_collects_sidecar_files():
    """The managed-runtime transport writes the main media file plus a thumbnail,
    subtitle files, and an info.json into the host scratch dir; DownloadedMedia
    must carry all three sidecar kinds, and the main media selection is unchanged
    by the sidecars sitting alongside it."""
    extractor = _make_cli_extractor(
        files={
            "fixture123.webm": b"fake webm audio",
            "fixture123.webp": b"fake webp thumbnail bytes",
            "fixture123.en.vtt": b"WEBVTT\n\nfake english subs",
            "fixture123.es.vtt": b"WEBVTT\n\nfake spanish subs",
            "fixture123.info.json": b'{"id": "fixture123"}',
        },
        info={
            "yt_dlp_id": "fixture123",
            "title": "Fixture video",
            "duration_seconds": 12.5,
            "extractor": "Youtube",
        },
    )

    result = download_media(
        YOUTUBE_WATCH,
        media_type="audio",
        extra_opts={
            "writethumbnail": True,
            "writesubtitles": True,
            "writeinfojson": True,
            "subtitleslangs": ["es"],
        },
        extractor=extractor,
    )

    # Main media selection is unchanged by the sidecars sitting alongside it.
    assert result.data == b"fake webm audio"
    assert result.mime == "audio/webm"
    assert result.filename == "fixture123.webm"

    sidecars = {sidecar.kind: sidecar for sidecar in result.sidecars}
    assert set(sidecars) == {"thumbnail", "subtitles", "info_json"}

    thumbnail = sidecars["thumbnail"]
    assert thumbnail.data == b"fake webp thumbnail bytes"
    assert thumbnail.mime == "image/webp"
    assert thumbnail.filename == "fixture123.webp"

    subtitles = sidecars["subtitles"]
    assert subtitles.filename == "fixture123.es.vtt"
    assert subtitles.mime == "text/vtt"
    assert b"spanish" in subtitles.data

    info_json = sidecars["info_json"]
    assert info_json.filename == "fixture123.info.json"
    assert info_json.mime == "application/json"
    assert b"fixture123" in info_json.data


def test_download_media_subtitle_language_preference_falls_back_to_english():
    """With no subtitleslangs preference, 'en' wins over other languages."""
    extractor = _make_cli_extractor(
        files={
            "fixture123.webm": b"fake webm audio",
            "fixture123.fr.vtt": b"WEBVTT\n\nfake french subs",
            "fixture123.en.vtt": b"WEBVTT\n\nfake english subs",
        },
        info={
            "yt_dlp_id": "fixture123",
            "title": "Fixture video",
            "duration_seconds": 12.5,
            "extractor": "Youtube",
        },
    )

    result = download_media(
        YOUTUBE_WATCH,
        extra_opts={"writesubtitles": True},
        extractor=extractor,
    )

    subtitles = next(s for s in result.sidecars if s.kind == "subtitles")
    assert subtitles.filename == "fixture123.en.vtt"
