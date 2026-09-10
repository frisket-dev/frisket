"""Regression coverage for the wave-1 second-model review fixes to
``frisket.ytdlp`` (findings #6-#8).

Additive tests around the frozen cutover contract
(tests/test_media_ytdlp_managed_runtime_cutover.py,
tests/test_ytdlp_download.py). They pin the consumer-side scratch-escape guard,
the runtime-evidence shape check, and the declared-sidecar authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.ops.ytdlp import (
    _validated_runtime_evidence,
    download_media,
)
from frisket.ops.ytdlp_outputs import (
    _collect_sidecar_files,
    _require_within_scratch,
)

_WATCH = "https://www.youtube.com/watch?v=video123"
_GOOD_EVIDENCE = {
    "runtime_name": "yt-dlp",
    "resolved_version": "2099.12.31",
    "artifact_sha256": "a" * 64,
}


class _InjectedTransportPolicy:
    def check_url(self, _url: str) -> None:
        """Keep transport-fake tests independent of live DNS."""


_INJECTED_TRANSPORT_POLICY = _InjectedTransportPolicy()


# --- #6: consumer-side scratch-escape guard --------------------------------


def test_require_within_scratch_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="absolute"):
        _require_within_scratch(tmp_path, "/etc/passwd", "media")


def test_require_within_scratch_rejects_dotdot_escape(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(RuntimeError, match="escapes"):
        _require_within_scratch(scratch, "../outside.bin", "media")


def test_require_within_scratch_accepts_nested_relative(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    resolved = _require_within_scratch(scratch, "sub/dir/file.m4a", "media")
    assert resolved == (scratch / "sub" / "dir" / "file.m4a").resolve()


def test_collect_sidecars_rejects_declared_escape(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "video123.m4a").write_bytes(b"media")
    with pytest.raises(RuntimeError, match="escapes"):
        _collect_sidecar_files(
            scratch,
            video_id="video123",
            media_path=scratch / "video123.m4a",
            extra_opts=None,
            declared_sidecars=("../evil.vtt",),
        )


def _extractor(files: dict[str, bytes], *, sidecars: list[str], evidence: dict):
    def _extract(request: dict[str, Any], work_dir: Path) -> dict[str, Any]:
        media_name = ""
        for name, data in files.items():
            (Path(work_dir) / name).write_bytes(data)
            if name not in sidecars and not media_name:
                media_name = name
        return {
            "media_filename": media_name,
            "sidecar_filenames": sidecars,
            "metadata": {"yt_dlp_id": "video123", "webpage_url": request.get("url")},
            "runtime_evidence": dict(evidence),
        }

    return _extract


def test_escaping_media_relpath_from_transport_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Belt-and-suspenders: even if run_media_download were bypassed, the ytdlp
    # consumption site refuses an escaping media path. Stub run_media_download to
    # return a hostile handoff and prove ytdlp still fails closed.
    from frisket.plugins import media as media_mod

    class _Handoff:
        media_relpath = "../../etc/shadow"
        sidecar_relpaths: tuple[str, ...] = ()
        metadata: dict[str, Any] = {}
        runtime_evidence: dict[str, str] = {}

    # download_media does a call-time `from frisket.plugins.media import
    # run_media_download`, so patching the media-module attribute is what binds.
    monkeypatch.setattr(media_mod, "run_media_download", lambda *a, **k: _Handoff())
    with pytest.raises(RuntimeError, match="escapes"):
        download_media(
            _WATCH,
            extractor=lambda *a, **k: {},
            policy=_INJECTED_TRANSPORT_POLICY,
        )


# --- #7: runtime-evidence shape check --------------------------------------


def test_validated_runtime_evidence_passes_well_formed() -> None:
    assert _validated_runtime_evidence(dict(_GOOD_EVIDENCE)) == _GOOD_EVIDENCE


def test_validated_runtime_evidence_allows_empty() -> None:
    assert _validated_runtime_evidence(None) == {}
    assert _validated_runtime_evidence({}) == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"runtime_name": "", "resolved_version": "1", "artifact_sha256": "a" * 64},
        {"runtime_name": "yt-dlp", "resolved_version": "", "artifact_sha256": "a" * 64},
        {"runtime_name": "yt-dlp", "resolved_version": "1", "artifact_sha256": "xyz"},
        {
            "runtime_name": "yt-dlp",
            "resolved_version": "1",
            "artifact_sha256": "A" * 64,
        },
        {"runtime_name": "yt-dlp", "resolved_version": "1"},  # missing sha
    ],
)
def test_validated_runtime_evidence_rejects_malformed(bad: dict) -> None:
    with pytest.raises(RuntimeError):
        _validated_runtime_evidence(bad)


def test_download_rejects_malformed_transport_evidence(tmp_path: Path) -> None:
    extractor = _extractor(
        {"video123.m4a": b"audio"},
        sidecars=[],
        evidence={"runtime_name": "yt-dlp", "artifact_sha256": "nope"},
    )
    with pytest.raises(RuntimeError):
        download_media(
            _WATCH,
            extractor=extractor,
            policy=_INJECTED_TRANSPORT_POLICY,
        )


# --- #8: the declared sidecar list is authoritative ------------------------


def test_undeclared_stray_sidecar_is_not_ingested() -> None:
    # A stray file matches the naming convention but is NOT declared in the
    # handoff; the declared-list pool must not pick it up.
    def extractor(request: dict[str, Any], work_dir: Path) -> dict[str, Any]:
        wd = Path(work_dir)
        (wd / "video123.m4a").write_bytes(b"audio")
        (wd / "video123.en.vtt").write_bytes(b"WEBVTT\n\ndeclared")
        # Undeclared stray thumbnail sitting alongside, convention-matching.
        (wd / "video123.webp").write_bytes(b"stray thumbnail")
        return {
            "media_filename": "video123.m4a",
            "sidecar_filenames": ["video123.en.vtt"],
            "metadata": {"yt_dlp_id": "video123", "webpage_url": request.get("url")},
            "runtime_evidence": dict(_GOOD_EVIDENCE),
        }

    result = download_media(
        _WATCH,
        extra_opts={"writesubtitles": True},
        extractor=extractor,
        policy=_INJECTED_TRANSPORT_POLICY,
    )
    kinds = {s.kind for s in result.sidecars}
    assert kinds == {"subtitles"}, (
        "only the declared subtitle sidecar should be ingested; the undeclared "
        "stray thumbnail must be ignored"
    )
