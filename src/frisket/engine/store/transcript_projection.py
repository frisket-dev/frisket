"""Pure projection of timestamped transcript segments into a derived timeline.

This module deliberately knows nothing about projects, rows, evidence links, or
transactions.  An action resolves and snapshots the exact source transcript,
passes its segment rows here, and later persists the returned values and span
fields in the same transaction as the derived media artifact.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


PARTIAL_SPEECH_MARKER = "[partial speech — open source context]"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class TranscriptWord:
    text: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("transcript word requires text")
        if not _is_int(self.start_ms) or not _is_int(self.end_ms):
            raise ValueError("transcript word times must be integer milliseconds")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("transcript word must have a positive range")


@dataclass(frozen=True)
class TranscriptSourceSegment:
    """One immutable source transcript segment.

    ``metadata`` may contain a prior ``projection`` object.  That is how a
    clip-of-a-clip propagates its root-span identity and root coordinates while
    still naming the immediate parent span on the next projection.
    """

    span_stable_id: str
    start_ms: int
    end_ms: int
    text: str
    segment_index: int | None = None
    speaker: str | None = None
    speaker_confidence: str | None = None
    words: tuple[TranscriptWord, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    partial: bool = False
    text_precision: str = "source_segment"
    text_may_extend_outside_clip: bool = False
    source_quote: str | None = None

    def __post_init__(self) -> None:
        if not self.span_stable_id:
            raise ValueError("transcript source segment requires span_stable_id")
        if not _is_int(self.start_ms) or not _is_int(self.end_ms):
            raise ValueError("transcript segment times must be integer milliseconds")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("transcript source segment must have a positive range")
        if self.segment_index is not None and (
            not _is_int(self.segment_index) or self.segment_index < 0
        ):
            raise ValueError("transcript segment_index must be a nonnegative integer")
        if not self.text_precision:
            raise ValueError("transcript text_precision must be nonempty")


@dataclass(frozen=True)
class ProjectedTranscriptWord:
    text: str
    start_ms: int
    end_ms: int
    source_start_ms: int
    source_end_ms: int
    clipped_left: bool
    clipped_right: bool

    def as_transcript_word(self) -> dict[str, Any]:
        return {
            "word": self.text,
            "start": round(self.start_ms / 1000, 3),
            "end": round(self.end_ms / 1000, 3),
        }

    def as_span_word(self) -> dict[str, Any]:
        return {
            "word": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "source_start_ms": self.source_start_ms,
            "source_end_ms": self.source_end_ms,
            "clipped_left": self.clipped_left,
            "clipped_right": self.clipped_right,
        }


@dataclass(frozen=True)
class ProjectedTranscriptSegment:
    segment_index: int
    start_ms: int
    end_ms: int
    text: str
    source_start_ms: int
    source_end_ms: int
    source_segment_start_ms: int
    source_segment_end_ms: int
    source_segment_index: int | None
    parent_span_stable_id: str
    root_span_stable_id: str
    root_start_ms: int
    root_end_ms: int
    clipping: str
    partial: bool
    text_precision: str
    text_may_extend_outside_clip: bool
    speaker: str | None
    speaker_confidence: str | None
    speaker_namespace: str | None
    words: tuple[ProjectedTranscriptWord, ...]
    source_artifact_stable_id: str
    transcript_run_id: int | None
    evidence_link_stable_id: str | None
    source_quote: str | None

    def lineage(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "source_artifact_stable_id": self.source_artifact_stable_id,
            "parent_span_stable_id": self.parent_span_stable_id,
            "root_span_stable_id": self.root_span_stable_id,
            "parent_start_ms": self.source_start_ms,
            "parent_end_ms": self.source_end_ms,
            "root_start_ms": self.root_start_ms,
            "root_end_ms": self.root_end_ms,
            "source_segment_start_ms": self.source_segment_start_ms,
            "source_segment_end_ms": self.source_segment_end_ms,
            "source_segment_index": self.source_segment_index,
            "clipping": self.clipping,
        }
        if self.transcript_run_id is not None:
            value["transcript_run_id"] = self.transcript_run_id
        if self.evidence_link_stable_id:
            value["evidence_link_stable_id"] = self.evidence_link_stable_id
        if self.source_quote is not None:
            value["source_quote"] = self.source_quote
        return value

    def as_transcript_segment(self) -> dict[str, Any]:
        """Return the existing transcript-segments cell shape plus lineage.

        Existing transcription columns use seconds.  Evidence spans use the
        millisecond shape returned by :meth:`as_source_span_fields` instead.
        """

        value: dict[str, Any] = {
            "segment_index": self.segment_index,
            "start": round(self.start_ms / 1000, 3),
            "end": round(self.end_ms / 1000, 3),
            "text": self.text,
            "partial": self.partial,
            "text_precision": self.text_precision,
            "text_may_extend_outside_clip": self.text_may_extend_outside_clip,
            "source": self.lineage(),
        }
        if self.speaker:
            value["speaker"] = self.speaker
        if self.speaker_confidence:
            value["speaker_confidence"] = self.speaker_confidence
        if self.speaker_namespace:
            value["speaker_namespace"] = self.speaker_namespace
        if self.words:
            value["words"] = [word.as_transcript_word() for word in self.words]
        return value

    def as_source_span_fields(self) -> dict[str, Any]:
        """Fields accepted by ``record_source_span`` for the derived artifact."""

        selector: dict[str, Any] = {
            "segment_index": self.segment_index,
            "source_segment_index": self.source_segment_index,
        }
        if self.speaker:
            selector["speaker"] = self.speaker
        if self.speaker_confidence:
            selector["speaker_confidence"] = self.speaker_confidence
        if self.speaker_namespace:
            selector["speaker_namespace"] = self.speaker_namespace
        if self.words:
            selector["words"] = [word.as_span_word() for word in self.words]
        return {
            "span_kind": "temporal",
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "quote": self.text,
            "selector": selector,
            "metadata": {
                "projection": self.lineage(),
                "partial": self.partial,
                "text_precision": self.text_precision,
                "text_may_extend_outside_clip": self.text_may_extend_outside_clip,
            },
        }


@dataclass(frozen=True)
class TranscriptProjection:
    text: str
    segments: tuple[ProjectedTranscriptSegment, ...]
    warnings: tuple[str, ...] = ()
    language: str | None = None

    def transcript_segments_value(self) -> list[dict[str, Any]]:
        return [segment.as_transcript_segment() for segment in self.segments]


def _parse_word(value: Any) -> TranscriptWord | None:
    if not isinstance(value, Mapping):
        return None
    text = str(value.get("word") or value.get("text") or "").strip()
    if not text:
        return None
    start_ms = value.get("start_ms")
    end_ms = value.get("end_ms")
    if start_ms is None or end_ms is None:
        start = _finite_number(value.get("start"))
        end = _finite_number(value.get("end"))
        if start is None or end is None:
            return None
        start_ms = round(start * 1000)
        end_ms = round(end * 1000)
    if not _is_int(start_ms) or not _is_int(end_ms):
        return None
    if start_ms < 0 or end_ms <= start_ms:
        return None
    return TranscriptWord(text=text, start_ms=start_ms, end_ms=end_ms)


def _parse_source_segment(
    value: TranscriptSourceSegment | Mapping[str, Any],
) -> tuple[TranscriptSourceSegment | None, list[str]]:
    if isinstance(value, TranscriptSourceSegment):
        return value, []
    if not isinstance(value, Mapping):
        return None, ["transcript_segment_not_an_object"]
    stable_id = str(value.get("span_stable_id") or value.get("stable_id") or "").strip()
    start_ms = value.get("start_ms")
    end_ms = value.get("end_ms")
    outer_selector = _loads_object(value.get("selector") or value.get("selector_json"))
    if start_ms is None:
        start_ms = outer_selector.get("start_ms")
    if end_ms is None:
        end_ms = outer_selector.get("end_ms")
    temporal = outer_selector.get("temporal")
    if isinstance(temporal, dict):
        selector = dict(temporal)
    else:
        selector = outer_selector
    if not stable_id or not _is_int(start_ms) or not _is_int(end_ms):
        return None, [f"invalid_transcript_segment:{stable_id or 'unknown'}"]
    raw_words = selector.get("words")
    if raw_words is None:
        raw_words = value.get("words")
    warnings: list[str] = []
    words: list[TranscriptWord] = []
    if isinstance(raw_words, list):
        for raw_word in raw_words:
            parsed = _parse_word(raw_word)
            if parsed is None:
                warnings.append(f"invalid_transcript_word:{stable_id}")
            else:
                words.append(parsed)
    segment_index = selector.get("segment_index", value.get("segment_index"))
    if not _is_int(segment_index) or segment_index < 0:
        segment_index = None
    metadata = _loads_object(value.get("metadata"))
    prior_projection = metadata.get("projection")
    if not isinstance(prior_projection, Mapping):
        prior_projection = value.get("source")
    if not isinstance(prior_projection, Mapping):
        prior_projection = {}
    partial = bool(value.get("partial", metadata.get("partial", False)))
    text_precision = str(
        value.get("text_precision")
        or metadata.get("text_precision")
        or "source_segment"
    )
    text_may_extend = bool(
        value.get(
            "text_may_extend_outside_clip",
            metadata.get("text_may_extend_outside_clip", False),
        )
    )
    source_quote = prior_projection.get("source_quote")
    try:
        segment = TranscriptSourceSegment(
            span_stable_id=stable_id,
            start_ms=start_ms,
            end_ms=end_ms,
            text=str(value.get("quote") or value.get("text") or ""),
            segment_index=segment_index,
            speaker=(
                str(selector.get("speaker") or value.get("speaker"))
                if selector.get("speaker") or value.get("speaker")
                else None
            ),
            speaker_confidence=(
                str(
                    selector.get("speaker_confidence")
                    or value.get("speaker_confidence")
                )
                if selector.get("speaker_confidence") or value.get("speaker_confidence")
                else None
            ),
            words=tuple(words),
            metadata=metadata,
            partial=partial,
            text_precision=text_precision,
            text_may_extend_outside_clip=text_may_extend,
            source_quote=str(source_quote) if source_quote is not None else None,
        )
    except ValueError:
        return None, [f"invalid_transcript_segment:{stable_id}"]
    return segment, warnings


_NO_SPACE_BEFORE = re.compile(r"^[,.;:!?%\)\]\}]+$")
_NO_SPACE_AFTER = re.compile(r"^[\(\[\{]+$")


def _join_words(words: Iterable[ProjectedTranscriptWord]) -> str:
    parts: list[str] = []
    previous = ""
    for word in words:
        token = word.text.strip()
        if not token:
            continue
        if not parts:
            parts.append(token)
        elif _NO_SPACE_BEFORE.match(token) or token.startswith(("'", "’")):
            parts[-1] += token
        elif _NO_SPACE_AFTER.match(previous):
            parts[-1] += token
        else:
            parts.append(token)
        previous = token
    return " ".join(parts)


def _clipping(segment: TranscriptSourceSegment, start_ms: int, end_ms: int) -> str:
    left = segment.start_ms < start_ms
    right = segment.end_ms > end_ms
    if left and right:
        return "both"
    if left:
        return "left"
    if right:
        return "right"
    return "full"


def _root_coordinates(
    segment: TranscriptSourceSegment, overlap_start: int, overlap_end: int
) -> tuple[str, int, int]:
    projection = segment.metadata.get("projection")
    if not isinstance(projection, Mapping):
        return segment.span_stable_id, overlap_start, overlap_end
    root_span = str(projection.get("root_span_stable_id") or segment.span_stable_id)
    parent_root_start = projection.get("root_start_ms")
    if not _is_int(parent_root_start):
        return root_span, overlap_start, overlap_end
    return (
        root_span,
        parent_root_start + (overlap_start - segment.start_ms),
        parent_root_start + (overlap_end - segment.start_ms),
    )


def whole_intersecting_transcript_chunks(
    segments: Iterable[Mapping[str, Any]],
    *,
    source_start_ms: int,
    source_end_ms: int,
) -> tuple[tuple[Mapping[str, Any], ...], int, int]:
    """Select chunks touched by a range and return their whole-span window.

    Selection is deliberately based on the original range exactly once.  A
    chunk pulled in by widening the window must not pull in another neighbor in
    turn; callers use the returned chunk identities when projecting so that an
    overlapping ASR chain neither loses words nor expands transitively.
    """

    selected = tuple(
        segment
        for segment in segments
        if int(segment["start_ms"]) < source_end_ms
        and int(segment["end_ms"]) > source_start_ms
    )
    if not selected:
        return (), source_start_ms, source_end_ms
    return (
        selected,
        min(source_start_ms, *(int(segment["start_ms"]) for segment in selected)),
        max(source_end_ms, *(int(segment["end_ms"]) for segment in selected)),
    )


def project_transcript_segments(
    segments: Iterable[TranscriptSourceSegment | Mapping[str, Any]],
    *,
    source_start_ms: int,
    source_end_ms: int,
    source_artifact_stable_id: str,
    transcript_run_id: int | None = None,
    evidence_link_stable_id: str | None = None,
    language: str | None = None,
) -> TranscriptProjection:
    """Intersect and rebase transcript segments onto ``[source_start, end)``.

    Invalid source rows/words are skipped with stable warnings.  A partial
    segment without usable word timestamps emits an explicit marker instead of
    presenting the complete source quote as if it were wholly in the clip.
    """

    if not _is_int(source_start_ms) or not _is_int(source_end_ms):
        raise ValueError("projection range must use integer milliseconds")
    if source_start_ms < 0 or source_end_ms <= source_start_ms:
        raise ValueError("projection range must be positive")
    if not source_artifact_stable_id:
        raise ValueError("source_artifact_stable_id is required")
    if transcript_run_id is not None and (
        not _is_int(transcript_run_id) or transcript_run_id <= 0
    ):
        raise ValueError("transcript_run_id must be a positive integer")

    parsed: list[TranscriptSourceSegment] = []
    warnings: list[str] = []
    for raw in segments:
        segment, item_warnings = _parse_source_segment(raw)
        warnings.extend(item_warnings)
        if segment is not None:
            parsed.append(segment)
    # Transcript ordinal is authoritative when ASR timestamps overlap, nest,
    # or tie.  Use explicit source indices when every segment has one; otherwise
    # preserve the caller's evidence-link order rather than reordering speech by
    # imperfect timestamps.
    source_indices = [item.segment_index for item in parsed]
    if all(index is not None for index in source_indices) and len(
        set(source_indices)
    ) == len(source_indices):
        parsed.sort(key=lambda item: int(item.segment_index))

    projected: list[ProjectedTranscriptSegment] = []
    speaker_namespace = (
        f"media.transcribe:{transcript_run_id}"
        if transcript_run_id is not None
        else None
    )
    for segment in parsed:
        if segment.start_ms >= source_end_ms or segment.end_ms <= source_start_ms:
            continue
        overlap_start = max(segment.start_ms, source_start_ms)
        overlap_end = min(segment.end_ms, source_end_ms)
        if overlap_end <= overlap_start:
            continue
        clipping = _clipping(segment, source_start_ms, source_end_ms)
        clipped_now = clipping != "full"
        partial = clipped_now or segment.partial

        projected_words: list[ProjectedTranscriptWord] = []
        for word in segment.words:
            word_start = max(word.start_ms, segment.start_ms)
            word_end = min(word.end_ms, segment.end_ms)
            if word_start >= overlap_end or word_end <= overlap_start:
                continue
            clipped_start = max(word_start, overlap_start)
            clipped_end = min(word_end, overlap_end)
            if clipped_end <= clipped_start:
                continue
            projected_words.append(
                ProjectedTranscriptWord(
                    text=word.text,
                    start_ms=clipped_start - source_start_ms,
                    end_ms=clipped_end - source_start_ms,
                    source_start_ms=clipped_start,
                    source_end_ms=clipped_end,
                    clipped_left=word.start_ms < clipped_start,
                    clipped_right=word.end_ms > clipped_end,
                )
            )

        if clipped_now and projected_words:
            text = _join_words(projected_words)
            text_precision = "word_timestamps"
            text_may_extend = False
            if not text:
                text = PARTIAL_SPEECH_MARKER
                text_precision = "source_segment"
                text_may_extend = True
        elif clipped_now:
            text = PARTIAL_SPEECH_MARKER
            text_precision = "source_segment"
            text_may_extend = True
        else:
            text = segment.text
            text_precision = segment.text_precision
            text_may_extend = segment.text_may_extend_outside_clip

        root_span, root_start, root_end = _root_coordinates(
            segment, overlap_start, overlap_end
        )
        projected.append(
            ProjectedTranscriptSegment(
                segment_index=len(projected),
                start_ms=overlap_start - source_start_ms,
                end_ms=overlap_end - source_start_ms,
                text=text,
                source_start_ms=overlap_start,
                source_end_ms=overlap_end,
                source_segment_start_ms=segment.start_ms,
                source_segment_end_ms=segment.end_ms,
                source_segment_index=segment.segment_index,
                parent_span_stable_id=segment.span_stable_id,
                root_span_stable_id=root_span,
                root_start_ms=root_start,
                root_end_ms=root_end,
                clipping=clipping,
                partial=partial,
                text_precision=text_precision,
                text_may_extend_outside_clip=text_may_extend,
                speaker=segment.speaker,
                speaker_confidence=segment.speaker_confidence,
                speaker_namespace=speaker_namespace,
                words=tuple(projected_words),
                source_artifact_stable_id=source_artifact_stable_id,
                transcript_run_id=transcript_run_id,
                evidence_link_stable_id=evidence_link_stable_id,
                source_quote=(segment.source_quote or segment.text)
                if partial
                else None,
            )
        )

    text = " ".join(item.text.strip() for item in projected if item.text.strip())
    return TranscriptProjection(
        text=text,
        segments=tuple(projected),
        warnings=tuple(dict.fromkeys(warnings)),
        language=language,
    )
