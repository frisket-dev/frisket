"""Filesystem output discovery and sidecar selection for yt-dlp handoffs."""

from __future__ import annotations

import mimetypes
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SidecarFile:
    """One non-main-media file yt-dlp wrote alongside the download.

    ``kind`` is one of ``'thumbnail' | 'subtitles' | 'info_json'``. Only one
    file per kind is ever collected: for subtitles this is the single file
    the recipe stores as its one subtitles column, chosen by language
    preference (see ``_select_subtitle_file``).
    """

    kind: str
    data: bytes
    mime: str
    filename: str


# Sidecar file matching, keyed to the video id stem (e.g. yt-dlp writes
# `<id>.<ext>` for a thumbnail, `<id>.<lang>.<ext>` for a subtitle track, and
# `<id>.info.json` for the info JSON).
_THUMBNAIL_EXTS = frozenset({"webp", "jpg", "jpeg", "png", "mhtml"})
_THUMBNAIL_EXT_PRIORITY = ("webp", "jpg", "jpeg", "png", "mhtml")
_SUBTITLE_EXTS = frozenset(
    {"vtt", "srt", "ass", "ttml", "srv1", "srv2", "srv3", "json3", "json", "lrc"}
)
_SUBTITLE_MIME_BY_EXT = {
    "vtt": "text/vtt",
    "srt": "application/x-subrip",
    "ass": "text/x-ssa",
    "ttml": "application/ttml+xml",
    "srv1": "application/xml",
    "srv2": "application/xml",
    "srv3": "application/xml",
    "json3": "application/json",
    "json": "application/json",
    "lrc": "text/plain",
}
# Positive classification keeps an unfamiliar subtitle/storyboard/metadata
# extension from becoming a second "media" file merely because it is absent from
# the known-sidecar sets. This covers yt-dlp's normal audio/video containers;
# `mimetypes` supplements it for platform registrations not listed here.
_MEDIA_CONTAINER_EXTS = frozenset(
    {
        "3g2",
        "3ga",
        "3gp",
        "aac",
        "ac3",
        "aif",
        "aiff",
        "alac",
        "ape",
        "asf",
        "avi",
        "divx",
        "dts",
        "eac3",
        "f4a",
        "f4b",
        "f4v",
        "flac",
        "flv",
        "isma",
        "ismv",
        "it",
        "m2ts",
        "m4a",
        "m4b",
        "m4r",
        "m4s",
        "m4v",
        "mk3d",
        "mka",
        "mkv",
        "mov",
        "mp3",
        "mp1",
        "mp2v",
        "mp4",
        "mpa",
        "mpeg",
        "mpeg1",
        "mpeg2",
        "mpeg4",
        "mpg",
        "mxf",
        "mod",
        "mts",
        "oga",
        "ogg",
        "ogm",
        "ogv",
        "ogx",
        "opus",
        "spx",
        "rm",
        "shn",
        "swf",
        "ts",
        "vob",
        "vid",
        "vorbis",
        "vp9",
        "wav",
        "weba",
        "webm",
        "wma",
        "wmv",
        # GenericIE uses this sentinel when a successful direct-media probe
        # cannot identify a safe conventional extension.
        "unknown_video",
    }
)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _partition_cli_outputs(work_dir: Path) -> tuple[str, list[str]]:
    """Require one main media file and return scratch-relative sidecars."""
    media: list[Path] = []
    sidecars: list[str] = []
    for path in sorted(work_dir.iterdir()):
        if not path.is_file() or path.name.endswith((".part", ".ytdl")):
            continue
        if path.name == "info.json":
            continue
        if _is_media_output(path):
            media.append(Path(path.name))
        else:
            sidecars.append(path.name)
    if not media:
        raise RuntimeError("yt-dlp runtime did not produce a media file")
    if len(media) != 1:
        raise RuntimeError(
            f"yt-dlp runtime produced {len(media)} media files; expected exactly one"
        )
    return media[0].name, sidecars


def _is_media_output(path: Path) -> bool:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in _MEDIA_CONTAINER_EXTS:
        return True
    guessed_mime, _encoding = mimetypes.guess_type(path.name)
    return bool(guessed_mime and guessed_mime.startswith(("audio/", "video/")))


def _collect_sidecar_files(
    temp_dir: Path,
    *,
    video_id: str,
    media_path: Path,
    extra_opts: dict[str, Any] | None,
    declared_sidecars: tuple[str, ...] = (),
) -> list[SidecarFile]:
    """Collect thumbnail, subtitle, and info-json sidecars from scratch."""
    use_declared = bool(declared_sidecars)
    if use_declared:
        candidates = sorted(
            (
                _require_within_scratch(temp_dir, rel, "sidecar")
                for rel in declared_sidecars
            ),
            key=lambda p: p.name,
        )
    else:
        if not video_id:
            return []
        candidates = sorted(temp_dir.iterdir())
    thumbnails: list[Path] = []
    subtitles: list[Path] = []
    info_json: Path | None = None
    for path in candidates:
        if (
            not path.is_file()
            or path == media_path
            or path.name.endswith((".part", ".ytdl"))
        ):
            continue
        if not use_declared and not path.name.startswith(f"{video_id}."):
            continue
        if path.name.endswith(".info.json"):
            info_json = path
            continue
        suffix = path.suffix.lower().lstrip(".")
        if suffix in _THUMBNAIL_EXTS:
            thumbnails.append(path)
        elif suffix in _SUBTITLE_EXTS:
            subtitles.append(path)
    sidecars: list[SidecarFile] = []
    thumbnail_path = _select_thumbnail_file(thumbnails)
    if thumbnail_path is not None:
        sidecars.append(
            SidecarFile(
                kind="thumbnail",
                data=thumbnail_path.read_bytes(),
                mime=_thumbnail_mime(thumbnail_path),
                filename=thumbnail_path.name,
            )
        )
    subtitle_path = _select_subtitle_file(
        subtitles, video_id=video_id, extra_opts=extra_opts
    )
    if subtitle_path is not None:
        sidecars.append(
            SidecarFile(
                kind="subtitles",
                data=subtitle_path.read_bytes(),
                mime=_subtitle_mime(subtitle_path),
                filename=subtitle_path.name,
            )
        )
    if info_json is not None:
        sidecars.append(
            SidecarFile(
                kind="info_json",
                data=info_json.read_bytes(),
                mime="application/json",
                filename=info_json.name,
            )
        )
    return sidecars


def _select_thumbnail_file(candidates: list[Path]) -> Path | None:
    if not candidates:
        return None

    def sort_key(path: Path) -> tuple[int, str]:
        suffix = path.suffix.lower().lstrip(".")
        try:
            rank = _THUMBNAIL_EXT_PRIORITY.index(suffix)
        except ValueError:
            rank = len(_THUMBNAIL_EXT_PRIORITY)
        return (rank, path.name)

    return sorted(candidates, key=sort_key)[0]


def _thumbnail_mime(path: Path) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed:
        return guessed
    suffix = path.suffix.lower().lstrip(".")
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(suffix, "application/octet-stream")


def _subtitle_mime(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return _SUBTITLE_MIME_BY_EXT.get(suffix, "text/plain")


def _subtitle_language(filename: str, video_id: str) -> str | None:
    stem = filename
    prefix = f"{video_id}."
    if stem.startswith(prefix):
        stem = stem[len(prefix) :]
    parts = stem.split(".")
    if len(parts) >= 2:
        return ".".join(parts[:-1])
    return None


def _select_subtitle_file(
    candidates: list[Path],
    *,
    video_id: str,
    extra_opts: dict[str, Any] | None,
) -> Path | None:
    if not candidates:
        return None
    raw_langs = (extra_opts or {}).get("subtitleslangs")
    if isinstance(raw_langs, list):
        preferred_langs = [str(v) for v in raw_langs if v]
    elif isinstance(raw_langs, str) and raw_langs:
        preferred_langs = [raw_langs]
    else:
        preferred_langs = []
    preferred_langs = [*preferred_langs, "en"]
    ordered = sorted(candidates, key=lambda p: p.name)
    by_lang: dict[str, Path] = {}
    for path in ordered:
        lang = _subtitle_language(path.name, video_id)
        if lang and lang not in by_lang:
            by_lang[lang] = path
    for lang in preferred_langs:
        if lang in by_lang:
            return by_lang[lang]
    return ordered[0]


def _mime_for(path: Path, media_type: str) -> str:
    suffix = path.suffix.lower().lstrip(".")
    guessed = mimetypes.guess_type(path.name)[0]
    if media_type == "audio":
        if suffix == "webm":
            return "audio/webm"
        if suffix in {"m4a", "mp4"}:
            return "audio/mp4"
        if suffix == "opus":
            return "audio/ogg"
        if guessed and guessed.startswith("audio/"):
            return guessed
    if media_type == "video":
        if suffix == "webm":
            return "video/webm"
        if guessed and guessed.startswith("video/"):
            return guessed
    return guessed or "application/octet-stream"


def _require_within_scratch(scratch: Path, relpath: str, label: str) -> Path:
    """Resolve ``relpath`` under ``scratch`` or raise loudly."""
    rel = Path(relpath)
    if rel.is_absolute():
        raise RuntimeError(
            f"media extractor returned an absolute {label} path {relpath!r}; "
            "expected a path relative to the scratch dir"
        )
    scratch_resolved = scratch.resolve()
    abs_path = (scratch_resolved / rel).resolve()
    if abs_path != scratch_resolved and scratch_resolved not in abs_path.parents:
        raise RuntimeError(
            f"media extractor {label} path {relpath!r} escapes the scratch dir"
        )
    return abs_path
