from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from frisket.engine.store.grounding_contract import (
    AnchorStream,
    AnchorUnit,
    SegmentStream,
    SegmentToken,
    SpanSpec,
)


@dataclass(frozen=True)
class ResolvedSegmentStream:
    """The row's transcript segments as both addressable units (strategy B,
    the default) and a positioned stream (strategy A, the fallback).

    ``artifact_id`` is the ``media.transcribe`` writer's own ``av`` source
    artifact (blob-hash-backed, real audio/video ``media_type``) -- any NEW
    span a caller records against this stream MUST land on this artifact, never
    on a caller-supplied text/row artifact (extract-scalar-temporal-anchors-v1
    regression: a citation grounded on the row-json artifact carries no
    renderable blob, so the evidence viewer falls back to "no first-party
    renderer" despite the span carrying start_ms/end_ms).

    ``span_ids_by_segment_index`` lets a caller PREFER lookup over re-recording
    using the stored-coordinate strategy: each numbered segment
    already has exactly one ``temporal`` span the transcribe writer recorded
    -- reusing its id means the extract citation points at the SAME span the
    transcript's own evidence link uses, instead of a duplicate row."""

    anchors: AnchorStream
    segments: SegmentStream
    artifact_id: int
    span_ids_by_segment_index: dict[int, int]


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def resolve_transcript_segment_stream(
    project: Any, *, sheet_id: int, row_id: int
) -> ResolvedSegmentStream | None:
    """Resolve the most recent ``media.transcribe`` segment stream for this row,
    or ``None`` if no positioned transcript stream exists (-> Degradation Law at
    the caller: keep the coarse text/no-evidence state, never a guessed span)."""

    rows = project.db.execute(
        "SELECT id FROM source_artifacts "
        "WHERE artifact_kind='av' AND source_sheet_id=? AND source_row_id=? "
        "ORDER BY id DESC",
        (int(sheet_id), int(row_id)),
    ).fetchall()
    for row in rows:
        artifact_id = int(row["id"])
        units, tokens, span_ids_by_index = _segments_for_artifact(project, artifact_id)
        if not units:
            continue
        return ResolvedSegmentStream(
            anchors=AnchorStream(units=units),
            segments=SegmentStream(tokens=tokens),
            artifact_id=artifact_id,
            span_ids_by_segment_index=span_ids_by_index,
        )
    return None


def _segments_for_artifact(
    project: Any, artifact_id: int, *, span_ids: list[int] | None = None
) -> tuple[list[AnchorUnit], list[SegmentToken], dict[int, int]]:
    if span_ids == []:
        return [], [], {}
    restriction = (
        " AND id IN (" + ",".join("?" for _ in span_ids) + ")"
        if span_ids is not None
        else ""
    )
    span_rows = project.db.execute(
        "SELECT id, start_ms, end_ms, quote, selector_json FROM source_spans "
        "WHERE artifact_id=? AND span_kind='temporal'" + restriction + " ORDER BY id",
        (artifact_id, *(span_ids or [])),
    ).fetchall()
    units: list[AnchorUnit] = []
    tokens: list[SegmentToken] = []
    span_ids_by_index: dict[int, int] = {}
    for span_row in span_rows:
        selector = _loads(span_row["selector_json"], {})
        index = selector.get("segment_index") if isinstance(selector, dict) else None
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        text = span_row["quote"] or ""
        start_ms = span_row["start_ms"]
        end_ms = span_row["end_ms"]
        # Optional per-turn speaker; absent on non-diarized/old
        # spans -> None (regression pin: old evidence still resolves).
        speaker = selector.get("speaker") if isinstance(selector, dict) else None
        span_metadata: dict[str, Any] = {"segment_index": index}
        if speaker:
            span_metadata["speaker"] = speaker
        units.append(
            AnchorUnit(
                unit_id=index,
                spans=[
                    SpanSpec(
                        span_kind="temporal",
                        start_ms=start_ms,
                        end_ms=end_ms,
                        quote=text,
                        metadata=span_metadata,
                    )
                ],
                text=text,
                speaker=speaker,
            )
        )
        tokens.append(
            SegmentToken(
                text=text,
                start_ms=start_ms,
                end_ms=end_ms,
                index=index,
                speaker=speaker,
            )
        )
        span_ids_by_index[index] = int(span_row["id"])
    return units, tokens, span_ids_by_index
