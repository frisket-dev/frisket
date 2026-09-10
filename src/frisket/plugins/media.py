from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from frisket.ops.ytdlp import (
    YTDLP_EXTRA_OPTS_ALLOWED_KEYS,
    validate_extra_opts,
)


class UnmappableExtraOpt(ValueError):
    """A STOP-and-report: an allowlisted extra_opts key has no deterministic
    out-of-process representation. Raised instead of silently dropping the key
    so a coverage gap fails closed rather than changing what a
    caller asked yt-dlp to do."""


# Per-key deterministic representation of every YTDLP_EXTRA_OPTS_ALLOWED_KEYS
# entry as yt-dlp CLI arguments. Three shapes:
#   ("flag", "--x", "--no-x")  boolean -> a flag (True) or its negation (False)
#   ("value", "--x")           scalar  -> "--x", str(value)
#   ("list", "--x")            list    -> "--x", ",".join(values)
# This table is the single source of truth for out-of-process coverage; a key in
# the public allowlist that is missing here is an UnmappableExtraOpt, never a
# silent drop. Kept exhaustive by test_every_allowed_extra_opt_key_is_mapped_*.
_EXTRA_OPT_CLI_MAP: dict[str, tuple[str, ...]] = {
    # subtitles
    "writesubtitles": ("flag", "--write-subs", "--no-write-subs"),
    "writeautomaticsub": ("flag", "--write-auto-subs", "--no-write-auto-subs"),
    "subtitleslangs": ("list", "--sub-langs"),
    "subtitlesformat": ("value", "--sub-format"),
    "allsubtitles": ("flag", "--all-subs", "--no-write-subs"),
    # metadata
    "writeinfojson": ("flag", "--write-info-json", "--no-write-info-json"),
    "writethumbnail": ("flag", "--write-thumbnail", "--no-write-thumbnail"),
    # `writedescription` is intentionally unsupported: the action has no output
    # contract for the standalone `.description` artifact.
    # format / quality
    "merge_output_format": ("value", "--merge-output-format"),
    "prefer_free_formats": (
        "flag",
        "--prefer-free-formats",
        "--no-prefer-free-formats",
    ),
    "format_sort": ("list", "--format-sort"),
    # retries
    "retries": ("value", "--retries"),
    "fragment_retries": ("value", "--fragment-retries"),
    "extractor_retries": ("value", "--extractor-retries"),
    "file_access_retries": ("value", "--file-access-retries"),
    "socket_timeout": ("value", "--socket-timeout"),
    # rate limiting
    "ratelimit": ("value", "--limit-rate"),
    "throttledratelimit": ("value", "--throttled-rate"),
    "sleep_interval": ("value", "--sleep-interval"),
    "max_sleep_interval": ("value", "--max-sleep-interval"),
    "sleep_interval_requests": ("value", "--sleep-requests"),
    "sleep_interval_subtitles": ("value", "--sleep-subtitles"),
}

# Fail fast at import if the public allowlist and the CLI map drift apart: every
# allowed key must have a deterministic representation (no silent drop), and the
# map must not invent keys outside the choke point's allowlist.
# The frozen suite parametrizes over the allowlist; this guard catches drift the
# moment the allowlist changes, not only under test.
_UNMAPPED = YTDLP_EXTRA_OPTS_ALLOWED_KEYS - set(_EXTRA_OPT_CLI_MAP)
_EXTRA = set(_EXTRA_OPT_CLI_MAP) - YTDLP_EXTRA_OPTS_ALLOWED_KEYS
if _UNMAPPED or _EXTRA:  # pragma: no cover - guards a code-edit invariant
    raise RuntimeError(
        "media extra_opts CLI map is out of sync with "
        f"YTDLP_EXTRA_OPTS_ALLOWED_KEYS (unmapped={sorted(_UNMAPPED)}, "
        f"outside-allowlist={sorted(_EXTRA)})"
    )


@dataclass(frozen=True)
class MediaExtraOptsPlan:
    """A deterministic out-of-process representation of a validated ``extra_opts``
    dict. ``covered_keys`` accounts for EVERY provided allowed key (no silent
    drop); ``cli_args`` is the yt-dlp CLI argument vector; ``runner_opts`` is the
    equivalent python-runner ``YoutubeDL`` options dict — the seam admits either
    transport."""

    covered_keys: tuple[str, ...]
    cli_args: tuple[str, ...]
    runner_opts: dict[str, Any] = field(default_factory=dict)


def _cli_value(value: Any) -> str:
    if isinstance(value, bool):
        # Booleans are handled by the flag branch; reaching here would misrepresent
        # the value, so surface it rather than stringify "True".
        raise UnmappableExtraOpt("boolean values are represented as flags, not values")
    return str(value)


def plan_extra_opts_for_runtime(extra_opts: dict[str, Any]) -> MediaExtraOptsPlan:
    """Map a validated ``extra_opts`` dict to a deterministic out-of-process plan.

    REUSES the existing public choke point: ``validate_extra_opts`` rejects any
    disallowed key (and out-of-bounds numeric) exactly as the in-process path
    does — this seam never forks a second allowlist. Every surviving (allowed)
    key is then deterministically represented; an allowed key with no
    representation raises ``UnmappableExtraOpt`` rather than being dropped.
    """
    # Strict: a disallowed key raises ValueError here, same contract as today's
    # in-process path (no allow_safety_forced — a caller-supplied outtmpl/etc is
    # rejected, not silently tolerated).
    validate_extra_opts(extra_opts)

    covered: list[str] = []
    cli_args: list[str] = []
    for key in sorted(extra_opts):
        spec = _EXTRA_OPT_CLI_MAP.get(key)
        if spec is None:
            # Allowlisted (validate_extra_opts passed) but unrepresentable: STOP.
            raise UnmappableExtraOpt(
                f"extra_opts key {key!r} is allowed but has no deterministic "
                "out-of-process representation; refusing to silently drop it "
                "(plugin media transport invariant)"
            )
        value = extra_opts[key]
        shape = spec[0]
        if shape == "flag":
            cli_args.append(spec[1] if value else spec[2])
        elif shape == "value":
            cli_args.extend([spec[1], _cli_value(value)])
        elif shape == "list":
            items = value if isinstance(value, list) else [value]
            cli_args.extend([spec[1], ",".join(str(item) for item in items)])
        else:  # pragma: no cover - table is closed over the three shapes above
            raise UnmappableExtraOpt(f"unknown mapping shape {shape!r} for {key!r}")
        covered.append(key)

    return MediaExtraOptsPlan(
        covered_keys=tuple(covered),
        cli_args=tuple(cli_args),
        # The python-runner transport consumes the already-validated dict directly
        # (yt_dlp.YoutubeDL accepts the Python-API keys as-is); the CLI vector is
        # the equivalent for the subprocess transport.
        runner_opts=dict(extra_opts),
    )


class MediaExtractor(Protocol):
    """The injected transport seam. A real extractor runs the managed-runtime
    yt-dlp (CLI or python-runner) inside the child; tests inject an offline fake.
    It WRITES media + sidecars into ``work_dir`` and returns metadata + the
    written filenames — never bytes."""

    def __call__(self, request: dict[str, Any], work_dir: Path) -> dict[str, Any]: ...


@dataclass(frozen=True)
class MediaHandoff:
    """A file-based handoff from the child to the host.

    Carries RELATIVE paths (under the host-provided scratch dir) + metadata +
    the runtime's receipt evidence — deliberately NO raw-bytes field, so
    no media bytes cross IPC. The host reads the files from the scratch dir and
    ingests them via ``ingest_media_handoff``.
    """

    media_relpath: str
    sidecar_relpaths: tuple[str, ...]
    metadata: dict[str, Any]
    runtime_evidence: dict[str, str]


def _child_request(request: dict[str, Any], plan: MediaExtraOptsPlan) -> dict[str, Any]:
    """The bounded request the child receives. It carries the mapped extra_opts
    plan (CLI args / runner opts) — never a Project/SQLite handle."""
    return {
        "url": request.get("url"),
        "media_type": request.get("media_type", "video"),
        "format_selector": request.get("format_selector"),
        "extra_opts_cli_args": list(plan.cli_args),
        "extra_opts_runner_opts": dict(plan.runner_opts),
        "covered_extra_opts": list(plan.covered_keys),
        # The server-level media egress proxy rides to the child as data, not
        # env: the transport decides how to honor it (--proxy for the CLI).
        "proxy": request.get("proxy"),
    }


def _require_relative_under(name: str, scratch_dir: Path) -> Path:
    """A child-returned filename must be a plain relative path that resolves
    under the scratch dir — never absolute, never an escape."""
    rel = Path(name)
    if rel.is_absolute():
        raise ValueError(f"child returned an absolute path {name!r}; expected relative")
    abs_path = (scratch_dir / rel).resolve()
    scratch_resolved = scratch_dir.resolve()
    if scratch_resolved not in abs_path.parents and abs_path != scratch_resolved:
        raise ValueError(f"child path {name!r} escapes the scratch dir")
    return abs_path


def run_media_download(
    request: dict[str, Any],
    *,
    scratch_dir: Path,
    extractor: MediaExtractor,
) -> MediaHandoff:
    """CHILD-side seam: run a media download out of process into ``scratch_dir``.

    Threads NO Project/SQLite/blob-store handle — the child never touches host
    persistence. The ``extractor`` transport writes the media +
    sidecars and returns their filenames; this seam validates their scratch-
    relative paths and returns metadata plus relative paths only. No media
    bytes are returned, and no artificial size cap is enforced.
    """
    scratch_dir = Path(scratch_dir)
    extra_opts = request.get("extra_opts") or {}
    # Enforce the public choke point in the child path too: a disallowed key
    # fails closed here, before any transport work.
    plan = (
        plan_extra_opts_for_runtime(extra_opts)
        if extra_opts
        else MediaExtraOptsPlan(covered_keys=(), cli_args=())
    )

    result = extractor(_child_request(request, plan), scratch_dir)

    media_name = str(result["media_filename"])
    media_abs = _require_relative_under(media_name, scratch_dir)
    if not media_abs.is_file():
        raise RuntimeError("media extractor did not write the media file")

    sidecar_names = tuple(str(name) for name in result.get("sidecar_filenames") or [])
    for name in sidecar_names:
        sidecar_abs = _require_relative_under(name, scratch_dir)
        if not sidecar_abs.is_file():
            raise RuntimeError(f"media extractor did not write sidecar {name!r}")

    evidence = dict(result.get("runtime_evidence") or {})

    return MediaHandoff(
        media_relpath=media_name,
        sidecar_relpaths=sidecar_names,
        metadata=dict(result.get("metadata") or {}),
        runtime_evidence=evidence,
    )


def _mime_for_name(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def ingest_media_handoff(
    project: Any,
    handoff: MediaHandoff,
    *,
    scratch_dir: Path,
) -> list[str]:
    """HOST-side seam: read the child's scratch files and ingest them into the
    blob store. This is the ONLY seam that touches the Project — the child never
    does. Returns the blob ids the host recorded (media first,
    then sidecars)."""
    scratch_dir = Path(scratch_dir)
    source_url = str(
        handoff.metadata.get("webpage_url") or handoff.metadata.get("url") or ""
    )

    digests: list[str] = []
    ordered = [handoff.media_relpath, *handoff.sidecar_relpaths]
    for rel in ordered:
        abs_path = _require_relative_under(rel, scratch_dir)
        data = abs_path.read_bytes()
        digest = project.add_blob(
            data,
            filename=Path(rel).name,
            mime=_mime_for_name(rel),
            source_url=source_url,
        )
        digests.append(digest)
    return digests
