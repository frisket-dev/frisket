"""Artifact-local timeline anchors and exact V1 clip mappings.

Media anchors are derived from immutable bytes and duration.  Transcript
anchors additionally persist the hash of their timestamped segments because
that fact is not present on ``source_artifacts``.  Derived anchors are computed
from the single complete, rate-1 mapping row V1 supports.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for annotations
    from frisket.engine.store.project import Project


TIMELINE_FINGERPRINT_SCHEMA_VERSION = "frisket.timeline_fingerprint.v1"
TRANSCRIPT_TIMELINE_SCHEMA_VERSION = "frisket.transcript_timeline.v1"
TIMELINE_SEGMENTS_HASH_SCHEMA_VERSION = "frisket.timeline_segments_hash.v1"

MEDIA_CLOCK_POLICY = "normalized_zero.v1"
TRANSCRIPT_CLOCK_POLICY = "timestamped_segments.v1"
DERIVED_CLOCK_POLICY = "artifact_mapping.v1"

MAX_TIMELINE_TRAVERSAL_DEPTH = 32
_PRECISIONS = frozenset({"frame_accurate", "sample_accurate", "exact"})
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class TimelineError(ValueError):
    """A stable temporal-storage failure.

    ``code`` uses a stable public vocabulary so action families can translate
    the error without parsing a message.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TimelineAnchor:
    artifact_id: int
    artifact_stable_id: str
    fingerprint: str
    duration_ms: int
    kind: Literal["media", "transcript", "derived"]
    clock_policy: str

    def wire_value(self) -> dict[str, Any]:
        """The source-bound anchor embedded in a canonical temporal value."""

        return {
            "artifact_stable_id": self.artifact_stable_id,
            "fingerprint": self.fingerprint,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class EphemeralMediaClock:
    """A preview-owned root clock, with no persisted artifact identity."""

    preview_clock_id: str
    fingerprint: str
    duration_ms: int

    def wire_value(self) -> dict[str, Any]:
        return {
            "preview_clock_id": self.preview_clock_id,
            "fingerprint": self.fingerprint,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class RootClockExtent:
    root_artifact_fingerprint: str
    root_start_ms: int
    root_end_ms: int


def root_clock_extent(
    project: Project,
    clock: TimelineAnchor | EphemeralMediaClock,
    *,
    start_ms: int = 0,
    end_ms: int | None = None,
) -> RootClockExtent:
    """Resolve clock coordinates without inventing artifact IDs for scratch media."""
    end = clock.duration_ms if end_ms is None else end_ms
    if isinstance(clock, EphemeralMediaClock):
        if start_ms < 0 or end <= start_ms or end > clock.duration_ms:
            raise TimelineError("range_out_of_bounds", "range exceeds preview clock")
        return RootClockExtent(clock.fingerprint, start_ms, end)
    resolved = map_range_to_root(
        project, artifact_id=clock.artifact_id, start_ms=start_ms, end_ms=end
    )
    return RootClockExtent(
        resolved.root_artifact_fingerprint,
        resolved.root_start_ms,
        resolved.root_end_ms,
    )


@dataclass(frozen=True)
class TimelineLease:
    """Resolved source-cell snapshot used by temporal actions.

    The blob itself remains owned by the project's blob store.  "Lease" here
    means the immutable identity/value snapshot; callers that need a filesystem
    path still use ``Project.materialize_blob`` for its normal context lease.
    """

    anchor: TimelineAnchor | EphemeralMediaClock
    blob_hash: str
    media_type: str
    media_kind: Literal["audio", "video"]
    filename: str | None
    source_cell_ref: dict[str, int]
    current_value_ref: dict[str, Any]
    source_value_hash: str


@dataclass(frozen=True)
class TimelineSource:
    """Read-only admission of an exact captured cell and its existing anchor."""

    anchor: TimelineAnchor | None
    duration_ms: int
    blob_hash: str
    media_type: str
    media_kind: Literal["audio", "video"]
    filename: str | None
    source_cell_ref: dict[str, int]
    current_value_ref: dict[str, Any]
    source_value_hash: str


@dataclass(frozen=True)
class ArtifactTimelineSegment:
    id: int
    derived_artifact_id: int
    ordinal: int
    derived_start_ms: int
    derived_end_ms: int
    source_artifact_id: int
    source_start_ms: int
    source_end_ms: int
    rate_num: int
    rate_den: int
    precision: str
    producer_run_id: int | None
    receipt_id: str | None
    params: dict[str, Any]


@dataclass(frozen=True)
class ResolvedTimelineRange:
    artifact_id: int
    start_ms: int
    end_ms: int
    root_artifact_id: int
    root_artifact_stable_id: str
    root_artifact_fingerprint: str
    root_start_ms: int
    root_end_ms: int
    path: tuple[ArtifactTimelineSegment, ...]


def canonical_json_hash(value: Any) -> str:
    """SHA-256 of canonical JSON (UTF-8, sorted keys, no insignificant space)."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TimelineError(
            "invalid_temporal_value", "timeline identity payload is not JSON-safe"
        ) from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def media_timeline_fingerprint(*, blob_hash: str, duration_ms: int) -> str:
    duration = _positive_int(duration_ms, "duration_ms")
    if not isinstance(blob_hash, str) or not blob_hash:
        raise TimelineError("timeline_not_found", "media timeline requires a blob hash")
    return canonical_json_hash(
        {
            "schema_version": TIMELINE_FINGERPRINT_SCHEMA_VERSION,
            "kind": "media",
            "blob_hash": blob_hash,
            "duration_ms": duration,
            "clock_policy": MEDIA_CLOCK_POLICY,
        }
    )


def ordered_segments_hash(segments: Sequence[Any]) -> str:
    if isinstance(segments, (str, bytes, bytearray)) or not isinstance(
        segments, Sequence
    ):
        raise TimelineError(
            "invalid_temporal_value", "ordered transcript segments must be a list"
        )
    return canonical_json_hash(
        {
            "schema_version": TIMELINE_SEGMENTS_HASH_SCHEMA_VERSION,
            "items": list(segments),
        }
    )


def transcript_timeline_fingerprint(
    *,
    artifact_stable_id: str,
    duration_ms: int,
    ordered_segments_hash_value: str,
) -> str:
    _validate_artifact_stable_id(artifact_stable_id)
    duration = _positive_int(duration_ms, "duration_ms")
    segments_hash = _fingerprint(ordered_segments_hash_value, "ordered_segments_hash")
    return canonical_json_hash(
        {
            "schema_version": TIMELINE_FINGERPRINT_SCHEMA_VERSION,
            "kind": "transcript",
            "artifact_stable_id": artifact_stable_id,
            "duration_ms": duration,
            "ordered_segments_hash": segments_hash,
            "clock_policy": TRANSCRIPT_CLOCK_POLICY,
        }
    )


def resolve_artifact_timeline(project: Project, artifact_id: int) -> TimelineAnchor:
    """Resolve an artifact's canonical timeline from its current stored facts."""

    return _compute_timeline(project, _positive_int(artifact_id, "artifact_id"))


def validate_timeline_anchor(
    project: Project,
    anchor: Mapping[str, Any] | TimelineAnchor,
) -> TimelineAnchor:
    """Contextually validate a persisted temporal value's source anchor.

    Structural temporal-value validation cannot query project artifacts.  This
    is the store-level ingress check: resolve the exact stable artifact,
    recompute its immutable timeline identity (including blob/mapping
    integrity), and require the stored fingerprint and any supplied duration
    to match the value's anchor exactly.  An omitted duration is safe because
    the fingerprint must still resolve against a finalized artifact.
    """

    if isinstance(anchor, TimelineAnchor):
        artifact_stable_id = anchor.artifact_stable_id
        fingerprint = anchor.fingerprint
        duration_ms = anchor.duration_ms
    elif isinstance(anchor, Mapping):
        allowed = {"artifact_stable_id", "fingerprint", "duration_ms"}
        unknown = set(anchor) - allowed
        if unknown:
            raise TimelineError(
                "invalid_temporal_value",
                "timeline anchor contains unknown fields: "
                + ", ".join(sorted(unknown)),
            )
        artifact_stable_id = anchor.get("artifact_stable_id")
        fingerprint = anchor.get("fingerprint")
        duration_ms = anchor.get("duration_ms")
    else:
        raise TimelineError(
            "invalid_temporal_value", "timeline anchor must be an object"
        )

    stable_id = _validate_artifact_stable_id(artifact_stable_id)
    expected_fingerprint = _fingerprint(fingerprint, "fingerprint")
    if duration_ms is None:
        expected_duration = None
    else:
        expected_duration = _positive_int(duration_ms, "duration_ms")
    row = project.db.execute(
        "SELECT id FROM source_artifacts WHERE stable_id=?", (stable_id,)
    ).fetchone()
    if row is None:
        raise TimelineError(
            "timeline_not_found", f"timeline artifact {stable_id!r} does not exist"
        )
    resolved = resolve_artifact_timeline(project, int(row["id"]))
    if resolved.fingerprint != expected_fingerprint:
        raise TimelineError(
            "timeline_stale", "timeline anchor fingerprint does not match artifact"
        )
    if expected_duration is not None and resolved.duration_ms != expected_duration:
        raise TimelineError(
            "timeline_stale", "timeline anchor duration does not match artifact"
        )
    return resolved


def ensure_artifact_timeline(project: Project, artifact_id: int) -> TimelineAnchor:
    """Compatibility name for resolving a canonical timeline anchor.

    Media and derived identity is fully derivable from the artifact and its V1
    mapping, so there is no second identity record to persist.
    """

    return resolve_artifact_timeline(project, artifact_id)


def _select_media_timeline(
    project: Project,
    *,
    blob_hash: str,
    duration_ms: int,
    source_scope: Mapping[str, int | None],
    stable_id: str | None = None,
) -> TimelineAnchor | None:
    """Select without writes, including derived-lineage validation and refusal."""
    if stable_id is not None:
        claimed = project.db.execute(
            "SELECT * FROM source_artifacts WHERE stable_id=?", (stable_id,)
        ).fetchone()
        if claimed is not None:
            if not _media_artifact_matches(
                claimed,
                blob_hash=blob_hash,
                duration_ms=duration_ms,
                source_scope=source_scope,
            ):
                raise TimelineError(
                    "timeline_stale",
                    "requested artifact stable ID already belongs to another artifact",
                )
            return resolve_artifact_timeline(project, int(claimed["id"]))
    candidates = project.db.execute(
        "SELECT * FROM source_artifacts "
        "WHERE artifact_kind='av' AND blob_hash=? AND duration_ms=? "
        "AND source_sheet_id IS ? AND source_row_id IS ? "
        "AND source_column_id IS ? ORDER BY id",
        (
            blob_hash,
            duration_ms,
            source_scope["source_sheet_id"],
            source_scope["source_row_id"],
            source_scope["source_column_id"],
        ),
    ).fetchall()
    # A clip's exact cell may also contain an unmapped artifact. Prefer its
    # derived identity so downstream timeline resolution retains the lineage.
    mapped = [
        candidate
        for candidate in candidates
        if _mapping_rows(project, int(candidate["id"]))
    ]
    if len(mapped) > 1:
        raise TimelineError(
            "ambiguous_time_mapping",
            "source cell has multiple derived timeline artifacts",
        )
    selected = mapped or candidates
    return (
        resolve_artifact_timeline(project, int(selected[0]["id"])) if selected else None
    )


def ensure_media_timeline(
    project: Project,
    *,
    blob_hash: str,
    duration_ms: int,
    media_type: str | None = None,
    filename: str | None = None,
    stable_id: str | None = None,
    source_sheet_id: int | None = None,
    source_row_id: int | None = None,
    source_column_id: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> TimelineAnchor:
    """Resolve or create the media artifact for exact bytes, duration and cell."""

    duration = _positive_int(duration_ms, "duration_ms")
    if stable_id is not None:
        _validate_artifact_stable_id(stable_id)
    if metadata is not None and "timeline" in metadata:
        raise TimelineError(
            "invalid_temporal_value",
            "metadata.timeline is reserved for host-owned timeline identity",
        )
    source_scope = _source_scope(
        source_sheet_id=source_sheet_id,
        source_row_id=source_row_id,
        source_column_id=source_column_id,
    )
    # The absence check and insert are one write transaction.  Without the
    # up-front write lock, two first callers can both observe no candidate and
    # then serialize two valid INSERTs because timeline equivalence is broader
    # than any existing SQL uniqueness constraint.  Callers that already own a
    # transaction retain ownership: this helper never commits or rolls it back.
    owns_transaction = not project.db.in_transaction
    try:
        if owns_transaction:
            project.db.execute("BEGIN IMMEDIATE")

        blob = project.db.execute(
            "SELECT hash, filename, mime FROM blobs WHERE hash=?", (blob_hash,)
        ).fetchone()
        if blob is None:
            raise TimelineError(
                "timeline_not_found", f"blob {blob_hash!r} is not stored"
            )
        resolved_media_type = str(
            media_type or blob["mime"] or "application/octet-stream"
        )
        resolved_filename = filename or blob["filename"]
        anchor = _select_media_timeline(
            project,
            blob_hash=blob_hash,
            duration_ms=duration,
            source_scope=source_scope,
            stable_id=stable_id,
        )

        if anchor is None:
            from frisket.engine.store.evidence import record_source_artifact

            created = record_source_artifact(
                project,
                artifact_kind="av",
                media_type=resolved_media_type,
                stable_id=stable_id,
                blob_hash=blob_hash,
                filename=resolved_filename,
                duration_ms=duration,
                source_sheet_id=source_scope["source_sheet_id"],
                source_row_id=source_scope["source_row_id"],
                source_column_id=source_scope["source_column_id"],
                metadata=dict(metadata or {}),
            )
            anchor = resolve_artifact_timeline(project, int(created["id"]))

        if owns_transaction:
            project.db.commit()
        return anchor
    except BaseException:
        if owns_transaction:
            project.db.rollback()
        raise


def ensure_transcript_timeline(
    project: Project,
    *,
    artifact_id: int,
    duration_ms: int,
    segments: Sequence[Any] | None = None,
    ordered_segments_hash_value: str | None = None,
) -> TimelineAnchor:
    """Persist the one non-derivable fact of a standalone transcript clock."""

    ref = _positive_int(artifact_id, "artifact_id")
    duration = _positive_int(duration_ms, "duration_ms")
    if (segments is None) == (ordered_segments_hash_value is None):
        raise TimelineError(
            "invalid_temporal_value",
            "provide exactly one of segments or ordered_segments_hash_value",
        )
    segments_hash = (
        ordered_segments_hash(segments or [])
        if segments is not None
        else _fingerprint(ordered_segments_hash_value, "ordered_segments_hash")
    )
    artifact = _artifact(project, ref)
    if _mapping_rows(project, ref):
        raise TimelineError(
            "timeline_stale",
            "a derived artifact cannot be finalized as a transcript timeline",
        )
    if artifact["blob_hash"] is not None:
        raise TimelineError(
            "timeline_mismatch",
            "a synchronized media transcript must use its media timeline",
        )
    if artifact["duration_ms"] is not None and artifact["duration_ms"] != duration:
        raise TimelineError(
            "timeline_stale", "transcript artifact duration is already different"
        )
    metadata = _metadata(artifact)
    stored = _transcript_timeline_metadata(metadata)
    if stored is not None:
        if stored["ordered_segments_hash"] != segments_hash:
            raise TimelineError(
                "timeline_stale",
                "transcript timestamped segments differ from its finalized clock",
            )
        return resolve_artifact_timeline(project, ref)

    metadata["timeline"] = {
        "schema_version": TRANSCRIPT_TIMELINE_SCHEMA_VERSION,
        "kind": "transcript",
        "ordered_segments_hash": segments_hash,
    }
    owns_transaction = not project.db.in_transaction
    try:
        project.db.execute(
            "UPDATE source_artifacts SET duration_ms=?, metadata=? WHERE id=?",
            (duration, _canonical_json(metadata), ref),
        )
        if owns_transaction:
            project.db.commit()
    except BaseException:
        if owns_transaction:
            project.db.rollback()
        raise
    return resolve_artifact_timeline(project, ref)


def resolve_timeline(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    duration_ms: int | None = None,
) -> TimelineLease:
    """Resolve a live audio/video/file cell and ensure its immutable timeline."""
    sheet_id = _positive_int(sheet_id, "sheet_id")
    row_id = _positive_int(row_id, "row_id")
    column_id = _positive_int(column_id, "column_id")
    values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
    source = read_timeline_source(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        value=values.get(row_id),
        current_value_ref=refs.get(row_id),
        duration_ms=duration_ms,
    )
    anchor = ensure_media_timeline(
        project,
        blob_hash=source.blob_hash,
        duration_ms=source.duration_ms,
        media_type=source.media_type,
        filename=source.filename,
        source_sheet_id=sheet_id,
        source_row_id=row_id,
        source_column_id=column_id,
    )
    return TimelineLease(
        anchor=anchor,
        blob_hash=source.blob_hash,
        media_type=source.media_type,
        media_kind=source.media_kind,
        filename=source.filename,
        source_cell_ref=source.source_cell_ref,
        current_value_ref=source.current_value_ref,
        source_value_hash=source.source_value_hash,
    )


def read_timeline_source(
    project: Project,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    value: Any,
    current_value_ref: Any,
    duration_ms: int | None = None,
) -> TimelineSource:
    """Admit captured cell facts without rereading values or creating artifacts."""

    from frisket.engine.store.media_blobs import MediaBlobStore

    sheet_ref = _positive_int(sheet_id, "sheet_id")
    row_ref = _positive_int(row_id, "row_id")
    column_ref = _positive_int(column_id, "column_id")
    location = project.db.execute(
        "SELECT c.type AS column_type "
        "FROM columns c JOIN sheets s ON s.id=c.sheet_id "
        "JOIN rows r ON r.sheet_id=s.id "
        "WHERE s.id=? AND r.id=? AND c.id=? "
        "AND s.hidden=0 AND r.hidden=0 AND c.hidden=0",
        (sheet_ref, row_ref, column_ref),
    ).fetchone()
    if location is None:
        raise TimelineError("timeline_not_found", "source cell is not visible")
    if location["column_type"] not in {"audio", "video", "file"}:
        raise TimelineError(
            "timeline_not_found", "source column is not audio, video, or file"
        )
    if not isinstance(value, dict):
        raise TimelineError("timeline_not_found", "source cell is not blob-backed")
    blob_hash = value.get("blob")
    if not isinstance(blob_hash, str) or not blob_hash:
        raise TimelineError("timeline_not_found", "source cell has no blob hash")
    blob = project.db.execute(
        "SELECT hash, filename, mime, metadata FROM blobs WHERE hash=?", (blob_hash,)
    ).fetchone()
    if blob is None:
        raise TimelineError("timeline_not_found", "source blob is missing")
    blob_metadata = MediaBlobStore(project).display_metadata(blob_hash)
    media_type = str(value.get("mime") or blob["mime"] or "application/octet-stream")
    media_kind = _resolve_media_kind(
        column_type=str(location["column_type"]),
        media_types=(value.get("mime"), blob["mime"]),
        cell=value,
        blob_metadata=blob_metadata,
    )
    resolved_duration = _resolve_media_duration_ms(
        explicit=duration_ms,
        cell=value,
        blob_metadata=blob_metadata,
    )
    if resolved_duration is None:
        existing = project.db.execute(
            "SELECT DISTINCT duration_ms FROM source_artifacts "
            "WHERE blob_hash=? AND source_sheet_id=? AND source_row_id=? "
            "AND source_column_id=? AND duration_ms IS NOT NULL",
            (blob_hash, sheet_ref, row_ref, column_ref),
        ).fetchall()
        known = {int(item["duration_ms"]) for item in existing}
        if len(known) == 1:
            resolved_duration = next(iter(known))
        elif len(known) > 1:
            raise TimelineError(
                "timeline_stale", "source cell has conflicting timeline durations"
            )
    if resolved_duration is None:
        raise TimelineError(
            "timeline_duration_required", "source duration must be probed first"
        )

    if (
        not isinstance(current_value_ref, dict)
        or current_value_ref.get("kind") == "missing"
    ):
        raise TimelineError("stale_input", "source cell has no current value ref")
    anchor = _select_media_timeline(
        project,
        blob_hash=blob_hash,
        duration_ms=resolved_duration,
        source_scope=_source_scope(
            source_sheet_id=sheet_ref,
            source_row_id=row_ref,
            source_column_id=column_ref,
        ),
    )
    return TimelineSource(
        anchor=anchor,
        duration_ms=resolved_duration,
        blob_hash=blob_hash,
        media_type=media_type,
        media_kind=media_kind,
        filename=value.get("filename") or blob["filename"],
        source_cell_ref={
            "sheet_id": sheet_ref,
            "row_id": row_ref,
            "column_id": column_ref,
        },
        current_value_ref=dict(current_value_ref),
        source_value_hash=canonical_json_hash(value),
    )


def write_rate1_timeline_segment(
    project: Project,
    *,
    derived_artifact_id: int,
    source_artifact_id: int,
    source_start_ms: int,
    source_end_ms: int,
    precision: Literal["frame_accurate", "sample_accurate", "exact"],
    producer_run_id: int | None = None,
    receipt_id: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> ArtifactTimelineSegment:
    """Write the one complete, exact rate-1 mapping supported by v1."""

    derived_id = _positive_int(derived_artifact_id, "derived_artifact_id")
    source_id = _positive_int(source_artifact_id, "source_artifact_id")
    start = _nonnegative_int(source_start_ms, "source_start_ms")
    end = _positive_int(source_end_ms, "source_end_ms")
    if end <= start:
        raise TimelineError("invalid_range", "source mapping range is empty")
    if precision not in _PRECISIONS:
        raise TimelineError("invalid_temporal_value", "unsupported mapping precision")
    if derived_id == source_id:
        raise TimelineError(
            "ambiguous_time_mapping", "timeline mapping cannot self-link"
        )
    params_json = _canonical_json(dict(params or {}))
    producer = _optional_positive_int(producer_run_id, "producer_run_id")
    if receipt_id is not None and (
        not isinstance(receipt_id, str) or not receipt_id.strip()
    ):
        raise TimelineError("invalid_temporal_value", "receipt_id must be nonempty")

    owns_transaction = not project.db.in_transaction
    try:
        if owns_transaction:
            project.db.execute("BEGIN IMMEDIATE")
        derived = _artifact(project, derived_id)
        source = _artifact(project, source_id)
        derived_duration = _artifact_duration(derived)
        source_duration = _artifact_duration(source)
        elapsed = end - start
        if elapsed != derived_duration:
            raise TimelineError(
                "ambiguous_time_mapping",
                "rate-1 mapping elapsed length must equal derived duration exactly",
            )
        if end > source_duration:
            raise TimelineError(
                "range_out_of_bounds", "source mapping exceeds source duration"
            )
        existing = _mapping_rows(project, derived_id)
        if existing:
            raise TimelineError(
                "ambiguous_time_mapping",
                "derived artifact already has a timeline mapping",
            )
        if _transcript_timeline_metadata(_metadata(derived)) is not None:
            raise TimelineError(
                "timeline_stale",
                "a finalized transcript cannot become a derived timeline",
            )

        resolve_artifact_timeline(project, source_id)
        ancestors = {source_id}
        for segment in timeline_path_to_root(project, source_id):
            ancestors.add(segment.source_artifact_id)
        if derived_id in ancestors:
            raise TimelineError("ambiguous_time_mapping", "timeline cycle detected")

        cursor = project.db.execute(
            "INSERT INTO artifact_timeline_segments ("
            "derived_artifact_id, ordinal, derived_start_ms, derived_end_ms, "
            "source_artifact_id, source_start_ms, source_end_ms, rate_num, "
            "rate_den, precision, producer_run_id, receipt_id, params_json"
            ") VALUES (?, 0, 0, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)",
            (
                derived_id,
                elapsed,
                source_id,
                start,
                end,
                precision,
                producer,
                receipt_id,
                params_json,
            ),
        )
        segment = _segment(
            project.db.execute(
                "SELECT * FROM artifact_timeline_segments WHERE id=?",
                (int(cursor.lastrowid),),
            ).fetchone()
        )
        _validate_v1_segment(project, segment)
        resolve_artifact_timeline(project, derived_id)
        if owns_transaction:
            project.db.commit()
        return segment
    except BaseException:
        if owns_transaction:
            project.db.rollback()
        raise


def timeline_path_to_root(
    project: Project,
    artifact_id: int,
    *,
    max_depth: int = MAX_TIMELINE_TRAVERSAL_DEPTH,
) -> tuple[ArtifactTimelineSegment, ...]:
    """Return the unique immediate-source path, failing closed on corruption."""

    current = _positive_int(artifact_id, "artifact_id")
    depth_limit = _positive_int(max_depth, "max_depth")
    _artifact(project, current)
    visited = {current}
    path: list[ArtifactTimelineSegment] = []
    for _ in range(depth_limit):
        rows = _mapping_rows(project, current)
        if not rows:
            return tuple(path)
        if len(rows) != 1:
            raise TimelineError(
                "ambiguous_time_mapping",
                f"artifact {current} has {len(rows)} timeline segments; v1 requires one",
            )
        segment = _segment(rows[0])
        _validate_v1_segment(project, segment)
        if segment.source_artifact_id in visited:
            raise TimelineError("ambiguous_time_mapping", "timeline cycle detected")
        path.append(segment)
        current = segment.source_artifact_id
        visited.add(current)
    if _mapping_rows(project, current):
        raise TimelineError(
            "ambiguous_time_mapping",
            f"timeline traversal exceeds {depth_limit} mappings",
        )
    return tuple(path)


def map_range_to_root(
    project: Project,
    *,
    artifact_id: int,
    start_ms: int,
    end_ms: int,
    max_depth: int = MAX_TIMELINE_TRAVERSAL_DEPTH,
) -> ResolvedTimelineRange:
    """Compose an exact local half-open range through the unique rate-1 chain."""

    source_id = _positive_int(artifact_id, "artifact_id")
    start = _nonnegative_int(start_ms, "start_ms")
    end = _positive_int(end_ms, "end_ms")
    if end <= start:
        raise TimelineError("invalid_range", "timeline range is empty")
    source = _artifact(project, source_id)
    if end > _artifact_duration(source):
        raise TimelineError("range_out_of_bounds", "timeline range exceeds duration")
    path = timeline_path_to_root(project, source_id, max_depth=max_depth)
    mapped_start = start
    mapped_end = end
    current = source_id
    for segment in path:
        if segment.derived_artifact_id != current:
            raise TimelineError(
                "ambiguous_time_mapping", "timeline path is not contiguous"
            )
        if (
            mapped_start < segment.derived_start_ms
            or mapped_end > segment.derived_end_ms
        ):
            raise TimelineError(
                "selection_unmappable", "range is not completely covered by mapping"
            )
        mapped_start = segment.source_start_ms + mapped_start - segment.derived_start_ms
        mapped_end = segment.source_start_ms + mapped_end - segment.derived_start_ms
        current = segment.source_artifact_id
    root = _artifact(project, current)
    root_timeline = _compute_timeline(project, current)
    if mapped_end > _artifact_duration(root):
        raise TimelineError(
            "selection_unmappable", "mapped range exceeds root timeline duration"
        )
    return ResolvedTimelineRange(
        artifact_id=source_id,
        start_ms=start,
        end_ms=end,
        root_artifact_id=current,
        root_artifact_stable_id=str(root["stable_id"]),
        root_artifact_fingerprint=root_timeline.fingerprint,
        root_start_ms=mapped_start,
        root_end_ms=mapped_end,
        path=path,
    )


def _compute_timeline(
    project: Project,
    artifact_id: int,
    *,
    stack: tuple[int, ...] = (),
    depth: int = 0,
) -> TimelineAnchor:
    if artifact_id in stack:
        raise TimelineError("ambiguous_time_mapping", "timeline cycle detected")
    if depth >= MAX_TIMELINE_TRAVERSAL_DEPTH:
        raise TimelineError(
            "ambiguous_time_mapping", "timeline fingerprint traversal is too deep"
        )
    artifact = _artifact(project, artifact_id)
    stable_id = str(artifact["stable_id"])
    _validate_artifact_stable_id(stable_id)
    duration = _artifact_duration(artifact)
    rows = _mapping_rows(project, artifact_id)
    next_stack = (*stack, artifact_id)

    if rows:
        if len(rows) != 1:
            raise TimelineError(
                "ambiguous_time_mapping",
                f"artifact {artifact_id} has {len(rows)} timeline segments; "
                "v1 requires one",
            )
        segment = _segment(rows[0])
        _validate_v1_segment(project, segment)
        source_timeline = _compute_timeline(
            project,
            segment.source_artifact_id,
            stack=next_stack,
            depth=depth + 1,
        )
        fingerprint = _derived_timeline_fingerprint(
            artifact=artifact,
            segment=segment,
            source_fingerprint=source_timeline.fingerprint,
        )
        return TimelineAnchor(
            artifact_id=artifact_id,
            artifact_stable_id=stable_id,
            fingerprint=fingerprint,
            duration_ms=duration,
            kind="derived",
            clock_policy=DERIVED_CLOCK_POLICY,
        )

    transcript = _transcript_timeline_metadata(_metadata(artifact))
    if transcript is not None:
        segments_hash = transcript["ordered_segments_hash"]
        fingerprint = transcript_timeline_fingerprint(
            artifact_stable_id=stable_id,
            duration_ms=duration,
            ordered_segments_hash_value=segments_hash,
        )
        return TimelineAnchor(
            artifact_id=artifact_id,
            artifact_stable_id=stable_id,
            fingerprint=fingerprint,
            duration_ms=duration,
            kind="transcript",
            clock_policy=TRANSCRIPT_CLOCK_POLICY,
        )

    blob_hash = artifact["blob_hash"]
    if not isinstance(blob_hash, str) or not blob_hash:
        raise TimelineError(
            "timeline_not_found",
            "artifact has neither media bytes nor a transcript clock",
        )
    if (
        project.db.execute("SELECT 1 FROM blobs WHERE hash=?", (blob_hash,)).fetchone()
        is None
    ):
        raise TimelineError("timeline_not_found", "artifact timeline blob is missing")
    fingerprint = media_timeline_fingerprint(blob_hash=blob_hash, duration_ms=duration)
    return TimelineAnchor(
        artifact_id=artifact_id,
        artifact_stable_id=stable_id,
        fingerprint=fingerprint,
        duration_ms=duration,
        kind="media",
        clock_policy=MEDIA_CLOCK_POLICY,
    )


def _derived_timeline_fingerprint(
    *,
    artifact: Mapping[str, Any],
    segment: ArtifactTimelineSegment,
    source_fingerprint: str,
) -> str:
    payload: dict[str, Any] = {
        "schema_version": TIMELINE_FINGERPRINT_SCHEMA_VERSION,
        "kind": "derived",
        "duration_ms": _artifact_duration(artifact),
        "source_timeline_fingerprint": _fingerprint(
            source_fingerprint, "source_timeline_fingerprint"
        ),
        "source_start_ms": segment.source_start_ms,
        "source_end_ms": segment.source_end_ms,
        "clock_policy": DERIVED_CLOCK_POLICY,
    }
    blob_hash = artifact.get("blob_hash")
    if blob_hash is not None:
        if not isinstance(blob_hash, str) or not blob_hash:
            raise TimelineError(
                "timeline_not_found", "derived timeline blob hash must be nonempty"
            )
        payload["blob_hash"] = blob_hash
    return canonical_json_hash(payload)


def _validate_v1_segment(project: Project, segment: ArtifactTimelineSegment) -> None:
    if segment.derived_artifact_id == segment.source_artifact_id:
        raise TimelineError("ambiguous_time_mapping", "timeline mapping self-links")
    if segment.ordinal != 0:
        raise TimelineError(
            "ambiguous_time_mapping", "v1 timeline mapping ordinal must be zero"
        )
    if segment.derived_start_ms != 0:
        raise TimelineError(
            "ambiguous_time_mapping", "v1 derived timeline must start at zero"
        )
    if segment.rate_num != 1 or segment.rate_den != 1:
        raise TimelineError(
            "ambiguous_time_mapping", "v1 timeline mapping must have rate 1/1"
        )
    derived_length = segment.derived_end_ms - segment.derived_start_ms
    source_length = segment.source_end_ms - segment.source_start_ms
    if derived_length != source_length:
        raise TimelineError(
            "ambiguous_time_mapping",
            "rate-1 mapping elapsed lengths differ as stored integers",
        )
    derived = _artifact(project, segment.derived_artifact_id)
    source = _artifact(project, segment.source_artifact_id)
    if segment.derived_end_ms != _artifact_duration(derived):
        raise TimelineError(
            "selection_unmappable", "v1 mapping does not cover the derived timeline"
        )
    if segment.source_start_ms < 0 or segment.source_end_ms > _artifact_duration(
        source
    ):
        raise TimelineError(
            "range_out_of_bounds", "timeline mapping exceeds source duration"
        )
    if segment.precision not in _PRECISIONS:
        raise TimelineError("ambiguous_time_mapping", "mapping precision is invalid")


def _mapping_rows(project: Project, derived_artifact_id: int) -> list[Any]:
    return project.db.execute(
        "SELECT * FROM artifact_timeline_segments "
        "WHERE derived_artifact_id=? ORDER BY ordinal, id",
        (derived_artifact_id,),
    ).fetchall()


def _segment(row: Any) -> ArtifactTimelineSegment:
    if row is None:
        raise TimelineError("timeline_not_found", "timeline segment row is missing")
    data = dict(row)
    return ArtifactTimelineSegment(
        id=int(data["id"]),
        derived_artifact_id=int(data["derived_artifact_id"]),
        ordinal=int(data["ordinal"]),
        derived_start_ms=int(data["derived_start_ms"]),
        derived_end_ms=int(data["derived_end_ms"]),
        source_artifact_id=int(data["source_artifact_id"]),
        source_start_ms=int(data["source_start_ms"]),
        source_end_ms=int(data["source_end_ms"]),
        rate_num=int(data["rate_num"]),
        rate_den=int(data["rate_den"]),
        precision=str(data["precision"]),
        producer_run_id=(
            int(data["producer_run_id"])
            if data.get("producer_run_id") is not None
            else None
        ),
        receipt_id=(
            str(data["receipt_id"]) if data.get("receipt_id") is not None else None
        ),
        params=_json_object(data.get("params_json")),
    )


def _artifact(project: Project, artifact_id: int) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT * FROM source_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    if row is None:
        raise TimelineError(
            "timeline_not_found", f"source artifact {artifact_id} does not exist"
        )
    return dict(row)


def _artifact_duration(artifact: Mapping[str, Any]) -> int:
    value = artifact.get("duration_ms")
    if value is None:
        raise TimelineError(
            "timeline_duration_required", "artifact timeline duration is unknown"
        )
    return _positive_int(value, "duration_ms")


def _metadata(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return _json_object(artifact.get("metadata"))


def _transcript_timeline_metadata(
    metadata: Mapping[str, Any],
) -> dict[str, str] | None:
    value = metadata.get("timeline")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TimelineError(
            "timeline_stale", "transcript timeline metadata is malformed"
        )
    if value.get("kind") != "transcript":
        return None
    if value.get("schema_version") != TRANSCRIPT_TIMELINE_SCHEMA_VERSION:
        raise TimelineError(
            "timeline_stale", "transcript timeline schema version is unsupported"
        )
    return {
        "ordered_segments_hash": _fingerprint(
            value.get("ordered_segments_hash"), "ordered_segments_hash"
        )
    }


def _media_artifact_matches(
    artifact: Mapping[str, Any],
    *,
    blob_hash: str,
    duration_ms: int,
    source_scope: Mapping[str, int | None],
) -> bool:
    data = dict(artifact)
    return (
        data.get("artifact_kind") == "av"
        and data.get("blob_hash") == blob_hash
        and data.get("duration_ms") == duration_ms
        and data.get("source_sheet_id") == source_scope["source_sheet_id"]
        and data.get("source_row_id") == source_scope["source_row_id"]
        and data.get("source_column_id") == source_scope["source_column_id"]
    )


def _source_scope(
    *,
    source_sheet_id: int | None,
    source_row_id: int | None,
    source_column_id: int | None,
) -> dict[str, int | None]:
    values = (source_sheet_id, source_row_id, source_column_id)
    if all(value is None for value in values):
        return {
            "source_sheet_id": None,
            "source_row_id": None,
            "source_column_id": None,
        }
    if any(value is None for value in values):
        raise TimelineError(
            "invalid_temporal_value",
            "source cell scope must include sheet, row and column",
        )
    return {
        "source_sheet_id": _positive_int(source_sheet_id, "source_sheet_id"),
        "source_row_id": _positive_int(source_row_id, "source_row_id"),
        "source_column_id": _positive_int(source_column_id, "source_column_id"),
    }


def _resolve_media_duration_ms(
    *,
    explicit: int | None,
    cell: Mapping[str, Any],
    blob_metadata: Mapping[str, Any],
) -> int | None:
    if explicit is not None:
        return _positive_int(explicit, "duration_ms")
    candidates: list[int] = []
    if cell.get("duration_ms") is not None:
        candidates.append(_positive_int(cell["duration_ms"], "duration_ms"))
    for value in (blob_metadata.get("duration_seconds"),):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TimelineError(
                "timeline_stale", "media duration metadata must be numeric seconds"
            )
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise TimelineError("timeline_stale", "media duration metadata is invalid")
        rounded = round(number * 1000)
        if rounded <= 0:
            raise TimelineError("timeline_stale", "media duration rounds to zero")
        candidates.append(rounded)
    if not candidates:
        return None
    if len(set(candidates)) != 1:
        raise TimelineError("timeline_stale", "media duration metadata conflicts")
    return candidates[0]


def _resolve_media_kind(
    *,
    column_type: str,
    media_types: Sequence[Any],
    cell: Mapping[str, Any],
    blob_metadata: Mapping[str, Any],
) -> Literal["audio", "video"]:
    """Resolve an actionable media kind without treating duration as proof.

    A semantic audio/video column is authoritative.  Generic file columns need
    an audio/video MIME type or consistent host metadata; opaque files remain
    non-temporal even when a caller supplies or discovers a duration.
    """

    if column_type == "audio":
        return "audio"
    if column_type == "video":
        return "video"

    candidates: set[Literal["audio", "video"]] = set()
    for raw_media_type in media_types:
        if not isinstance(raw_media_type, str):
            continue
        normalized_media_type = raw_media_type.partition(";")[0].strip().lower()
        if normalized_media_type.startswith("audio/"):
            candidates.add("audio")
        elif normalized_media_type.startswith("video/"):
            candidates.add("video")
    for raw_kind in (blob_metadata.get("kind"),):
        if not isinstance(raw_kind, str):
            continue
        normalized_kind = raw_kind.strip().lower()
        if normalized_kind == "audio":
            candidates.add("audio")
        elif normalized_kind == "video":
            candidates.add("video")
    if len(candidates) > 1:
        raise TimelineError(
            "timeline_stale",
            "This file has conflicting audio and video type information. "
            "Re-import it or correct its file type before trying again.",
        )
    if candidates:
        return next(iter(candidates))
    raise TimelineError(
        "timeline_not_found",
        "This file is not recognized as audio or video. Choose a media file or "
        "correct its file type before trying again.",
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TimelineError(
            "timeline_stale", "stored timeline JSON is malformed"
        ) from exc
    if not isinstance(parsed, dict):
        raise TimelineError("timeline_stale", "stored timeline JSON must be an object")
    return parsed


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TimelineError(
            "invalid_temporal_value", "timeline metadata is not JSON-safe"
        ) from exc


def _validate_artifact_stable_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("source_artifact:"):
        raise TimelineError("timeline_stale", "artifact stable ID is malformed")
    try:
        uuid.UUID(value.removeprefix("source_artifact:"))
    except (ValueError, AttributeError) as exc:
        raise TimelineError(
            "timeline_stale", "artifact stable ID is malformed"
        ) from exc
    return value


def _fingerprint(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise TimelineError("timeline_stale", f"{field} is not a canonical SHA-256")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TimelineError(
            "invalid_temporal_value", f"{field} must be a nonnegative integer"
        )
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TimelineError(
            "invalid_temporal_value", f"{field} must be a positive integer"
        )
    return value


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


__all__ = [
    "ArtifactTimelineSegment",
    "DERIVED_CLOCK_POLICY",
    "MAX_TIMELINE_TRAVERSAL_DEPTH",
    "MEDIA_CLOCK_POLICY",
    "ResolvedTimelineRange",
    "TIMELINE_FINGERPRINT_SCHEMA_VERSION",
    "TRANSCRIPT_CLOCK_POLICY",
    "TRANSCRIPT_TIMELINE_SCHEMA_VERSION",
    "TimelineAnchor",
    "TimelineError",
    "TimelineLease",
    "canonical_json_hash",
    "ensure_artifact_timeline",
    "ensure_media_timeline",
    "ensure_transcript_timeline",
    "map_range_to_root",
    "media_timeline_fingerprint",
    "ordered_segments_hash",
    "resolve_artifact_timeline",
    "resolve_timeline",
    "timeline_path_to_root",
    "transcript_timeline_fingerprint",
    "validate_timeline_anchor",
    "write_rate1_timeline_segment",
]
