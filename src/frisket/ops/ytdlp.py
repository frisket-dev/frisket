"""Out-of-process best-effort media extraction through yt-dlp.

The core ``media.*`` acquisition ops run the installed ``yt-dlp`` Python package
in a subprocess and extract via the transport-agnostic ``frisket.plugins.media``
seams
(``run_media_download`` writes media + sidecars into a host scratch dir; this
module reads them back). There is no in-process ``yt_dlp.YoutubeDL`` extraction
here and no second yt-dlp artifact pin: the installed dependency is the sole
execution and version source.

The extra_opts choke point (``validate_extra_opts`` + the allowlist below) is the
one place a caller's options are validated, whichever seam consumes them.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as importlib_metadata
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from frisket.runtime.supervisor import guarded_argv, stop_guard

from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.ops.media_probe import probe_for_ingest
from frisket.ops.ytdlp_outputs import (
    SidecarFile as SidecarFile,
    _collect_sidecar_files,
    _mime_for,
    _partition_cli_outputs,
    _read_json_object,
    _require_within_scratch,
)
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    media_cell,
    owned_media_metadata_document,
)
from frisket.runtime.launch import worker_argv

if TYPE_CHECKING:
    # Runtime imports of frisket.ops.* stay function-level: the ops package
    # init imports modules that import this one back.
    from frisket.ops.egress_policy import MediaEgressPolicy

YTDLP_ENGINE_ID = "yt_dlp"
YOUTUBE_PROVIDER_BLOCKED_CODE = "youtube_provider_blocked"
YOUTUBE_PROVIDER_BLOCKED_MESSAGE = (
    "YouTube blocked this download from the server's network with an anti-bot "
    "challenge. This commonly affects cloud-hosted egress IPs. Upload the media "
    "file instead or run the download from a different network; retrying unchanged "
    "from this server is unlikely to help. If you operate this server, running "
    "`frisket proxy up` on your own computer routes media downloads through "
    "your network connection instead."
)
YTDLP_DOWNLOAD_TIMEOUT_SECONDS = 60 * 60
_YTDLP_PROCESS_SHUTDOWN_TIMEOUT_SECONDS = 5
# How often the download wait loop surfaces to poll cooperative cancellation.
# A yt-dlp download blocks for as long as an hour; without this the child ran
# to completion after a run was cancelled, holding the queue (and, under a
# residential proxy, a live tunnel connection) long after the operator hit
# stop. Coarse on purpose — the cancel signal is a SQLite read.
_YTDLP_CANCEL_POLL_SECONDS = 2.0


class MediaDownloadCancelled(RuntimeError):
    """The run was cancelled while this row's yt-dlp child was still running;
    the process tree has been killed. A ``RuntimeError`` subclass so the row
    executor treats it as an ordinary per-row abort — the map runner's cancel
    fence then drops the row wholesale (no result, no failure)."""


_YTDLP_DIAGNOSTIC_LIMIT = 2_000
# Conservative allowlist of yt-dlp option keys that `extra_opts` may set. This is
# defense-in-depth on top of the downloader's safety-key re-forcing (single file,
# sandboxed temp output): it keeps options that only tune subtitles,
# metadata, format/quality, retries, or rate limiting, and rejects anything that
# could execute code, write outside the sandboxed temp dir, or change the output
# path/template (e.g. `exec`/`exec_cmd`, `paths`, `postprocessors`,
# `external_downloader`, `cookiesfrombrowser`, `outtmpl`).
YTDLP_EXTRA_OPTS_ALLOWED_KEYS = frozenset(
    {
        # subtitles
        "writesubtitles",
        "writeautomaticsub",
        "subtitleslangs",
        "subtitlesformat",
        "allsubtitles",
        # metadata
        "writeinfojson",
        "writethumbnail",
        # Intentionally no `writedescription`: Frisket has no output contract for
        # yt-dlp's free-standing `.description` file. Keep rejecting it rather
        # than silently creating an artifact the action cannot return.
        # format / quality
        "merge_output_format",
        "prefer_free_formats",
        "format_sort",
        # retries
        "retries",
        "fragment_retries",
        "extractor_retries",
        "file_access_retries",
        "socket_timeout",
        # rate limiting
        "ratelimit",
        "throttledratelimit",
        "sleep_interval",
        "max_sleep_interval",
        "sleep_interval_requests",
        "sleep_interval_subtitles",
    }
)

# Inclusive (min, max) bounds for numeric extra_opts knobs, so a hostile or
# mistaken value can't hang a download worker. ratelimit floors avoid a
# crawl-slow (e.g. 1 byte/s) hang.
YTDLP_EXTRA_OPTS_NUMERIC_BOUNDS: dict[str, tuple[float, float]] = {
    "retries": (0, 20),
    "fragment_retries": (0, 20),
    "extractor_retries": (0, 20),
    "file_access_retries": (0, 20),
    "socket_timeout": (1, 120),
    "sleep_interval": (0, 60),
    "max_sleep_interval": (0, 60),
    "sleep_interval_requests": (0, 60),
    "sleep_interval_subtitles": (0, 60),
    "ratelimit": (1024, 1_000_000_000),
    "throttledratelimit": (1024, 1_000_000_000),
}

# Keys that ``download_media`` always re-forces after merging
# extra_opts (single-file, sandboxed temp dir; see the runtime CLI argv
# below). A caller's value for one of these can never take effect, so the
# choke-point check tolerates them as inert; user-facing validation stays
# strict and rejects them so a caller is never silently overridden.
YTDLP_EXTRA_OPTS_SAFETY_FORCED_KEYS = frozenset(
    {
        "format",
        "noplaylist",
        "outtmpl",
        "quiet",
        "no_warnings",
        "noprogress",
    }
)


def _is_extra_opt_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def validate_extra_opts(
    extra_opts: dict[str, Any], *, allow_safety_forced: bool = False
) -> None:
    """Validate a yt-dlp ``extra_opts`` dict against the allowlist and bounds.

    This is the single choke point for extra_opts safety: it runs inside
    ``download_media`` regardless of which caller (contract layer,
    recipe, plugin, or future spec source) constructed the dict, so nothing
    can reach yt-dlp without going through this check.

    ``allow_safety_forced`` tolerates the keys ``download_media``
    re-forces on merge (they are inert by construction). Only the downloader
    itself should set it; user-facing validation keeps the default strict so
    a caller-supplied ``outtmpl``/``noplaylist``/... is rejected loudly
    instead of silently overridden.
    """
    if any(not isinstance(key, str) or not key for key in extra_opts):
        raise ValueError("invalid_params")
    tolerated = (
        YTDLP_EXTRA_OPTS_SAFETY_FORCED_KEYS if allow_safety_forced else frozenset()
    )
    disallowed = sorted(set(extra_opts) - YTDLP_EXTRA_OPTS_ALLOWED_KEYS - tolerated)
    if disallowed:
        raise ValueError(
            "invalid_params: extra_opts contains disallowed key(s) "
            f"{disallowed!r}; allowed keys are "
            f"{sorted(YTDLP_EXTRA_OPTS_ALLOWED_KEYS)!r}"
        )
    # Values must be scalars (or a flat list of scalars); reject nested
    # structures, and cap the numeric knobs so a hostile/mistaken value
    # can't hang a download worker (e.g. sleep_interval=10_000_000).
    for key, raw in extra_opts.items():
        items = raw if isinstance(raw, list) else [raw]
        if isinstance(raw, list) and not all(
            _is_extra_opt_scalar(item) for item in items
        ):
            raise ValueError(f"invalid_params: extra_opts.{key} must be scalars")
        if not isinstance(raw, list) and not _is_extra_opt_scalar(raw):
            raise ValueError(f"invalid_params: extra_opts.{key} must be a scalar")
        bounds = YTDLP_EXTRA_OPTS_NUMERIC_BOUNDS.get(key)
        if bounds is not None:
            low, high = bounds
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"invalid_params: extra_opts.{key} must be a number")
            if not (low <= raw <= high):
                raise ValueError(
                    f"invalid_params: extra_opts.{key} must be between {low} and {high}"
                )


class ManagedRuntimeUnavailable(RuntimeError):
    """Raised when the installed yt-dlp runtime cannot be used for execution."""


@dataclass(frozen=True)
class DownloadedMedia:
    data: bytes
    mime: str
    filename: str
    duration_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sidecars: list[SidecarFile] = field(default_factory=list)
    # The selected runtime's receipt evidence
    # ({runtime_name, resolved_version, artifact_sha256}); the installed package
    # content hash, not only a version string. Empty when the media was produced
    # by an injected extractor that supplied none.
    runtime_evidence: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StoredMedia:
    digest: str
    cell: dict[str, Any]
    metadata: dict[str, Any]


def managed_runtime_status() -> dict[str, Any]:
    """Return installed yt-dlp availability/version without importing yt_dlp.

    The function name is retained for compatibility with the existing action
    catalog seam; the package installed alongside Frisket is now the only
    production runtime and version source.
    """
    try:
        version = importlib_metadata.version("yt-dlp")
    except importlib_metadata.PackageNotFoundError:
        return {
            "available": False,
            "error": "yt-dlp is not installed in Frisket's Python environment.",
            "version": None,
        }
    return {"available": True, "error": None, "version": version}


def download_media(
    url: str,
    *,
    media_type: str = "video",
    format_selector: str | None = None,
    extra_opts: dict[str, Any] | None = None,
    extractor: Any | None = None,
    policy: MediaEgressPolicy | None = None,
    proxy: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> DownloadedMedia:
    """Ask yt-dlp to extract one HTTP(S) URL through a subprocess.

    Direct RSS/media enclosures use ``frisket.enclosures.download_url``.
    Frisket validates only the URL scheme; yt-dlp determines whether the URL is
    supported. ``--no-playlist`` handles URLs that identify both a video and
    playlist, while ``--playlist-items 1`` bounds a playlist-only extractor to
    one item.

    The extraction runs OUT OF the app process: the ``extractor`` transport (the
    ``frisket.plugins.media`` MediaExtractor, called with ``(request, work_dir)``)
    writes the media + sidecars into a host-provided scratch dir and this function
    reads them back — no in-process ``yt_dlp.YoutubeDL``. When ``extractor`` is
    None the default runs the installed yt-dlp dependency through the current
    Python interpreter; tests inject an offline fake. The returned media carries
    the runtime's receipt evidence.

    ``proxy`` is the media egress proxy URL the transport should route through
    (yt-dlp's ``--proxy``); None resolves the server-level setting
    (``frisket.ops.media_proxy``). When one is in use the returned metadata
    carries the boolean fact ``proxied`` — never the URL itself.
    """
    from frisket.plugins.media import run_media_download
    from frisket.features.url_classification import (
        YTDLP_DOWNLOAD_ACTION_KIND,
        classify_url,
    )

    from frisket.ops.egress_policy import server_default_policy
    from frisket.ops.http_urls import is_http_url

    if not is_http_url(url):
        raise ValueError("yt-dlp extraction requires an HTTP(S) URL")
    # yt-dlp's reach cannot be bounded by a domain allowlist — the extractor
    # set is open-ended and the generic extractor accepts any page. KNOWN GAP:
    # the policy check covers only this entry URL. The yt-dlp subprocess
    # follows redirects and re-resolves DNS itself, so a hostile page an
    # operator's server is asked to fetch can still steer the child to a
    # private address after this check; the app cannot re-check those hops.
    # Bounding what the child can reach is container-level egress control,
    # owned by the operator — every surface describing this setting says so.
    if policy is None:
        policy = server_default_policy()
    policy.check_url(url)
    classification = classify_url(url)
    if media_type not in {"audio", "video"}:
        raise ValueError("media_type must be 'audio' or 'video'")
    # The single choke point, enforced before any transport work: a disallowed
    # (or out-of-bounds) key fails closed here, before the runtime is resolved.
    if extra_opts:
        validate_extra_opts(extra_opts)

    if proxy is None:
        from frisket.ops.media_proxy import resolve_media_proxy

        proxy = resolve_media_proxy()

    request = {
        "url": url,
        "media_type": media_type,
        "format_selector": format_selector or _default_format(media_type),
        "extra_opts": dict(extra_opts) if extra_opts else {},
        "proxy": proxy,
    }

    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        transport = (
            extractor
            if extractor is not None
            else _build_default_extractor(should_cancel)
        )
        handoff = run_media_download(request, scratch_dir=scratch, extractor=transport)

        # Belt-and-suspenders scratch-escape guard at the consumption site.
        # frisket.plugins.media.run_media_download already validates the media +
        # every declared sidecar relpath against scratch escape, but this module
        # reads the files, so it re-validates here regardless: a relpath that is
        # absolute or resolves outside the scratch dir is a loud error, never a
        # read from an attacker-chosen path.
        media_path = _require_within_scratch(scratch, handoff.media_relpath, "media")
        for sidecar_rel in handoff.sidecar_relpaths:
            _require_within_scratch(scratch, sidecar_rel, "sidecar")
        if not media_path.is_file():
            raise RuntimeError("yt-dlp extractor did not produce a media file")
        # No byte ceiling by design: an oversized download fails as the real
        # OS error (ENOSPC/MemoryError), not a frisket-invented size refusal.
        data = media_path.read_bytes()
        mime = _mime_for(media_path, media_type)

        info = dict(handoff.metadata or {})
        video_id = str(info.get("yt_dlp_id") or media_path.stem)
        duration = _float_or_none(info.get("duration_seconds"))
        runtime_evidence = _validated_runtime_evidence(handoff.runtime_evidence)
        known_provider = (
            classification.provider
            if classification is not None
            and classification.handler.action_kind == YTDLP_DOWNLOAD_ACTION_KIND
            else None
        )
        extractor_name = str(info.get("extractor") or "").strip().lower()
        metadata = {
            "provider": known_provider or extractor_name or "yt_dlp",
            "yt_dlp_id": info.get("yt_dlp_id"),
            "title": info.get("title"),
            "duration_seconds": duration,
            "extractor": info.get("extractor"),
            "webpage_url": info.get("webpage_url") or url,
            # youtube-channel-backfill-v1 part (b): the durable source of truth
            # the download-time backfill in sdk/ops/ytdlp_download.py reads. A
            # single-video full extraction carries the channel/uploader; the managed
            # runtime surfaces them under the same field names the poll-time row
            # (sources/youtube.py) already uses.
            "channel_id": info.get("channel_id"),
            "channel_title": info.get("channel_title"),
            # Provenance records only THAT a proxy carried the download, never
            # which one: the proxy URL is operator infrastructure and receipts
            # must not echo it.
            "proxied": True if proxy else None,
        }
        metadata = {k: v for k, v in metadata.items() if v is not None}
        if runtime_evidence:
            # Persisted so the resolved runtime content hash reaches the stored
            # blob: add_downloaded_media_blob copies this metadata into the
            # blob's own metadata.
            metadata["managed_runtime"] = runtime_evidence

        sidecars = _collect_sidecar_files(
            scratch,
            video_id=video_id,
            media_path=media_path,
            extra_opts=extra_opts,
            declared_sidecars=handoff.sidecar_relpaths,
        )
        return DownloadedMedia(
            data=data,
            mime=mime,
            filename=media_path.name,
            duration_seconds=duration,
            metadata=metadata,
            sidecars=sidecars,
            runtime_evidence=runtime_evidence,
        )


def add_downloaded_media_blob(
    project: Any,
    download: DownloadedMedia,
    *,
    source_url: str,
) -> StoredMedia:
    acquisition_metadata = dict(download.metadata)
    if download.duration_seconds is not None:
        acquisition_metadata.setdefault("duration_seconds", download.duration_seconds)
    if download.runtime_evidence:
        # Runtime content-hash receipt evidence rides in the acquisition namespace.
        acquisition_metadata.setdefault(
            "managed_runtime", dict(download.runtime_evidence)
        )
    return _add_media_blob(
        project,
        data=download.data,
        mime=download.mime,
        filename=download.filename,
        source_url=source_url,
        acquisition_metadata=acquisition_metadata,
    )


def add_sidecar_media_blob(
    project: Any,
    sidecar: SidecarFile,
    *,
    source_url: str,
) -> StoredMedia:
    """Store one yt-dlp sidecar (thumbnail/subtitles/info JSON) as a project blob.

    Reuses the same blob-store + metadata-probe + media-cell path as
    ``add_downloaded_media_blob`` so sidecar cells render with the same
    machinery as the main media cell.
    """
    return _add_media_blob(
        project,
        data=sidecar.data,
        mime=sidecar.mime,
        filename=sidecar.filename,
        source_url=source_url,
    )


def _add_media_blob(
    project: Any,
    *,
    data: bytes,
    mime: str,
    filename: str,
    source_url: str,
    acquisition_metadata: dict[str, Any] | None = None,
) -> StoredMedia:
    digest = project.add_blob(
        data,
        filename=filename,
        mime=mime,
        source_url=source_url,
    )
    with project.materialize_blob(digest) as path:
        metadata = probe_for_ingest(
            Path(path),
            mime=mime,
            filename=filename,
            digest=digest,
        )
    acquisition = {
        key: value
        for key, value in (acquisition_metadata or {}).items()
        if value is not None
    }
    MediaBlobStore(project).update_metadata(
        digest,
        owned_media_metadata_document(
            probe=metadata,
            acquisition=acquisition if acquisition else None,
        ),
    )
    cell = media_cell(digest, mime=mime, filename=filename)
    reported_metadata = dict(metadata)
    reported_metadata.update(acquisition)
    return StoredMedia(digest=digest, cell=cell, metadata=reported_metadata)


def _default_format(media_type: str) -> str:
    if media_type == "audio":
        return "bestaudio/best"
    return "bestvideo*+bestaudio/best"


# Installed yt-dlp package resolution. No version literal lives here: the exact
# environment version comes from uv.lock for source/image installs and from the
# package resolver for wheel installs.


def _build_default_extractor(
    should_cancel: Callable[[], bool] | None = None,
) -> Any:
    """Build the production extractor from Frisket's installed dependency.

    ``python -I -m yt_dlp`` keeps extraction out of the app process while using
    the exact package in the current venv/image. Receipt evidence identifies the
    installed package files that command will execute. ``should_cancel`` rides
    into the extractor so a cancelled run aborts an in-flight download.
    """
    evidence = _installed_ytdlp_evidence()
    return _installed_ytdlp_cli_extractor(evidence, should_cancel)


@lru_cache(maxsize=1)
def _installed_ytdlp_evidence() -> dict[str, str]:
    """Return version and content identity for the installed yt-dlp package.

    Only ``yt_dlp/**`` is hashed. Distribution inventories can also contain
    generated entry-point scripts outside site-packages; those embed install
    paths and may be symlinks even though ``python -m yt_dlp`` never executes
    them. Hashing the actual package tree makes the receipt stable and faithful
    to the production command. The result is cached because it cannot change
    safely without replacing/restarting the Frisket environment.
    """
    try:
        distribution = importlib_metadata.distribution("yt-dlp")
    except importlib_metadata.PackageNotFoundError as exc:
        raise ManagedRuntimeUnavailable(
            "yt-dlp is unavailable in Frisket's Python environment"
        ) from exc
    package_files = sorted(
        (
            relative
            for relative in distribution.files or ()
            if relative.parts
            and relative.parts[0] == "yt_dlp"
            and ".." not in relative.parts
        ),
        key=str,
    )
    if not package_files:
        raise ManagedRuntimeUnavailable(
            "installed yt-dlp distribution has no verifiable package files"
        )
    digest = hashlib.sha256()
    for relative in package_files:
        path = Path(distribution.locate_file(relative))
        if path.is_symlink() or not path.is_file():
            raise ManagedRuntimeUnavailable(
                f"installed yt-dlp package file is missing or unsafe: {relative}"
            )
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return {
        "runtime_name": "yt-dlp",
        "resolved_version": distribution.version,
        "artifact_sha256": digest.hexdigest(),
    }


def _admin_ytdlp_config_paths() -> tuple[Path, ...]:
    """Admin-managed yt-dlp config files, in yt-dlp's own precedence order.

    Covers yt-dlp's User and System slots. Its Portable and Home slots are
    excluded by design: Home expands ``paths['home']``, which defaults to empty
    and therefore resolves against the process CWD, so anything able to write
    into the worker's working directory could inject options into the child.
    """
    candidates: list[Path] = []
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    candidates.append(config_home / "yt-dlp.conf")
    candidates.append(config_home / "yt-dlp" / "config")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "yt-dlp.conf")
        candidates.append(Path(appdata) / "yt-dlp" / "config")
    candidates.append(Path("/etc/yt-dlp.conf"))
    candidates.append(Path("/etc/yt-dlp/config"))
    return tuple(candidates)


def _admin_ytdlp_config_args() -> list[str]:
    """``--config-locations`` arguments for each admin config that exists.

    yt-dlp rejects a ``--config-locations`` path that does not exist with a
    fatal argument error, so candidates must be filtered before reaching argv.
    """
    args: list[str] = []
    for path in _admin_ytdlp_config_paths():
        if path.is_file():
            args.extend(["--config-locations", str(path)])
    return args


def _installed_ytdlp_cli_extractor(
    evidence: dict[str, str],
    should_cancel: Callable[[], bool] | None = None,
):
    """Bind the installed yt-dlp module to the scratch-file handoff.

    ``should_cancel`` (the run's cooperative-cancel probe) is captured here so
    the blocking download wait can abort a killed run's child promptly rather
    than after the full download timeout."""

    def _extract(request: dict[str, Any], work_dir: Path) -> dict[str, Any]:
        work_dir = Path(work_dir)
        outtmpl = str(work_dir / "%(id)s.%(ext)s")
        # Admin-managed config (notably cookies for authenticated/age-restricted
        # media) is honored only through paths frisket names explicitly. yt-dlp's
        # own discovery must stay off: its Home slot resolves relative to the
        # process CWD, and a config there can set --exec, which no command-line
        # argument below can override. The media egress proxy travels only as
        # the explicit --proxy argument, which outranks any config-file value.
        proxy = request.get("proxy")
        argv = worker_argv(
            "yt-dlp",
            "--ignore-config",
            *_admin_ytdlp_config_args(),
            "--no-plugin-dirs",
            *(["--proxy", str(proxy)] if proxy else []),
            request["url"],
            "-f",
            request.get("format_selector") or _default_format(request["media_type"]),
            "-o",
            outtmpl,
            "--no-playlist",
            "--playlist-items",
            "1",
            "--no-progress",
            "--no-warnings",
            "--print-to-file",
            "%()j",
            str(work_dir / "info.json"),
            *request.get("extra_opts_cli_args", []),
        )
        process_group_kwargs: dict[str, Any]
        if os.name == "nt":
            process_group_kwargs = {
                "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            }
        else:
            process_group_kwargs = {"start_new_session": True}
        proc = subprocess.Popen(  # noqa: S603
            guarded_argv(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **process_group_kwargs,
        )
        try:
            stdout, stderr = _communicate_until_cancel_or_deadline(proc, should_cancel)
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(proc)
            try:
                drained_stdout, drained_stderr = proc.communicate(
                    timeout=_YTDLP_PROCESS_SHUTDOWN_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired as drain_exc:
                # A descendant outside the process group may still own a pipe.
                # Do not let diagnostic draining defeat the total deadline.
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=_YTDLP_PROCESS_SHUTDOWN_TIMEOUT_SECONDS)
                except (OSError, subprocess.SubprocessError):
                    pass
                drained_stdout = drain_exc.output
                drained_stderr = drain_exc.stderr
            detail = _bounded_ytdlp_diagnostic(
                drained_stderr,
                exc.stderr,
                drained_stdout,
                exc.output,
                fallback="no diagnostic output",
            )
            raise RuntimeError(
                f"yt-dlp runtime exceeded {YTDLP_DOWNLOAD_TIMEOUT_SECONDS} seconds: "
                f"{detail}"
            ) from exc
        if proc.returncode != 0:
            detail = _bounded_ytdlp_diagnostic(
                stderr,
                stdout,
                fallback=f"exit code {proc.returncode}",
            )
            raise _ytdlp_process_error(detail)
        info_path = work_dir / "info.json"
        info = _read_json_object(info_path)
        media_name, sidecar_names = _partition_cli_outputs(work_dir)
        return {
            "media_filename": media_name,
            "sidecar_filenames": sidecar_names,
            "metadata": {
                "yt_dlp_id": info.get("id"),
                "title": info.get("title"),
                "duration_seconds": info.get("duration"),
                "extractor": info.get("extractor_key") or info.get("extractor"),
                "webpage_url": info.get("webpage_url") or request.get("url"),
                "channel_id": info.get("channel_id") or info.get("uploader_id"),
                "channel_title": info.get("channel") or info.get("uploader"),
            },
            "runtime_evidence": dict(evidence),
        }

    return _extract


def _ytdlp_process_error(detail: str) -> HostedEngineError | RuntimeError:
    """Translate YouTube's stable anti-bot response without exposing stderr."""
    normalized = " ".join(detail.replace("’", "'").lower().split())
    if "sign in to confirm" in normalized and "not a bot" in normalized:
        return HostedEngineError(
            code=YOUTUBE_PROVIDER_BLOCKED_CODE,
            message=YOUTUBE_PROVIDER_BLOCKED_MESSAGE,
            retryable=False,
        )
    return RuntimeError(f"yt-dlp runtime failed: {detail}")


def _communicate_until_cancel_or_deadline(
    proc: subprocess.Popen[str],
    should_cancel: Callable[[], bool] | None,
) -> tuple[str, str]:
    """Drain the child to completion, honoring cooperative cancellation.

    Behaves like ``proc.communicate(timeout=YTDLP_DOWNLOAD_TIMEOUT_SECONDS)``
    — same pipe draining, same ``TimeoutExpired`` on the overall deadline — but
    surfaces every ``_YTDLP_CANCEL_POLL_SECONDS`` to check ``should_cancel``.
    On a cancel it kills the process tree and raises ``MediaDownloadCancelled``
    instead of blocking for the up-to-an-hour download. Retrying
    ``communicate`` after a ``TimeoutExpired`` is the documented,
    output-preserving pattern, so the accumulated stdout/stderr is intact on
    both the normal return and the deadline ``TimeoutExpired`` re-raise.
    """
    deadline = time.monotonic() + YTDLP_DOWNLOAD_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Past the overall deadline: one final zero-timeout poll produces
            # the genuine TimeoutExpired (carrying drained output) the caller's
            # existing timeout branch expects.
            return proc.communicate(timeout=0)
        try:
            return proc.communicate(timeout=min(_YTDLP_CANCEL_POLL_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            if should_cancel is not None and should_cancel():
                _kill_process_tree(proc)
                try:
                    proc.communicate(timeout=_YTDLP_PROCESS_SHUTDOWN_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                raise MediaDownloadCancelled(
                    "media download cancelled by run cancellation"
                ) from None
            # Not cancelled and not past the deadline — keep draining.
            continue


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Best-effort termination of yt-dlp and descendants such as ffmpeg."""
    if os.name == "posix":
        stop_guard(proc)
        return
    if os.name == "nt":
        try:
            completed = subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_YTDLP_PROCESS_SHUTDOWN_TIMEOUT_SECONDS,
                check=False,
            )
            if completed.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _bounded_ytdlp_diagnostic(
    *outputs: str | bytes | None,
    fallback: str,
) -> str:
    """Return the useful tail of stderr/stdout without bloating row receipts."""
    for output in outputs:
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        detail = output.strip() if isinstance(output, str) else ""
        if detail:
            return detail[-_YTDLP_DIAGNOSTIC_LIMIT:]
    return fallback


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _validated_runtime_evidence(evidence: dict[str, str] | None) -> dict[str, str]:
    """Shape-check the runtime's receipt evidence before it is persisted.

    Empty evidence is legitimate (an injected extractor may supply none), so it
    passes through untouched. When present, the three receipt fields must be
    well-formed -- ``runtime_name``/``resolved_version`` non-empty strings and
    ``artifact_sha256`` a bare lowercase 64-char hex digest -- so malformed evidence fails
    loudly here rather than being written as garbage into the run's receipts.
    """
    evidence = dict(evidence or {})
    if not evidence:
        return {}
    for key in ("runtime_name", "resolved_version"):
        value = evidence.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"runtime evidence {key!r} must be a non-empty string; "
                f"refusing to persist malformed receipt evidence: {evidence!r}"
            )
    sha = evidence.get("artifact_sha256")
    if not isinstance(sha, str) or not _SHA256_HEX_RE.fullmatch(sha):
        raise RuntimeError(
            "runtime evidence artifact_sha256 must be a 64-char hex "
            f"content hash; refusing to persist malformed receipt evidence: {evidence!r}"
        )
    return evidence
