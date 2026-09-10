from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from frisket.engine.store.grounding_contract import WordStream, WordToken


@dataclass(frozen=True)
class ResolvedWordStream:
    """The positioned OCR stream for a blob + the page images its region spans
    need to render, resolved from a sibling ``media.ocr`` artifact."""

    stream: WordStream
    page_images: dict[str, Any]
    artifact_id: int


@dataclass(frozen=True)
class ResolvedOcrEvidence:
    """One exact, current OCR grounding link and its addressable spans."""

    artifact_id: int
    evidence_link_id: int
    column_id: int
    column_name: str
    column_position: int
    value_ref: dict[str, Any]
    spans: tuple[dict[str, Any], ...]


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def resolve_ocr_word_stream(
    project: Any, blob_hash: str | None
) -> ResolvedWordStream | None:
    """Resolve the OCR word stream + page images for ``blob_hash`` from the most
    recent sibling ``media.ocr`` artifact, or ``None`` if no positioned OCR
    stream exists for the blob (-> Degradation Law at the caller)."""

    if not blob_hash:
        return None

    rows = project.db.execute(
        "SELECT id, metadata FROM source_artifacts WHERE blob_hash=? ORDER BY id DESC",
        (str(blob_hash),),
    ).fetchall()

    for row in rows:
        metadata = _loads(row["metadata"], {})
        if not isinstance(metadata, dict):
            continue
        page_images = metadata.get("page_images")
        # An OCR artifact is the one carrying a persisted page_images map + an
        # engine (media.ocr's evidence writer).
        if not isinstance(page_images, dict) or not metadata.get("engine"):
            continue
        artifact_id = int(row["id"])
        tokens = _tokens_from_region_spans(project, artifact_id)
        if not tokens:
            continue
        return ResolvedWordStream(
            stream=WordStream(tokens=tokens),
            page_images=page_images,
            artifact_id=artifact_id,
        )
    return None


def resolve_current_ocr_evidence(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    column_id: int | None = None,
    blob_hash: str | None = None,
) -> tuple[ResolvedOcrEvidence, ...]:
    """Resolve exact current OCR derivatives for a selected cell or source blob.

    Unlike :func:`resolve_ocr_word_stream`, this never chooses the newest
    artifact. Callers either address the OCR text column itself or require one
    unambiguous current sibling link for the selected image/file blob.
    """

    params: list[Any] = [int(sheet_id), int(row_id)]
    column_filter = ""
    if column_id is not None:
        column_filter = " AND el.column_id=?"
        params.append(int(column_id))
    rows = project.db.execute(
        "SELECT DISTINCT el.id, el.column_id, el.subject_ref_json, "
        "sp.artifact_id, sa.blob_hash, c.name, c.position "
        "FROM evidence_links el "
        "JOIN evidence_link_spans els ON els.link_id=el.id "
        "JOIN source_spans sp ON sp.id=els.span_id "
        "JOIN source_artifacts sa ON sa.id=sp.artifact_id "
        "JOIN columns c ON c.id=el.column_id "
        "WHERE el.status='active' AND el.link_role='media_ocr_grounding' "
        "AND el.subject_kind='cell' AND el.sheet_id=? AND el.row_id=?"
        + column_filter
        + " ORDER BY c.position, el.id",
        params,
    ).fetchall()
    resolved: list[ResolvedOcrEvidence] = []
    for row in rows:
        if blob_hash is not None and str(row["blob_hash"] or "") != str(blob_hash):
            continue
        candidate_column_id = int(row["column_id"])
        values, refs = project.get_values_with_refs(
            sheet_id, candidate_column_id, row_ids=[row_id]
        )
        value = values.get(row_id)
        value_ref = refs.get(row_id)
        if (
            not isinstance(value, str)
            or not value.strip()
            or not isinstance(value_ref, dict)
            or _loads(row["subject_ref_json"], {}) != value_ref
        ):
            continue
        span_rows = project.db.execute(
            "SELECT sp.*, els.rank FROM evidence_link_spans els "
            "JOIN source_spans sp ON sp.id=els.span_id "
            "WHERE els.link_id=? ORDER BY els.rank, sp.id",
            (int(row["id"]),),
        ).fetchall()
        if not span_rows or any(
            int(span["artifact_id"]) != int(row["artifact_id"]) for span in span_rows
        ):
            continue
        addressed = [
            span
            for span in span_rows
            if span["span_kind"] == "region"
            or (span["span_kind"] == "page_range" and str(span["quote"] or "").strip())
        ]
        # Older OCR artifacts stored all-unpositioned pages only as summary
        # spans. Preserve that coarse but valid grounding when no block-level
        # span exists; current writers persist quoted page spans per block.
        if not addressed:
            addressed = [
                span for span in span_rows if span["span_kind"] == "page_range"
            ]
        spans = tuple(
            {
                "id": int(span["id"]),
                "stable_id": str(span["stable_id"]),
                "artifact_id": int(span["artifact_id"]),
                "span_kind": str(span["span_kind"]),
                "quote": str(span["quote"] or span["snippet"] or ""),
                "rank": int(span["rank"]),
            }
            for span in addressed
            if str(span["quote"] or span["snippet"] or "").strip()
        )
        if not spans:
            continue
        resolved.append(
            ResolvedOcrEvidence(
                artifact_id=int(row["artifact_id"]),
                evidence_link_id=int(row["id"]),
                column_id=candidate_column_id,
                column_name=str(row["name"]),
                column_position=int(row["position"]),
                value_ref=value_ref,
                spans=spans,
            )
        )
    return tuple(resolved)


def _tokens_from_region_spans(project: Any, artifact_id: int) -> list[WordToken]:
    span_rows = project.db.execute(
        "SELECT page_start, bbox_json, quote, snippet FROM source_spans "
        "WHERE artifact_id=? AND span_kind='region' ORDER BY id",
        (artifact_id,),
    ).fetchall()
    tokens: list[WordToken] = []
    for span in span_rows:
        bbox_list = _loads(span["bbox_json"], [])
        if not isinstance(bbox_list, list) or not bbox_list:
            continue
        box = bbox_list[0]
        if not isinstance(box, dict):
            continue
        try:
            coords = (
                float(box["x0"]),
                float(box["y0"]),
                float(box["x1"]),
                float(box["y1"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        text = span["quote"] or span["snippet"] or ""
        page = span["page_start"]
        tokens.append(
            WordToken(
                text=str(text),
                box=coords,
                page=int(page) if page is not None else None,
                source="ocr",
            )
        )
    return tokens
