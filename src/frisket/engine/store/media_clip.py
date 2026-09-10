from __future__ import annotations

import mimetypes
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.engine.store.evidence import citation_temporal_runs
from frisket.engine.store.project import Project

MAX_CLIP_DURATION_MS = 10 * 60 * 1000
DEFAULT_PAD_MS = 500
MAX_PAD_MS = 5_000
_CLIP_WALL_SECONDS = 60


class ClipError(Exception):
    """A clip request failed a guard or the cut itself failed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ClipSource:
    span_stable_id: str
    blob_hash: str
    mime: str
    filename: str | None
    artifact_duration_ms: int | None
    start_ms: int
    end_ms: int
    label: str


def resolve_clip_source(project: Project, span_stable_id: str) -> ClipSource:
    """Look up a temporal span's clip source, enforcing the guard.

    Raises ``ClipError`` with a stable ``code`` for every failure mode so
    the route layer can map it to the right HTTP status without knowing
    the schema.
    """
    row = project.db.execute(
        """
        SELECT sp.stable_id, sp.span_kind, sp.start_ms, sp.end_ms,
               art.artifact_kind, art.media_type, art.blob_hash,
               art.title, art.filename, art.duration_ms
        FROM source_spans sp
        JOIN source_artifacts art ON art.id = sp.artifact_id
        WHERE sp.stable_id = ?
        """,
        (span_stable_id,),
    ).fetchone()
    if row is None:
        raise ClipError(
            "span_not_found", f"no source span with stable_id {span_stable_id!r}"
        )
    if row["span_kind"] != "temporal":
        raise ClipError(
            "not_temporal", "download clip is only available for temporal spans"
        )
    if row["artifact_kind"] != "av":
        raise ClipError(
            "not_av", "download clip requires an audio/video source artifact"
        )
    if row["start_ms"] is None or row["end_ms"] is None:
        raise ClipError("no_range", "span has no start_ms/end_ms to clip")
    blob_hash = row["blob_hash"]
    if not blob_hash:
        raise ClipError("no_blob", "span's artifact has no project blob")
    return ClipSource(
        span_stable_id=str(row["stable_id"]),
        blob_hash=blob_hash,
        mime=row["media_type"] or "application/octet-stream",
        filename=row["filename"],
        artifact_duration_ms=row["duration_ms"],
        start_ms=int(row["start_ms"]),
        end_ms=int(row["end_ms"]),
        label=row["title"] or row["filename"] or blob_hash,
    )


def resolve_link_run_clip_source(
    project: Project, evidence_link_ref: int | str, run_index: int
) -> ClipSource:
    """citation-span-runs-v1: look up run ``run_index`` of a citation link's
    contiguous cited-span runs (:func:`frisket.store.evidence.
    citation_temporal_runs` -- the SAME grouping the viewer payload used to
    render the run, so the clip a user downloads always matches what they
    saw) and resolve it to a clippable source spanning the run's full
    range -- start of its first span to end of its last. Reuses the
    per-span guard's AV/blob checks; ``resolve_clip_source``'s single-span
    guard does not apply here (citation_temporal_runs already filters to
    ``artifact_kind='av'`` temporal spans), so this only re-checks the parts
    a run adds: does the run exist, does its artifact reference a blob.
    """
    try:
        runs = citation_temporal_runs(project, evidence_link_ref)
    except KeyError as exc:
        raise ClipError(
            "evidence_link_not_found", f"no evidence link {evidence_link_ref!r}"
        ) from exc
    if run_index < 0 or run_index >= len(runs):
        raise ClipError(
            "run_not_found",
            f"evidence link {evidence_link_ref!r} has no run at index {run_index}",
        )
    run = runs[run_index]
    blob_hash = run["blob_hash"]
    if not blob_hash:
        raise ClipError("no_blob", "run's artifact has no project blob")
    return ClipSource(
        span_stable_id=run["span_stable_ids"][0],
        blob_hash=blob_hash,
        mime=run["media_type"] or "application/octet-stream",
        filename=run["filename"],
        artifact_duration_ms=run["duration_ms"],
        start_ms=int(run["start_ms"]),
        end_ms=int(run["end_ms"]),
        label=run["title"] or run["filename"] or blob_hash,
    )


def padded_range(source: ClipSource, *, pad_ms: int) -> tuple[int, int]:
    """Apply padding + the artifact's known duration, then enforce the cap.

    Padding is symmetric and clamped to ``[0, artifact_duration_ms]`` when
    the duration is known; unknown duration only clamps the lower bound
    (never guesses an upper bound). Raises ``ClipError("clip_too_long", …)``
    rather than silently truncating an over-cap request.
    """
    pad_ms = max(0, min(pad_ms, MAX_PAD_MS))
    start_ms = max(0, source.start_ms - pad_ms)
    end_ms = source.end_ms + pad_ms
    if source.artifact_duration_ms is not None:
        end_ms = min(end_ms, source.artifact_duration_ms)
    if end_ms <= start_ms:
        raise ClipError("empty_range", "padded clip range is empty")
    if end_ms - start_ms > MAX_CLIP_DURATION_MS:
        raise ClipError(
            "clip_too_long",
            f"requested clip ({end_ms - start_ms}ms) exceeds the "
            f"{MAX_CLIP_DURATION_MS}ms cap",
        )
    return start_ms, end_ms


async def cut_clip(
    source: ClipSource,
    *,
    source_path: Path,
    start_ms: int,
    end_ms: int,
    out_path: Path,
) -> None:
    """Cut ``[start_ms, end_ms)`` from a leased source into ``out_path``.

    Output-seeking (``-ss``/``-to`` placed after ``-i``) with ``-c copy``:
    accurate for audio (no re-encode, near-sample-exact splice); for video
    a stream-copy cut can only land on the nearest keyframe at/after the
    requested start (ffmpeg's own stream-copy limitation) -- accepted for
    v1 rather than paying a re-encode; measured clip provenance records the
    selected range.
    """
    argv = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-to",
        f"{end_ms / 1000:.3f}",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(out_path),
    ]
    result = await run_sandboxed(
        argv,
        policy=SandboxPolicy(
            wall_seconds=_CLIP_WALL_SECONDS,
            allow_network=False,
            env_passthrough=["PATH"],
        ),
        scratch_dir=out_path.parent,
    )
    if not result.ok or not out_path.is_file() or out_path.stat().st_size == 0:
        raise ClipError("ffmpeg_failed", f"ffmpeg clip failed: {result.stderr[:300]}")


async def cut_and_store_clip(
    project: Project,
    source: ClipSource,
    *,
    start_ms: int,
    end_ms: int,
    op: str = "ffmpeg_clip",
) -> str:
    """Cut ``[start_ms, end_ms)`` from ``source``'s blob, persist the result
    as a new project blob, and record blob-to-blob derivation lineage back
    to ``source.blob_hash`` (provenance-derived-audio-lineage-v1). This is
    the creation site for a DERIVED clip blob -- distinct from the download
    routes' ``cut_clip``, which streams an ephemeral cut straight to the
    client and never lands it in blob storage. Returns the derived blob's
    digest. Fails closed (``BlobNotFoundError`` via
    ``Project.record_blob_derivation``) if ``source.blob_hash`` is not
    actually in this project's blob store.
    """
    scratch = Path(tempfile.mkdtemp(prefix="frisket-clip-lineage-"))
    try:
        out_path = scratch / f"clip{output_suffix(source)}"
        with project.materialize_blob(source.blob_hash) as source_path:
            await cut_clip(
                source,
                source_path=Path(source_path),
                start_ms=start_ms,
                end_ms=end_ms,
                out_path=out_path,
            )
        data = out_path.read_bytes()
        digest = project.add_blob(
            data,
            filename=clip_filename(source, start_ms=start_ms, end_ms=end_ms),
            mime=source.mime,
            commit=False,
        )
        project.record_blob_derivation(
            derived_hash=digest,
            source_hash=source.blob_hash,
            op=op,
            params={"start_ms": start_ms, "end_ms": end_ms},
            commit=False,
        )
        project.db.commit()
        return digest
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def output_suffix(source: ClipSource) -> str:
    if source.filename:
        suffix = Path(source.filename).suffix
        if suffix:
            return suffix
    guessed = mimetypes.guess_extension(source.mime)
    return guessed or ".bin"


def clip_filename(source: ClipSource, *, start_ms: int, end_ms: int) -> str:
    """A sensible download filename: source name + the clipped range."""
    stem = _sanitize_filename_component(Path(source.label).stem or source.label)
    range_label = _format_range_label(start_ms, end_ms)
    return f"{stem}_{range_label}{output_suffix(source)}"


def _format_range_label(start_ms: int, end_ms: int) -> str:
    return f"{_format_ms(start_ms)}-{_format_ms(end_ms)}"


def _format_ms(ms: int) -> str:
    total_seconds = ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s"


def _sanitize_filename_component(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-")
    return cleaned[:80] or "clip"


def clip_error_status(error: ClipError) -> int:
    if error.code in {
        "span_not_found",
        "blob_missing",
        "evidence_link_not_found",
        "run_not_found",
    }:
        return 404
    if error.code == "ffmpeg_failed":
        return 500
    return 400


def clip_error_details(span_stable_id: str) -> dict[str, Any]:
    return {"span_stable_id": span_stable_id}
