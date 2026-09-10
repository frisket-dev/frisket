"""Extraction grounding and required-value publication at the host transaction."""

from __future__ import annotations

import hashlib
from typing import Any


_UNRESOLVED_TRANSCRIPT = object()


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_grounding_method(items: list[dict[str, Any]]) -> str | None:
    for item in items:
        method = _optional_string(item.get("grounding_method"))
        if method:
            return method
    return None


def _normalized_bbox(value: Any) -> dict[str, Any] | None:
    """map.extract regions are page-frame boxes; the canonical coercion now
    lives in ``store/grounding.normalize_bbox`` (the shared home OCR and
    extract_faces also use). map.extract keeps emitting ``page_normalized``."""
    from frisket.engine.store.grounding import normalize_bbox

    return normalize_bbox(value, frame="page")


def _source_artifact(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    source_columns: list[str],
    input_column_ids: dict[str, int],
    artifact_cache: dict[tuple[int, int, str], dict[str, Any]],
    captured_sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from frisket.engine.store.evidence import record_source_artifact
    from frisket.engine.store.media_blobs import MediaBlobStore

    for name in source_columns:
        column_id = input_column_ids.get(name)
        if column_id is None:
            continue
        value = (
            captured_sources.get(name, {}).get("value")
            if captured_sources is not None
            else project.get_values(sheet_id, column_id, row_ids=[row_id]).get(row_id)
        )
        if not isinstance(value, dict) or not value.get("blob"):
            continue
        blob_hash = str(value["blob"])
        cache_key = (row_id, column_id, blob_hash)
        if cache_key in artifact_cache:
            return artifact_cache[cache_key]
        metadata = MediaBlobStore(project).probe_metadata(blob_hash)
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type=str(value.get("mime") or "application/octet-stream"),
            blob_hash=blob_hash,
            filename=value.get("filename"),
            page_count=_int_or_none(metadata.get("pages")),
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            source_column_id=column_id,
            metadata=metadata,
        )
        artifact_cache[cache_key] = artifact
        return artifact
    cache_key = (row_id, 0, "row")
    if cache_key not in artifact_cache:
        artifact_cache[cache_key] = record_source_artifact(
            project,
            artifact_kind="row",
            media_type="application/vnd.frisket.row+json",
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            metadata={
                "source_columns": source_columns,
                **(
                    {"captured_sources": captured_sources}
                    if captured_sources is not None
                    else {}
                ),
            },
        )
    return artifact_cache[cache_key]


def _boxes_overlap(
    box_a: dict[str, Any], box_b: tuple[float, float, float, float]
) -> bool:
    try:
        ax0, ay0, ax1, ay1 = (
            float(box_a["x0"]),
            float(box_a["y0"]),
            float(box_a["x1"]),
            float(box_a["y1"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    bx0, by0, bx1, by1 = box_b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    return ix0 < ix1 and iy0 < iy1


def _region_spans_from_matches(
    project: Any,
    *,
    artifact: dict[str, Any],
    matches: list[Any],
    page_images: dict[str, Any],
    snippet: str | None,
    base_meta: dict[str, Any],
    alignment_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Persist a strategy result's region spans onto the extract artifact,
    borrowing the sibling OCR artifact's ``page_images`` so they render (W2.1)."""

    from frisket.engine.store.evidence import (
        merge_artifact_metadata,
        record_source_span,
    )

    merge_artifact_metadata(project, int(artifact["id"]), {"page_images": page_images})
    spans: list[dict[str, Any]] = []
    for match in matches:
        for spec in match.spans:
            if spec.span_kind != "region":
                continue
            meta = {**base_meta, **alignment_meta, "align_score": match.score}
            if spec.metadata:
                meta = {**meta, **spec.metadata}
            spans.append(
                record_source_span(
                    project,
                    artifact_id=int(artifact["id"]),
                    span_kind="region",
                    page_start=spec.page_start,
                    page_end=spec.page_end or spec.page_start,
                    bbox=spec.bbox,
                    quote=spec.quote,
                    snippet=snippet,
                    metadata=meta,
                )
            )
    return spans


def _model_bbox_spans(
    project: Any,
    *,
    artifact: dict[str, Any],
    bbox: dict[str, Any],
    page: int | None,
    page_end: int | None,
    quote: str | None,
    snippet: str | None,
    blob_hash: str | None,
    base_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fail-closed model-bbox verification (extract-model-bbox-verification-v1).

    A ``grounding_method=model_bbox`` rect is an UNVERIFIED model guess. Keep it
    as an authoritative ``region`` only if a resolvable OCR word stream
    corroborates it: the model box must overlap >=1 OCR block AND the quote must
    align within the overlapped blocks. Missing any of those (no word stream, no
    overlap, no quote, or a below-threshold quote) -> downgrade to ``page_range``
    + a ``model_bbox_unverified`` warning (PT SILENTLY-WRONG #1). Never render an
    unverifiable model box."""

    from frisket.engine.store.evidence import record_source_span
    from frisket.engine.store.grounding_contract import TextTarget, WordStream, ground
    from frisket.engine.store.ocr_word_stream import resolve_ocr_word_stream

    resolved = resolve_ocr_word_stream(project, blob_hash)
    if resolved is not None and quote is not None:
        overlapping = [
            token
            for token in resolved.stream.tokens
            if token.box is not None
            and _boxes_overlap(bbox, token.box)
            and (page is None or token.page is None or token.page == page)
        ]
        if overlapping:
            matches = ground(TextTarget(text=quote), WordStream(tokens=overlapping))
            if matches:
                # Corroborated: snap to the aligned line boxes (strictly more
                # honest than the raw model box), borrowing page_images.
                spans = _region_spans_from_matches(
                    project,
                    artifact=artifact,
                    matches=matches,
                    page_images=resolved.page_images,
                    snippet=snippet,
                    base_meta=base_meta,
                    alignment_meta={"alignment": "model_bbox_verified"},
                )
                if spans:
                    return spans

    # Fail closed: downgrade to the coarser honest anchor + a loud warning.
    meta = {
        **base_meta,
        "alignment": "model_bbox_unverified",
        "warnings": [*base_meta.get("warnings", []), "model_bbox_unverified"],
    }
    if page is not None:
        return [
            record_source_span(
                project,
                artifact_id=int(artifact["id"]),
                span_kind="page_range",
                page_start=page,
                page_end=page_end,
                snippet=snippet or quote or f"Page {page}",
                metadata=meta,
            )
        ]
    return [
        record_source_span(
            project,
            artifact_id=int(artifact["id"]),
            span_kind="text",
            quote=quote,
            snippet=snippet,
            metadata=meta,
        )
    ]


# Two occurrences whose top scores agree within this epsilon are treated as
# indistinguishable (a fuzzy-score tie): with nothing to disambiguate them we must
# NOT write both (highlights scattered across the doc) — we degrade (BLOCKER 2).
_REPEATED_QUOTE_EPSILON = 1e-6


def _aligned_quote_spans(
    project: Any,
    *,
    artifact: dict[str, Any],
    quote: str,
    snippet: str | None,
    blob_hash: str | None,
    base_meta: dict[str, Any],
    page: int | None = None,
    page_end: int | None = None,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Align a positionless extract quote to the resolved OCR word stream and
    emit one ``region`` span per covered line of the SINGLE winning occurrence
    (W2.1). Returns ``(spans, "aligned")`` on a confident, unambiguous match, else
    ``(None, reason)`` where ``reason`` is ``"no_word_stream"`` (no positioned
    OCR/text stream for this blob — the legible degraded state that drives the
    viewer copy), ``"below_threshold"`` (a stream exists but nothing cleared the
    bar), or ``"repeated_quote_ambiguous"`` (>=2 occurrences tie within
    :data:`_REPEATED_QUOTE_EPSILON` and nothing disambiguates them -> we refuse to
    scatter boxes across every occurrence). Any reason -> Degradation Law at the
    caller; never a guessed box.

    SCOPE INVARIANT (BLOCKER 2): when the item carries ``page``/``page_end`` the
    resolved stream is filtered to those pages BEFORE aligning, so a repeat on
    another page is structurally invisible and the right occurrence wins."""

    from frisket.engine.store.grounding_contract import TextTarget, WordStream, ground
    from frisket.engine.store.ocr_word_stream import resolve_ocr_word_stream

    resolved = resolve_ocr_word_stream(project, blob_hash)
    if resolved is None:
        return None, "no_word_stream"

    tokens = resolved.stream.tokens
    if page is not None:
        hi = page_end if page_end is not None else page
        lo, hi = min(page, hi), max(page, hi)
        # Page-less tokens must not compete with positioned in-scope tokens:
        # a legacy or corrupt page-less duplicate could turn a valid page hit
        # into repeated_quote_ambiguous.
        scoped = [
            tok for tok in tokens if tok.page is not None and lo <= tok.page <= hi
        ]
        # Only narrow when the page scope actually leaves positioned tokens; an
        # empty scope means the quote's page has no stream -> below_threshold.
        if not scoped:
            return None, "below_threshold"
        tokens = scoped

    matches = ground(TextTarget(text=quote), WordStream(tokens=tokens), threshold=0.8)
    if not matches:
        return None, "below_threshold"
    if len(matches) > 1 and (
        abs(matches[0].score - matches[1].score) <= _REPEATED_QUOTE_EPSILON
    ):
        # Indistinguishable repeats with no disambiguator -> degrade, keep the
        # coarse text/page anchor (never all candidates).
        return None, "repeated_quote_ambiguous"
    # Write only the single winning occurrence's per-line region spans.
    spans = _region_spans_from_matches(
        project,
        artifact=artifact,
        matches=matches[:1],
        page_images=resolved.page_images,
        snippet=snippet,
        base_meta=base_meta,
        alignment_meta={"alignment": "aligned"},
    )
    if not spans:
        return None, "below_threshold"
    return spans, "aligned"


def _transcript_segment_indices_spans(
    project: Any,
    *,
    artifact: dict[str, Any],
    sheet_id: int,
    row_id: int,
    entry: dict[str, Any],
    rank: int,
    transcript_stream: Any = _UNRESOLVED_TRANSCRIPT,
) -> list[dict[str, Any]] | None:
    """Strategy `anchor_id` (B), the transcript-sourced list-item DEFAULT
    (W2.3): resolve the model's `segment_indices` against the row's own
    transcript segments BY LOOKUP -- never by re-matching quote text (§2's
    correctness note). Returns ``None`` when the entry carries no
    `segment_indices` (caller tries the next resolution path); ``[]`` when a
    transcript stream resolves for this row but every index is invalid
    (Degradation Law -- the item falls through to unsupported, never a guess).

    Every matched span REUSES the exact span row ``media.transcribe``'s
    evidence writer already recorded for that segment (looked up via
    ``resolved.span_ids_by_segment_index`` -- ``transcript_segment_stream``'s
    own SELECT, keyed by segment index) rather than re-recording a duplicate:
    the citation then points at the SAME ``av`` artifact (real audio/video
    ``media_type``, blob-hash-backed) the transcript's own link uses, so the
    evidence viewer renders the seekable player instead of falling back to
    "no first-party renderer" (extract-scalar-temporal-anchors-v1 gap: spans
    used to land on the caller's ``artifact`` -- the row-json artifact when no
    source column carried the raw media blob directly). A lookup miss (should
    not happen -- every anchor unit is built from a real span row) is a
    defensive fallback: record fresh, but STILL against the transcript
    artifact (``resolved.artifact_id``), never the caller's ``artifact``."""

    raw_indices = entry.get("segment_indices")
    if not isinstance(raw_indices, list) or not raw_indices:
        return None
    # Tolerate the shapes real models return: ints, but also numeric strings
    # ("2") and floats (2.0) that the structured-output stack may surface for an
    # integer-typed array. A non-numeric entry is dropped, not fatal.
    indices: list[int] = []
    for value in raw_indices:
        coerced = _int_or_none(value)
        if coerced is not None:
            indices.append(coerced)
    if not indices:
        return None

    from frisket.engine.store.evidence import get_source_span, record_source_span
    from frisket.engine.store.grounding_contract import AnchorTarget, ground
    from frisket.engine.store.transcript_segment_stream import (
        resolve_transcript_segment_stream,
    )

    resolved = (
        resolve_transcript_segment_stream(project, sheet_id=sheet_id, row_id=row_id)
        if transcript_stream is _UNRESOLVED_TRANSCRIPT
        else transcript_stream
    )
    if resolved is None:
        return []
    matches = ground(
        AnchorTarget(unit_ids=indices), resolved.anchors, method="anchor_id"
    )
    if not matches:
        return []
    metadata = {
        "raw": entry,
        "grounding_method": "segment_indices",
        "rank": rank,
        "alignment": "segment_anchor",
        "warnings": [],
    }
    spans: list[dict[str, Any]] = []
    for span_spec in matches[0].spans:
        segment_index = span_spec.metadata.get("segment_index")
        existing_span_id = (
            resolved.span_ids_by_segment_index.get(segment_index)
            if isinstance(segment_index, int)
            else None
        )
        if existing_span_id is not None:
            spans.append(get_source_span(project, existing_span_id))
            continue
        spans.append(
            record_source_span(
                project,
                artifact_id=resolved.artifact_id,
                span_kind=span_spec.span_kind,
                start_ms=span_spec.start_ms,
                end_ms=span_spec.end_ms,
                quote=span_spec.quote,
                selector=(
                    {"segment_index": segment_index}
                    if segment_index is not None
                    else None
                ),
                metadata=metadata,
            )
        )
    return spans


def _transcript_quote_spans(
    project: Any,
    *,
    artifact: dict[str, Any],
    sheet_id: int,
    row_id: int,
    entry: dict[str, Any],
    rank: int,
    transcript_stream: Any = _UNRESOLVED_TRANSCRIPT,
) -> list[dict[str, Any]] | None:
    """Strategy `align` (A), the transcript FALLBACK (W2.3) -- only reached
    when the entry carried no `segment_indices` (retroactive items, or a
    prompt path that did not number segments). Aligns a free-text quote
    against the row's transcript segment stream; returns ``None`` when no
    transcript stream resolves for this row (caller falls through to the
    OCR/text path) or when the entry has no quote at all.

    The aligner (``store.quote_align.align``) reports which segment(s) its
    matched range covers via ``span_spec.metadata["segment_indices"]``. A
    single-segment match REUSES that segment's already-recorded span (same
    lookup-not-record preference as strategy B, above) so the citation lands
    on the transcribe writer's ``av`` artifact. A merged multi-segment match
    (or the defensive miss) has no single existing span to reuse -- it
    records a fresh one, but STILL against ``resolved.artifact_id`` (the
    transcript's own artifact), never the caller's ``artifact`` (extract's
    row-json/text artifact, which carries no renderable blob)."""

    quote = _optional_string(entry.get("quote"))
    if quote is None:
        return None

    from frisket.engine.store.transcript_segment_stream import (
        resolve_transcript_segment_stream,
    )

    resolved = (
        resolve_transcript_segment_stream(project, sheet_id=sheet_id, row_id=row_id)
        if transcript_stream is _UNRESOLVED_TRANSCRIPT
        else transcript_stream
    )
    if resolved is None:
        return None

    from frisket.engine.store.evidence import get_source_span, record_source_span
    from frisket.engine.store.grounding_contract import TextTarget, ground

    matches = ground(TextTarget(text=quote), resolved.segments, threshold=0.8)
    if not matches:
        return []
    span_spec = matches[0].spans[0]
    segment_indices = span_spec.metadata.get("segment_indices")
    if isinstance(segment_indices, list) and len(segment_indices) == 1:
        existing_span_id = resolved.span_ids_by_segment_index.get(segment_indices[0])
        if existing_span_id is not None:
            return [get_source_span(project, existing_span_id)]
    metadata = {
        "raw": entry,
        "grounding_method": "quote",
        "rank": rank,
        "alignment": "aligned_temporal",
        "warnings": [],
    }
    return [
        record_source_span(
            project,
            artifact_id=resolved.artifact_id,
            span_kind=span_spec.span_kind,
            start_ms=span_spec.start_ms,
            end_ms=span_spec.end_ms,
            quote=span_spec.quote,
            metadata=metadata,
        )
    ]


# Alignment tags that denote a CONFIDENT, non-degraded resolution -- any other
# tag on a span's metadata is surfaced as a link-level warning so the viewer can
# show the honest degraded copy. `aligned_temporal`/`segment_anchor` are the
# transcript wins (temporal spans); `aligned`/`model_bbox_verified` the OCR wins.
_CONFIDENT_ALIGNMENTS = {
    "aligned",
    "aligned_temporal",
    "segment_anchor",
    "model_bbox_verified",
}


def _resolve_evidence_entry_spans(
    project: Any,
    *,
    artifact: dict[str, Any],
    sheet_id: int,
    row_id: int,
    entry: dict[str, Any],
    rank: int,
    transcript_stream: Any = _UNRESOLVED_TRANSCRIPT,
) -> list[dict[str, Any]] | None:
    """Resolve ONE evidence entry into spans, transcript-first (W2.3). Tries in
    order: transcript `segment_indices` (strategy B, the DEFAULT for
    transcript-sourced evidence) -> transcript quote alignment (strategy A, the
    FALLBACK, only when a transcript stream resolves for this row) -> the
    unchanged OCR/PDF `_source_spans` path (bbox / aligned quote / page /
    snippet). Never mixes strategies within one entry; each entry resolves
    through exactly one path.

    When a transcript stream resolves and the entry aligns, the emitted TEMPORAL
    spans REPLACE (rather than accompany) the coarse text anchor: the temporal
    span already carries the segment `quote` AND `start_ms/end_ms`, so a separate
    text span would double-render (a player-less quote anchor + the seekable
    temporal anchor) for the same evidence. No stream / no alignment -> today's
    text/region/page anchor (Degradation Law). Returns ``[]`` when the entry
    resolved through a transcript path but produced nothing usable (the caller
    treats that as unsupported); a non-``None`` list otherwise."""

    entry_spans = _transcript_segment_indices_spans(
        project,
        artifact=artifact,
        sheet_id=sheet_id,
        row_id=row_id,
        entry=entry,
        rank=rank,
        transcript_stream=transcript_stream,
    )
    if entry_spans is None:
        entry_spans = _transcript_quote_spans(
            project,
            artifact=artifact,
            sheet_id=sheet_id,
            row_id=row_id,
            entry=entry,
            rank=rank,
            transcript_stream=transcript_stream,
        )
    if entry_spans is None:
        entry_spans = _source_spans(project, artifact=artifact, item=entry, rank=rank)
    return entry_spans


def _source_spans(
    project: Any, *, artifact: dict[str, Any], item: dict[str, Any], rank: int
) -> list[dict[str, Any]]:
    """Resolve an evidence item into 1..N source spans (W2.1). A positionless
    ``quote`` aligns to the OCR word stream -> per-line ``region`` spans; a
    ``model_bbox`` is verified fail-closed; everything else keeps today's honest
    coarse behavior. Empty list means the item produced no usable span."""

    from frisket.engine.store.evidence import record_source_span

    bbox = _normalized_bbox(item.get("bbox"))
    page = _int_or_none(item.get("page") or item.get("page_start"))
    page_end = _int_or_none(item.get("page_end")) or page
    quote = _optional_string(item.get("quote"))
    snippet = _optional_string(item.get("snippet"))
    grounding_method = _optional_string(item.get("grounding_method"))
    blob_hash = _optional_string(artifact.get("blob_hash"))
    metadata = {
        "raw": item,
        "grounding_method": grounding_method,
        "rank": rank,
        "warnings": [],
    }
    if bbox is not None:
        if grounding_method == "model_bbox":
            return _model_bbox_spans(
                project,
                artifact=artifact,
                bbox=bbox,
                page=page,
                page_end=page_end,
                quote=quote,
                snippet=snippet,
                blob_hash=blob_hash,
                base_meta=metadata,
            )
        return [
            record_source_span(
                project,
                artifact_id=int(artifact["id"]),
                span_kind="region",
                page_start=page,
                page_end=page_end,
                bbox=[bbox],
                quote=quote,
                snippet=snippet,
                metadata=metadata,
            )
        ]
    if quote is not None:
        spans, reason = _aligned_quote_spans(
            project,
            artifact=artifact,
            quote=quote,
            snippet=snippet,
            blob_hash=blob_hash,
            base_meta=metadata,
            page=page,
            page_end=page_end,
        )
        if spans is not None:
            return spans
        # Degradation Law: keep the positionless text span, but record WHY the
        # highlight is coarse so the viewer can show the honest degraded copy.
        metadata["alignment"] = reason
        if reason == "repeated_quote_ambiguous":
            metadata["warnings"] = [*metadata.get("warnings", []), reason]
        return [
            record_source_span(
                project,
                artifact_id=int(artifact["id"]),
                span_kind="text",
                page_start=page,
                page_end=page_end,
                quote=quote,
                snippet=snippet,
                metadata=metadata,
            )
        ]
    if page is not None:
        return [
            record_source_span(
                project,
                artifact_id=int(artifact["id"]),
                span_kind="page_range",
                page_start=page,
                page_end=page_end,
                snippet=snippet or f"Page {page}",
                metadata=metadata,
            )
        ]
    if snippet is not None:
        return [
            record_source_span(
                project,
                artifact_id=int(artifact["id"]),
                span_kind="whole",
                snippet=snippet,
                metadata=metadata,
            )
        ]
    return []


def _is_missing_value(value: Any) -> bool:
    """A required field is UNMET when the model found nothing: a real null (incl.
    the routing-boundary coercion of a ``"null"`` string), an empty string, or an
    empty list/dict. A ``0``/``False``/``0.0`` is a found value, never missing."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False
