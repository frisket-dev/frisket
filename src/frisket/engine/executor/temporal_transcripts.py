"""Resolve and persist first-class timestamped transcript evidence.

``timestamped_transcript`` is a semantic column type whose stored value is
still just display text.  Timing lives in the existing evidence substrate and
is bound to the exact current value ref.  This module intentionally has no
compatibility reader: only links written with the V1 semantic marker qualify.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping

from frisket.engine.store.artifact_timeline import (
    EphemeralMediaClock,
    TimelineAnchor,
    TimelineError,
    canonical_json_hash,
    map_range_to_root,
    resolve_artifact_timeline,
    root_clock_extent,
)
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    record_evidence_link,
    record_source_span,
)
from frisket.engine.store.transcript_projection import (
    TranscriptProjection,
    project_transcript_segments,
)

if TYPE_CHECKING:
    from frisket.engine.executor.temporal_materialization import ResolvedTemporalSource


@dataclass(frozen=True)
class ResolvedTranscriptSource:
    artifact_id: int
    artifact_stable_id: str
    artifact_duration_ms: int
    evidence_link_id: int
    evidence_link_stable_id: str
    transcript_run_id: int
    sheet_id: int
    row_id: int
    transcript_column_id: int
    transcript_column_name: str
    transcript_column_position: int
    transcript_value: str
    transcript_value_ref: dict[str, Any]
    language: str | None
    spans: tuple[dict[str, Any], ...]
    snapshot_hash: str
    # transcript-local = selected-source-local + this offset.  This is zero
    # for a transcript of the selected artifact and nonzero for an exact,
    # root-compatible sibling clip on the same row.
    source_to_transcript_offset_ms: int = 0


def _loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _candidate_payload(
    project: Any,
    *,
    link_id: int,
    artifact_id: int,
    sheet_id: int,
    row_id: int,
) -> dict[str, Any] | None:
    link = project.db.execute(
        "SELECT * FROM evidence_links WHERE id=? AND status='active' "
        "AND sheet_id=? AND row_id=?",
        (link_id, sheet_id, row_id),
    ).fetchone()
    if (
        link is None
        or link["subject_kind"] != "cell"
        or link["link_role"]
        not in {
            "media_transcribe_temporal",
            "temporal_transcript_projection",
        }
    ):
        return None
    transcript_run_id = link["run_id"]
    if (
        isinstance(transcript_run_id, bool)
        or not isinstance(transcript_run_id, int)
        or transcript_run_id <= 0
    ):
        # The semantic marker is not sufficient provenance on its own.  A
        # first-class transcript must retain the concrete producing run even
        # when its current cell ref is a projection/manual-edit ref.
        return None
    link_metadata = _loads_object(link["metadata"])
    if (
        link_metadata.get("schema_version")
        != TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION
        or link_metadata.get("semantic_type") != "timestamped_transcript"
    ):
        return None
    raw_language = link_metadata.get("language")
    language = (
        raw_language.strip()
        if isinstance(raw_language, str) and raw_language.strip()
        else None
    )
    column_id = link["column_id"]
    if not isinstance(column_id, int):
        return None
    location = project.db.execute(
        "SELECT c.type, c.name, c.position FROM columns c "
        "JOIN sheets s ON s.id=c.sheet_id "
        "JOIN rows r ON r.sheet_id=s.id "
        "WHERE s.id=? AND r.id=? AND c.id=? "
        "AND s.hidden=0 AND r.hidden=0 AND c.hidden=0",
        (sheet_id, row_id, int(column_id)),
    ).fetchone()
    if location is None or str(location["type"]) != "timestamped_transcript":
        return None
    values, refs = project.get_values_with_refs(
        sheet_id, int(column_id), row_ids=[row_id]
    )
    value_ref = refs.get(row_id)
    transcript_value = values.get(row_id)
    if not isinstance(transcript_value, str) or not transcript_value.strip():
        return None
    if not isinstance(value_ref, dict):
        return None
    value_ref_kind = value_ref.get("kind")
    if value_ref_kind not in {"run_result", "source_cell", "manual_edit"}:
        return None
    if link["link_role"] == "media_transcribe_temporal":
        if value_ref_kind != "run_result" or link["run_id"] != value_ref.get("run_id"):
            return None
    elif value_ref_kind == "manual_edit":
        # Extract Range publishes generated values through the operation/edit
        # layer so undo remains atomic. Only the exact producer op qualifies;
        # a later user edit has a different ref and invalidates the link.
        if link["op_id"] != value_ref.get("op_id"):
            return None
    elif value_ref_kind != "source_cell":
        return None
    subject_ref = _loads_object(link["subject_ref_json"])
    if subject_ref != value_ref:
        return None

    try:
        timeline = resolve_artifact_timeline(project, artifact_id)
    except (KeyError, TimelineError, TypeError, ValueError):
        return None

    rows = project.db.execute(
        "SELECT sp.*, els.rank FROM evidence_link_spans els "
        "JOIN source_spans sp ON sp.id=els.span_id "
        "WHERE els.link_id=? ORDER BY els.rank, sp.id",
        (link_id,),
    ).fetchall()
    if not rows or any(int(row["artifact_id"]) != artifact_id for row in rows):
        return None
    spans: list[dict[str, Any]] = []
    stable_ids: set[str] = set()
    prior_start_ms = -1
    for expected_rank, row in enumerate(rows):
        if row["span_kind"] != "temporal" or int(row["rank"]) != expected_rank:
            return None
        stable_id = str(row["stable_id"])
        start_ms = row["start_ms"]
        end_ms = row["end_ms"]
        quote = row["quote"]
        if (
            not stable_id
            or stable_id in stable_ids
            or not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
            or start_ms < 0
            or end_ms <= start_ms
            or end_ms > timeline.duration_ms
            or start_ms < prior_start_ms
            or not isinstance(quote, str)
            or not quote.strip()
        ):
            return None
        selector = _loads_object(row["selector_json"])
        segment_index = selector.get("segment_index")
        if (
            not isinstance(segment_index, int)
            or isinstance(segment_index, bool)
            or segment_index != expected_rank
        ):
            return None
        raw_words = selector.get("words")
        if raw_words is not None:
            if not isinstance(raw_words, list):
                return None
            for raw_word in raw_words:
                if not isinstance(raw_word, Mapping):
                    return None
                word_text = raw_word.get("word") or raw_word.get("text")
                word_start_ms = raw_word.get("start_ms")
                word_end_ms = raw_word.get("end_ms")
                if (
                    not isinstance(word_text, str)
                    or not word_text.strip()
                    or not isinstance(word_start_ms, int)
                    or isinstance(word_start_ms, bool)
                    or not isinstance(word_end_ms, int)
                    or isinstance(word_end_ms, bool)
                    or word_start_ms < start_ms
                    or word_end_ms <= word_start_ms
                    or word_end_ms > end_ms
                ):
                    return None
        stable_ids.add(stable_id)
        prior_start_ms = start_ms
        spans.append(
            {
                "id": int(row["id"]),
                "stable_id": stable_id,
                "artifact_id": int(row["artifact_id"]),
                "span_kind": "temporal",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "quote": quote,
                "selector": selector,
                "metadata": _loads_object(row["metadata"]),
                "rank": expected_rank,
            }
        )
    payload = {
        "artifact_id": artifact_id,
        "evidence_link_id": int(link["id"]),
        "evidence_link_stable_id": str(link["stable_id"]),
        "transcript_run_id": transcript_run_id,
        "sheet_id": sheet_id,
        "row_id": row_id,
        "transcript_column_id": int(column_id),
        "transcript_column_name": str(location["name"]),
        "transcript_column_position": int(location["position"]),
        "transcript_value": transcript_value,
        "transcript_value_ref": value_ref,
        "language": language,
        "timeline": timeline.wire_value(),
        "spans": spans,
    }
    payload["snapshot_hash"] = canonical_json_hash(payload)
    return payload


def resolve_timestamped_transcript(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int,
    artifact_id: int | None = None,
) -> ResolvedTranscriptSource | None:
    """Resolve one explicitly addressed semantic transcript cell.

    Column type alone is never authority.  Imported/retyped strings, legacy
    transcript evidence, and manual edits all fail closed because they lack a
    current, V1-marked evidence link.
    """

    params: list[Any] = [sheet_id, row_id, column_id]
    artifact_filter = ""
    if artifact_id is not None:
        artifact_filter = " AND sp.artifact_id=?"
        params.append(int(artifact_id))
    rows = project.db.execute(
        "SELECT DISTINCT el.id, sp.artifact_id FROM evidence_links el "
        "JOIN evidence_link_spans els ON els.link_id=el.id "
        "JOIN source_spans sp ON sp.id=els.span_id "
        "WHERE el.status='active' AND el.sheet_id=? AND el.row_id=? "
        "AND el.column_id=? "
        "AND el.link_role IN "
        "('media_transcribe_temporal','temporal_transcript_projection')"
        + artifact_filter
        + " ORDER BY el.id",
        params,
    ).fetchall()
    candidates = [
        payload
        for row in rows
        if (
            payload := _candidate_payload(
                project,
                link_id=int(row["id"]),
                artifact_id=int(row["artifact_id"]),
                sheet_id=sheet_id,
                row_id=row_id,
            )
        )
        is not None
    ]
    if len(candidates) != 1:
        return None
    payload = candidates[0]
    return ResolvedTranscriptSource(
        artifact_id=int(payload["artifact_id"]),
        artifact_stable_id=str(payload["timeline"]["artifact_stable_id"]),
        artifact_duration_ms=int(payload["timeline"]["duration_ms"]),
        evidence_link_id=payload["evidence_link_id"],
        evidence_link_stable_id=payload["evidence_link_stable_id"],
        transcript_run_id=payload["transcript_run_id"],
        sheet_id=payload["sheet_id"],
        row_id=payload["row_id"],
        transcript_column_id=payload["transcript_column_id"],
        transcript_column_name=payload["transcript_column_name"],
        transcript_column_position=payload["transcript_column_position"],
        transcript_value=payload["transcript_value"],
        transcript_value_ref=payload["transcript_value_ref"],
        language=payload["language"],
        spans=tuple(payload["spans"]),
        snapshot_hash=payload["snapshot_hash"],
    )


def bind_transcript_to_source(
    project: Any,
    transcript: ResolvedTranscriptSource,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
) -> ResolvedTranscriptSource | None:
    """Bind one exact transcript to an unambiguous overlapping source clock."""

    transcript_anchor = resolve_artifact_timeline(project, transcript.artifact_id)
    if (
        isinstance(source_anchor, TimelineAnchor)
        and transcript_anchor.artifact_id == source_anchor.artifact_id
    ):
        return replace(transcript, source_to_transcript_offset_ms=0)
    if (
        transcript_anchor.fingerprint == source_anchor.fingerprint
        and transcript_anchor.duration_ms == source_anchor.duration_ms
    ):
        return replace(transcript, source_to_transcript_offset_ms=0)

    transcript_root = map_range_to_root(
        project,
        artifact_id=transcript_anchor.artifact_id,
        start_ms=0,
        end_ms=transcript_anchor.duration_ms,
    )
    source_root = root_clock_extent(project, source_anchor)
    if (
        transcript_root.root_artifact_fingerprint
        != source_root.root_artifact_fingerprint
        or max(transcript_root.root_start_ms, source_root.root_start_ms)
        >= min(transcript_root.root_end_ms, source_root.root_end_ms)
    ):
        return None
    return replace(
        transcript,
        source_to_transcript_offset_ms=(
            source_root.root_start_ms - transcript_root.root_start_ms
        ),
    )


def resolve_compatible_transcripts_for_anchor(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    source_anchor: TimelineAnchor | EphemeralMediaClock,
) -> tuple[tuple[ResolvedTranscriptSource, ...], tuple[str, ...]]:
    """Resolve exact current transcripts compatible with one source anchor.

    The source column's stable sheet position is the only ordering rule.  We do
    not pick a latest run and do not collapse multiple compatible languages or
    engines into an arbitrary winner.  A nonempty typed transcript that cannot
    be inherited produces one ordinary reporter-facing warning.
    """

    columns = project.db.execute(
        "SELECT id, name, position FROM columns "
        "WHERE sheet_id=? AND hidden=0 AND type='timestamped_transcript' "
        "ORDER BY position, id",
        (sheet_id,),
    ).fetchall()
    resolved: list[ResolvedTranscriptSource] = []
    warnings: list[str] = []
    for column in columns:
        column_id = int(column["id"])
        values = project.get_values(
            sheet_id,
            column_id,
            row_ids=[row_id],
        )
        value = values.get(row_id)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        transcript = resolve_timestamped_transcript(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=column_id,
        )
        if transcript is None:
            warnings.append(
                f'Transcript "{str(column["name"])}" was not inherited because '
                "its current timestamp evidence is missing, stale, or ambiguous."
            )
            continue
        try:
            compatible = bind_transcript_to_source(
                project,
                transcript,
                source_anchor,
            )
        except (KeyError, TimelineError, TypeError, ValueError):
            compatible = None
        if compatible is None:
            warnings.append(
                f'Transcript "{transcript.transcript_column_name}" was not inherited '
                "because it is not on the selected media timeline."
            )
            continue
        resolved.append(compatible)
    return (
        tuple(
            sorted(
                resolved,
                key=lambda item: (
                    item.transcript_column_position,
                    item.transcript_column_id,
                ),
            )
        ),
        tuple(dict.fromkeys(warnings)),
    )


def resolve_compatible_transcripts(
    project: Any,
    source: ResolvedTemporalSource,
) -> tuple[tuple[ResolvedTranscriptSource, ...], tuple[str, ...]]:
    """Resolve exact current transcripts compatible with ``source``."""

    return resolve_compatible_transcripts_for_anchor(
        project,
        sheet_id=source.sheet_id,
        row_id=source.row_id,
        source_anchor=source.lease.anchor,
    )


def revalidate_transcript(project: Any, transcript: ResolvedTranscriptSource) -> None:
    current = resolve_timestamped_transcript(
        project,
        sheet_id=transcript.sheet_id,
        row_id=transcript.row_id,
        column_id=transcript.transcript_column_id,
        artifact_id=transcript.artifact_id,
    )
    if (
        current is None
        or current.evidence_link_id != transcript.evidence_link_id
        or current.snapshot_hash != transcript.snapshot_hash
    ):
        raise TimelineError(
            "stale_input", "source transcript changed during temporal execution"
        )


def transcript_snapshot_by_identity(
    project: Any,
    *,
    artifact_stable_id: str,
    evidence_link_stable_id: str,
    sheet_id: int,
    row_id: int,
) -> dict[str, Any] | None:
    """Re-resolve a persisted transcript snapshot for receipt replay checks.

    Receipt replay does not retain the full transcript payload and span list.
    Stable artifact/link identities are enough to rebuild that payload through
    the same current-value-scoped resolver used at execution time.
    """

    artifact = project.db.execute(
        "SELECT id FROM source_artifacts WHERE stable_id=?",
        (artifact_stable_id,),
    ).fetchone()
    link = project.db.execute(
        "SELECT id FROM evidence_links WHERE stable_id=?",
        (evidence_link_stable_id,),
    ).fetchone()
    if artifact is None or link is None:
        return None
    return _candidate_payload(
        project,
        link_id=int(link["id"]),
        artifact_id=int(artifact["id"]),
        sheet_id=sheet_id,
        row_id=row_id,
    )


def project_transcript(
    transcript: ResolvedTranscriptSource,
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> TranscriptProjection:
    if (
        isinstance(source_start_ms, bool)
        or not isinstance(source_start_ms, int)
        or isinstance(source_end_ms, bool)
        or not isinstance(source_end_ms, int)
        or source_start_ms < 0
        or source_end_ms <= source_start_ms
    ):
        raise ValueError("projection range must be positive integer milliseconds")
    transcript_start_ms = source_start_ms + transcript.source_to_transcript_offset_ms
    transcript_end_ms = source_end_ms + transcript.source_to_transcript_offset_ms
    overlap_start_ms = max(0, transcript_start_ms)
    overlap_end_ms = min(transcript.artifact_duration_ms, transcript_end_ms)
    if overlap_end_ms <= overlap_start_ms:
        return TranscriptProjection(
            text="",
            segments=(),
            warnings=(),
            language=transcript.language,
        )
    projection = project_transcript_segments(
        transcript.spans,
        source_start_ms=overlap_start_ms,
        source_end_ms=overlap_end_ms,
        source_artifact_stable_id=transcript.artifact_stable_id,
        transcript_run_id=transcript.transcript_run_id,
        evidence_link_stable_id=transcript.evidence_link_stable_id,
        language=transcript.language,
    )
    # If the selected source begins before a sibling transcript does, preserve
    # that leading silence in the projected clip-local coordinates.
    leading_gap_ms = overlap_start_ms - transcript_start_ms
    if leading_gap_ms <= 0 or not projection.segments:
        return projection
    return replace(
        projection,
        segments=tuple(
            replace(
                segment,
                start_ms=segment.start_ms + leading_gap_ms,
                end_ms=segment.end_ms + leading_gap_ms,
                words=tuple(
                    replace(
                        word,
                        start_ms=word.start_ms + leading_gap_ms,
                        end_ms=word.end_ms + leading_gap_ms,
                    )
                    for word in segment.words
                ),
            )
            for segment in projection.segments
        ),
    )


def persist_projected_transcript_evidence(
    project: Any,
    *,
    transcript: ResolvedTranscriptSource,
    projection: TranscriptProjection,
    derived_artifact_id: int,
    output_sheet_id: int,
    output_row_id: int,
    transcript_column_id: int,
    receipt_id: str,
    op_id: int,
) -> dict[str, Any] | None:
    """Persist clip-local spans and bind them to the projected text cell."""

    if not project.db.in_transaction:
        raise RuntimeError("transcript projection persistence requires a transaction")
    span_refs: list[dict[str, Any]] = []
    for rank, segment in enumerate(projection.segments):
        fields = segment.as_source_span_fields()
        span = record_source_span(
            project,
            artifact_id=derived_artifact_id,
            span_kind=fields["span_kind"],
            start_ms=fields["start_ms"],
            end_ms=fields["end_ms"],
            quote=fields["quote"],
            selector=fields["selector"],
            metadata=fields["metadata"],
        )
        span_refs.append({"span_id": span["id"], "rank": rank})
    if not span_refs:
        return None
    _values, refs = project.get_values_with_refs(
        output_sheet_id, transcript_column_id, row_ids=[output_row_id]
    )
    subject_ref = refs.get(output_row_id)
    if not isinstance(subject_ref, dict):
        raise RuntimeError("projected transcript cell has no current value ref")
    link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=subject_ref,
        spans=span_refs,
        sheet_id=output_sheet_id,
        row_id=output_row_id,
        column_id=transcript_column_id,
        run_id=transcript.transcript_run_id,
        op_id=op_id,
        receipt_id=receipt_id,
        link_role="temporal_transcript_projection",
        producer={
            "action_kind": "temporal_projection",
            "projection_version": "frisket.transcript_projection.v1",
        },
        metadata={
            "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
            "semantic_type": "timestamped_transcript",
            "source_evidence_link_stable_id": transcript.evidence_link_stable_id,
            "source_artifact_stable_id": transcript.artifact_stable_id,
            **({"language": transcript.language} if transcript.language else {}),
        },
    )
    return {
        "kind": "temporal_transcript_projection",
        "evidence_link_id": link["id"],
        "evidence_link_stable_id": link["stable_id"],
        "source_evidence_link_id": transcript.evidence_link_id,
        "source_evidence_link_stable_id": transcript.evidence_link_stable_id,
        "transcript_run_id": transcript.transcript_run_id,
        "span_count": len(span_refs),
    }


__all__ = [
    "ResolvedTranscriptSource",
    "bind_transcript_to_source",
    "persist_projected_transcript_evidence",
    "project_transcript",
    "resolve_compatible_transcripts",
    "resolve_timestamped_transcript",
    "revalidate_transcript",
]
