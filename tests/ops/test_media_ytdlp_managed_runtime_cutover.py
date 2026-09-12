from __future__ import annotations

import inspect
import re
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

import frisket.ops.ytdlp as ytdlp
from frisket.ops import egress_policy
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.runtime.launch import worker_argv
from frisket.runtime.supervisor import guarded_argv

# The acquisition-path modules whose in-process yt-dlp extraction must leave the
# app process. The YouTube SOURCE-POLL path (frisket.sources.youtube) is a
# distinct action kind (source.poll, not media.*) and is intentionally NOT in
# scope here — it keeps its own in-process poll until separately cut over.
_ACQUISITION_MODULES = (
    "frisket.ops.ytdlp",
    "frisket.sdk.ops.ytdlp_download",
    "frisket.engine.executor.file_fetch",
)

_WATCH_URL = "https://www.youtube.com/watch?v=video123"
_MEDIA_BYTES = b"\x00\x01managed-runtime-audio\x02\x03"
_SUBS_BYTES = b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n"

# What installed-package receipt evidence looks like: a content hash, not only a
# version string. The version here is deliberately NOT any literal the
# acquisition code could hold — it must ride in from the runtime.
_RUNTIME_EVIDENCE = {
    "runtime_name": "yt-dlp",
    "resolved_version": "2099.12.31",
    "artifact_sha256": "b" * 64,
}


@pytest.fixture(autouse=True)
def stable_public_dns(monkeypatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def fixture_getaddrinfo(host, port, *args, **kwargs):
        if host == "www.youtube.com":
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


def _make_fake_extractor() -> tuple[Any, dict[str, Any]]:
    """A frisket.plugins.media MediaExtractor stand-in: it is called with
    ``(request, work_dir)``, WRITES the media file + one subtitle sidecar into the
    host-provided scratch dir, and returns metadata + filenames + runtime evidence
    — never bytes. Records what it saw so the test can pin the seam contract."""
    seen: dict[str, Any] = {}

    def _fake_extractor(request: dict[str, Any], work_dir: Path) -> dict[str, Any]:
        seen["request"] = request
        seen["work_dir"] = Path(work_dir)
        media = Path(work_dir) / "video123.m4a"
        media.write_bytes(_MEDIA_BYTES)
        subs = Path(work_dir) / "video123.en.vtt"
        subs.write_bytes(_SUBS_BYTES)
        return {
            "media_filename": media.name,
            "sidecar_filenames": [subs.name],
            "metadata": {
                "yt_dlp_id": "video123",
                "title": "Fake video",
                "duration_seconds": 1.0,
                "webpage_url": request.get("url"),
            },
            "runtime_evidence": dict(_RUNTIME_EVIDENCE),
        }

    return _fake_extractor, seen


def _download_accepts_injected_extractor() -> bool:
    """True once download_media exposes the injected-extractor seam. Probed
    defensively so a pre-cutover run fails on a plain assertion, not a TypeError."""
    try:
        params = inspect.signature(ytdlp.download_media).parameters
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return False
    return "extractor" in params


def _require_injected_extractor() -> None:
    assert _download_accepts_injected_extractor(), (
        "download_media must accept an injected `extractor` seam so the "
        "core acquisition path runs through the managed-runtime extractor + "
        "file-based scratch handoff (frisket.plugins.media), not in-process "
        "YoutubeDL (media-plugin-frisket-media-v1 Phase 1 cutover)"
    )


# ---------------------------------------------------------------------------
# 1. The acquisition path exposes + uses the managed-runtime extractor seam.
# ---------------------------------------------------------------------------


def test_acquisition_executes_via_managed_runtime_extractor_and_scratch_handoff():
    _require_injected_extractor()
    extractor, seen = _make_fake_extractor()

    result = ytdlp.download_media(_WATCH_URL, media_type="audio", extractor=extractor)

    # The bytes come back from a file the extractor WROTE into a host scratch dir:
    # the only way to obtain them is the file-based handoff seam (run_media_download
    # + scratch read-back), never an in-process YoutubeDL bytes return.
    assert result.data == _MEDIA_BYTES, (
        "the acquired media bytes must be read back from the extractor's scratch "
        "files (managed-runtime file handoff), proving the in-process YoutubeDL "
        "bytes path is gone"
    )
    assert result.filename == "video123.m4a"
    # The extractor was invoked with the out-of-process CHILD request shape from
    # frisket.plugins.media (a mapped extra_opts plan), not the old
    # (url, media_type=...) in-process call shape.
    assert "extra_opts_cli_args" in seen.get("request", {}), (
        "the extractor must receive the frisket.plugins.media child request "
        "(mapped extra_opts plan), i.e. the acquisition rides run_media_download"
    )
    assert isinstance(seen.get("work_dir"), Path), (
        "the extractor must be handed a host-provided scratch work_dir"
    )
    # The subtitle sidecar the child wrote is collected and classified.
    subtitles = [s for s in result.sidecars if getattr(s, "kind", None) == "subtitles"]
    assert subtitles and subtitles[0].data == _SUBS_BYTES, (
        "the scratch-dir subtitle sidecar must be ingested from the handoff"
    )


# ---------------------------------------------------------------------------
# 2. The in-process yt_dlp.YoutubeDL path is deleted from the acquisition modules.
# ---------------------------------------------------------------------------


def test_acquisition_runs_without_in_process_yt_dlp_importable():
    _require_injected_extractor()
    extractor, _seen = _make_fake_extractor()
    saved = sys.modules.get("yt_dlp", "__absent__")
    # Make any lingering ``import yt_dlp`` in the acquisition path fail loudly.
    sys.modules["yt_dlp"] = None  # type: ignore[assignment]
    try:
        result = ytdlp.download_media(
            _WATCH_URL, media_type="audio", extractor=extractor
        )
    finally:
        if saved == "__absent__":
            sys.modules.pop("yt_dlp", None)
        else:
            sys.modules["yt_dlp"] = saved  # type: ignore[assignment]
    assert result.data == _MEDIA_BYTES, (
        "the managed-runtime acquisition path must not depend on an in-process "
        "yt_dlp module being importable"
    )


# ---------------------------------------------------------------------------
# 3. The public extra_opts schema (the single choke point) survives + is enforced.
# ---------------------------------------------------------------------------


def test_public_extra_opts_schema_is_intact_and_still_enforced():
    # The allowlist is the durable public schema; it must not be forked or eroded
    # by the cutover.
    allowed = ytdlp.YTDLP_EXTRA_OPTS_ALLOWED_KEYS
    for key in ("writesubtitles", "subtitleslangs", "retries", "ratelimit"):
        assert key in allowed, f"public extra_opts allowlist lost {key!r}"
    # The single choke point still rejects a disallowed key.
    with pytest.raises(ValueError):
        ytdlp.validate_extra_opts({"exec_cmd": "rm -rf /"})


def test_disallowed_extra_opts_rejected_before_any_transport_work():
    _require_injected_extractor()
    extractor, seen = _make_fake_extractor()
    with pytest.raises(ValueError):
        ytdlp.download_media(
            _WATCH_URL,
            extra_opts={"postprocessors": [{"key": "FFmpegExtractAudio"}]},
            extractor=extractor,
        )
    # A rejected extra_opts must fail closed BEFORE the transport is invoked.
    assert "request" not in seen, (
        "a disallowed extra_opts key must be rejected by the choke point before "
        "the managed-runtime extractor is called"
    )


# ---------------------------------------------------------------------------
# 4. Receipts carry the runtime's content-hash evidence.
# ---------------------------------------------------------------------------


def test_acquisition_result_and_persisted_metadata_carry_runtime_content_hash():
    _require_injected_extractor()
    extractor, _seen = _make_fake_extractor()
    result = ytdlp.download_media(_WATCH_URL, media_type="audio", extractor=extractor)

    evidence = getattr(result, "runtime_evidence", None) or {}
    assert set(evidence) >= {"runtime_name", "resolved_version", "artifact_sha256"}, (
        "the acquisition result must carry the runtime's receipt evidence"
    )
    assert evidence["runtime_name"] == "yt-dlp"
    assert evidence["artifact_sha256"] == _RUNTIME_EVIDENCE["artifact_sha256"], (
        "the receipt evidence must stamp the resolved CONTENT hash, not only a "
        "version string"
    )

    # The evidence must also ride in the metadata dict that add_downloaded_media_blob
    # persists to the blob store (the media-blob receipt refs read that metadata),
    # so the runtime content hash is discoverable from the run's receipts.
    persisted = (result.metadata or {}).get("managed_runtime")
    assert isinstance(persisted, dict), (
        "the acquired media's persisted metadata must carry a managed_runtime block "
        "so the run's receipt evidence records the resolved runtime content hash"
    )
    assert persisted.get("artifact_sha256") == _RUNTIME_EVIDENCE["artifact_sha256"]


# ---------------------------------------------------------------------------
# 5. A yt-dlp version bump is a dependency/lock change, not an app-code change:
#    the acquisition path holds NO version literal and takes its version from the
#    installed distribution.
# ---------------------------------------------------------------------------


def test_resolved_version_rides_in_from_the_runtime_not_a_literal():
    _require_injected_extractor()
    extractor, _seen = _make_fake_extractor()
    result = ytdlp.download_media(_WATCH_URL, media_type="audio", extractor=extractor)
    evidence = getattr(result, "runtime_evidence", None) or {}
    # The version is whatever the runtime reported (here the fake's), not a value
    # the acquisition code chose — so bumping the package bumps the receipt.
    assert evidence.get("resolved_version") == _RUNTIME_EVIDENCE["resolved_version"], (
        "the receipt's resolved_version must come from the selected runtime, so a "
        "version bump needs no acquisition-code change"
    )


# ---------------------------------------------------------------------------
# 6. The installed package remains out of process and disables plugins.
# ---------------------------------------------------------------------------


def _capture_extractor_argv(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: dict[str, list] = {}

    class _FakePopen:
        returncode = 1

        def __init__(self, argv, **kwargs):
            captured["kwargs"] = kwargs
            self.pid = 1234
            self.args = argv
            self.stdout = None
            self.stderr = None
            self._argv = argv
            captured["argv"] = argv

        def communicate(self, *, timeout):  # noqa: ARG002
            return "", "synthetic failure -- argv capture only, no real yt-dlp run"

    monkeypatch.setattr(ytdlp.subprocess, "Popen", _FakePopen)
    extractor = ytdlp._installed_ytdlp_cli_extractor(_RUNTIME_EVIDENCE)
    request = {"url": "https://example.invalid/watch?v=1", "media_type": "audio"}
    with pytest.raises(RuntimeError, match="yt-dlp runtime failed"):
        extractor(request, Path("/scratch/work"))
    return captured["argv"]


def test_installed_ytdlp_runs_out_of_process_with_admin_config_and_one_item(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(ytdlp, "_admin_ytdlp_config_paths", tuple)
    argv = _capture_extractor_argv(monkeypatch)
    prefix = guarded_argv(worker_argv("yt-dlp"))
    assert argv[: len(prefix)] == prefix
    # yt-dlp's own discovery includes a CWD-relative slot, so it stays off and
    # admin config reaches the child only through paths frisket names.
    assert "--ignore-config" in argv
    assert "--config-locations" not in argv
    assert "--no-plugin-dirs" in argv
    assert "--no-playlist" in argv
    assert argv[argv.index("--playlist-items") + 1] == "1"


def test_installed_ytdlp_passes_only_admin_config_paths_that_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    present = tmp_path / "yt-dlp.conf"
    present.write_text("--no-playlist\n", encoding="utf-8")
    absent = tmp_path / "missing" / "config"
    monkeypatch.setattr(ytdlp, "_admin_ytdlp_config_paths", lambda: (absent, present))
    argv = _capture_extractor_argv(monkeypatch)
    # A non-existent --config-locations path is a fatal argument error in
    # yt-dlp, so only resolvable configs may reach argv.
    assert argv.count("--config-locations") == 1
    assert argv[argv.index("--config-locations") + 1] == str(present)
    assert str(absent) not in argv


def test_admin_ytdlp_config_paths_exclude_cwd_relative_slots() -> None:
    paths = [str(path) for path in ytdlp._admin_ytdlp_config_paths()]
    assert paths, "expected at least the system-wide admin config candidates"
    # yt-dlp's Home slot resolves against the process CWD; a config planted
    # there could set --exec or --proxy in the download child.
    assert all(Path(path).is_absolute() for path in paths)
    assert str(Path("yt-dlp.conf").resolve()) not in paths


def test_installed_ytdlp_runtime_has_a_total_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    killed: list[object] = []
    slice_timeouts: list[float] = []
    # Real-time bounds so the loop reaches its deadline in milliseconds; the
    # contract under test is that a total deadline exists and is enforced,
    # not its production magnitude.
    monkeypatch.setattr(ytdlp, "YTDLP_DOWNLOAD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ytdlp, "_YTDLP_CANCEL_POLL_SECONDS", 0.01)

    class _TimeoutPopen:
        returncode = None

        def __init__(self, argv, **kwargs):
            captured.update(kwargs)
            self.args = argv
            self.pid = 1234
            self.stdout = None
            self.stderr = None
            self._communicate_calls = 0

        def communicate(self, *, timeout):
            # A child that NEVER finishes: the deadline is enforced by the
            # polling loop, so a fake that completes on its second call would
            # take the ordinary non-zero-exit path and never exercise it.
            self._communicate_calls += 1
            slice_timeouts.append(timeout)
            raise ytdlp.subprocess.TimeoutExpired(
                self.args,
                timeout,
                output="partial stdout",
                stderr="useful timeout detail",
            )

        def poll(self):
            return None

        def kill(self):
            killed.append(self)

        def wait(self, timeout=None):
            # Reached via the post-kill drain, which a real Popen supports;
            # the child is gone by now, so report a terminated status.
            self.returncode = -9
            return self.returncode

    monkeypatch.setattr(ytdlp.subprocess, "Popen", _TimeoutPopen)
    monkeypatch.setattr(ytdlp, "_kill_process_tree", lambda proc: killed.append(proc))
    extractor = ytdlp._installed_ytdlp_cli_extractor(_RUNTIME_EVIDENCE)

    with pytest.raises(RuntimeError, match="runtime exceeded.*useful timeout detail"):
        extractor(
            {"url": "https://vimeo.com/123456789", "media_type": "video"},
            tmp_path,
        )

    # The child is drained in slices no longer than the cancel-poll interval,
    # so a cancel is noticed promptly instead of after the whole download
    # window -- and the loop still terminates on the total deadline.
    assert slice_timeouts, "expected the runtime to drain the child at least once"
    # First drain is a polling slice, capped at the cancel-poll interval so a
    # cancel is noticed promptly rather than after the whole download window.
    assert slice_timeouts[0] <= ytdlp._YTDLP_CANCEL_POLL_SECONDS
    # Past the deadline the loop makes one final zero-timeout poll, which is
    # what produces the genuine TimeoutExpired carrying the drained output.
    assert 0 in slice_timeouts, (
        "expected a final zero-timeout poll once the total deadline passed"
    )
    # A child that never responds forces the escalation: kill the process
    # tree, then hard-kill once the post-kill drain times out too. Neither
    # alone guarantees the process is gone.
    assert len(killed) == 2
    if sys.platform == "win32":
        assert captured["creationflags"] == ytdlp.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert captured["start_new_session"] is True


def test_default_extractor_uses_only_installed_ytdlp_package(
    monkeypatch: pytest.MonkeyPatch,
):
    sentinel = object()
    monkeypatch.setattr(
        ytdlp, "_installed_ytdlp_evidence", lambda: dict(_RUNTIME_EVIDENCE)
    )
    monkeypatch.setattr(
        ytdlp,
        "_installed_ytdlp_cli_extractor",
        lambda evidence, should_cancel=None: (
            sentinel if evidence == _RUNTIME_EVIDENCE else None
        ),
    )
    assert ytdlp._build_default_extractor() is sentinel


def test_runtime_status_reports_installed_distribution_version(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(ytdlp.importlib_metadata, "version", lambda _name: "2099.1.2")
    assert ytdlp.managed_runtime_status() == {
        "available": True,
        "error": None,
        "version": "2099.1.2",
    }


def test_runtime_status_reports_missing_installed_dependency(
    monkeypatch: pytest.MonkeyPatch,
):
    def _missing(_name):
        raise ytdlp.importlib_metadata.PackageNotFoundError("yt-dlp")

    monkeypatch.setattr(ytdlp.importlib_metadata, "version", _missing)
    status = ytdlp.managed_runtime_status()
    assert status["available"] is False
    assert status["version"] is None
    assert "not installed" in status["error"]


def test_installed_ytdlp_receipt_hashes_package_tree_and_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    package = tmp_path / "yt_dlp"
    package.mkdir()
    module = package / "__main__.py"
    module.write_text("print('one')\n")
    generated_script = tmp_path / "bin" / "yt-dlp"
    generated_script.parent.mkdir()
    generated_script.write_text("#!/some/install/specific/python\n")

    class _Distribution:
        version = "2099.1.2"
        files = (
            Path("yt_dlp/__main__.py"),
            Path("../../../bin/yt-dlp"),
        )

        @staticmethod
        def locate_file(relative):
            if relative.parts[0] == "yt_dlp":
                return tmp_path / relative
            return generated_script

    monkeypatch.setattr(
        ytdlp.importlib_metadata,
        "distribution",
        lambda _name: _Distribution(),
    )
    ytdlp._installed_ytdlp_evidence.cache_clear()
    try:
        first = ytdlp._installed_ytdlp_evidence()
        module.write_text("print('two')\n")
        assert ytdlp._installed_ytdlp_evidence() == first
        ytdlp._installed_ytdlp_evidence.cache_clear()
        second = ytdlp._installed_ytdlp_evidence()
    finally:
        ytdlp._installed_ytdlp_evidence.cache_clear()

    assert first["resolved_version"] == "2099.1.2"
    assert re.fullmatch(r"[0-9a-f]{64}", first["artifact_sha256"])
    assert first["artifact_sha256"] != second["artifact_sha256"]


def test_missing_installed_ytdlp_fails_with_clear_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def _missing(_name):
        raise ytdlp.importlib_metadata.PackageNotFoundError("yt-dlp")

    monkeypatch.setattr(ytdlp.importlib_metadata, "distribution", _missing)
    ytdlp._installed_ytdlp_evidence.cache_clear()
    try:
        with pytest.raises(ytdlp.ManagedRuntimeUnavailable, match="yt-dlp"):
            ytdlp._installed_ytdlp_evidence()
    finally:
        ytdlp._installed_ytdlp_evidence.cache_clear()


def test_ytdlp_cloud_anti_bot_challenge_becomes_safe_provider_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CloudChallengePopen:
        returncode = 1

        def __init__(self, *_args, **_kwargs) -> None:
            self.pid = 1234

        def communicate(self, *, timeout):  # noqa: ARG002
            return "", (
                "ERROR: [youtube] video123: Sign in to confirm you’re not a bot. "
                "More detail: https://provider.invalid/help?token=must-not-persist"
            )

    monkeypatch.setattr(
        ytdlp.subprocess,
        "Popen",
        _CloudChallengePopen,
    )
    extractor = ytdlp._installed_ytdlp_cli_extractor(_RUNTIME_EVIDENCE)

    with pytest.raises(HostedEngineError) as captured:
        extractor(
            {"url": _WATCH_URL, "media_type": "audio"},
            Path("/scratch/work"),
        )

    assert captured.value.code == ytdlp.YOUTUBE_PROVIDER_BLOCKED_CODE
    assert captured.value.retryable is False
    assert str(captured.value) == ytdlp.YOUTUBE_PROVIDER_BLOCKED_MESSAGE
    assert "cloud-hosted egress IPs" in str(captured.value)
    assert "must-not-persist" not in str(captured.value)


def test_ytdlp_unrelated_cli_failure_keeps_existing_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnrelatedFailurePopen:
        returncode = 1

        def __init__(self, *_args, **_kwargs) -> None:
            self.pid = 1234

        def communicate(self, *, timeout):  # noqa: ARG002
            return "", "synthetic failure -- argv capture only, no real yt-dlp run"

    monkeypatch.setattr(
        ytdlp.subprocess,
        "Popen",
        _UnrelatedFailurePopen,
    )
    extractor = ytdlp._installed_ytdlp_cli_extractor(_RUNTIME_EVIDENCE)

    with pytest.raises(RuntimeError) as captured:
        extractor(
            {"url": _WATCH_URL, "media_type": "audio"},
            Path("/scratch/work"),
        )

    assert type(captured.value) is RuntimeError
    assert str(captured.value) == (
        "yt-dlp runtime failed: synthetic failure -- argv capture only, "
        "no real yt-dlp run"
    )
