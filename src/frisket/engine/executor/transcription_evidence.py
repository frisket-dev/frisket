"""Exact-output temporal grounding from checkpointed transcription returns."""

from __future__ import annotations

import math
from typing import Any

from frisket.engine.store.artifact_timeline import ensure_media_timeline
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore


def _milliseconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return round(seconds * 1000) if math.isfinite(seconds) else None


def _temporal_spans(segments, duration_ms):
    previous_start = None
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        start, end = (
            _milliseconds(segment.get("start")),
            _milliseconds(segment.get("end")),
        )
        if start is None or end is None:
            continue
        raw_end = end
        if duration_ms is not None:
            start, end = max(start, 0), min(end, duration_ms)
        if (
            start < 0
            or end <= start
            or (previous_start is not None and start < previous_start)
        ):
            continue
        previous_start = start
        selector = {}
        if segment.get("speaker"):
            selector["speaker"] = str(segment["speaker"])
        words = []
        raw_words = segment.get("words")
        for word in raw_words if isinstance(raw_words, list) else ():
            if not isinstance(word, dict):
                continue
            word_start, word_end = (
                _milliseconds(word.get("start")),
                _milliseconds(word.get("end")),
            )
            if word_start is None or word_end is None:
                continue
            word_start, word_end = max(word_start, start), min(word_end, end)
            if word_end > word_start:
                words.append(
                    {
                        "word": str(word.get("word") or "").strip(),
                        "start_ms": word_start,
                        "end_ms": word_end,
                    }
                )
        if words:
            selector["words"] = words
        yield {
            "start_ms": start,
            "end_ms": end,
            "quote": str(segment.get("text") or ""),
            "selector": selector,
            "metadata": {
                "raw": {"raw_end_ms": raw_end},
                "warnings": ["temporal_span_clamped_to_artifact_duration_ms"],
            }
            if raw_end > end
            else {},
        }


def write_transcription_evidence(
    project,
    spec,
    *,
    batch,
    run_id,
    op_id,
    output_columns,
    transcript_columns: set[int],
    writer_attempt_id,
    claim_token,
    **kwargs,
):
    """Called inside the result savepoint, never from mutable output siblings.

    ``transcript_columns`` is projected from declared TranscriptText fields by
    the host. Equal incidental strings are not an attribution request. Returned
    text that an author rewrites still publishes, but gets no precise grounding.
    """
    reads = {
        int(item["row_id"]): call
        for item in batch
        for call in item.get("row_file_calls") or []
        if isinstance(call, dict) and call.get("kind") == "transcribe_read"
    }
    if not reads or not transcript_columns:
        return
    OutputColumnClaimStore.require_current_writer(
        project.db,
        run_id=run_id,
        writer_attempt_id=writer_attempt_id,
        claim_token=claim_token,
    )
    receipt_id = project.db.execute(
        "SELECT DISTINCT receipt_id FROM output_column_claims "
        "WHERE run_id=? AND claim_token=? AND status='active'",
        (run_id, claim_token),
    ).fetchone()[0]
    store = ReceiptStore(project)
    for item in batch:
        row_id, column_id = int(item["row_id"]), int(item["column_id"])
        read = reads.get(row_id)
        value = item.get("value")
        if (
            column_id not in transcript_columns
            or item.get("error") is not None
            or read is None
            or not isinstance(value, str)
            or not value.strip()
            or value != read["text"]
        ):
            continue
        source = read["source"]
        media = source["value"]
        if not isinstance(media, dict) or not media.get("blob"):
            continue
        if (
            project.db.execute(
                "SELECT 1 FROM evidence_links WHERE run_id=? AND row_id=? AND column_id=? "
                "AND link_role='media_transcribe_temporal' LIMIT 1",
                (run_id, row_id, column_id),
            ).fetchone()
            is not None
        ):
            continue
        duration = source.get("duration_ms")
        duration = duration if type(duration) is int and duration > 0 else None
        segments = list(_temporal_spans(read["segments"], duration))
        if not segments:
            continue
        artifact_kwargs = {
            "blob_hash": media["blob"],
            "media_type": media.get("mime"),
            "filename": media.get("filename"),
            "duration_ms": duration,
            "source_sheet_id": source["sheet_id"],
            "source_row_id": row_id,
            "source_column_id": source["column_id"],
            "metadata": {"engine": read["engine"]},
        }
        if duration is None:
            artifact = record_source_artifact(
                project, artifact_kind="av", **artifact_kwargs
            )
        else:
            timeline = ensure_media_timeline(project, **artifact_kwargs)
            artifact = {
                "id": timeline.artifact_id,
                "stable_id": timeline.artifact_stable_id,
            }
        spans = []
        for rank, segment in enumerate(segments):
            segment["selector"]["segment_index"] = rank
            span = record_source_span(
                project, artifact_id=artifact["id"], span_kind="temporal", **segment
            )
            spans.append({"span_id": span["id"], "rank": rank})
        metadata = {
            "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
            "semantic_type": "timestamped_transcript",
        }
        language = read.get("language")
        if isinstance(language, str) and language.strip():
            metadata["language"] = language.strip()
        link = record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref={
                "kind": "run_result",
                "run_id": run_id,
                "op_id": op_id,
                "row_id": row_id,
                "column_id": column_id,
            },
            spans=spans,
            sheet_id=source["sheet_id"],
            row_id=row_id,
            column_id=column_id,
            run_id=run_id,
            op_id=op_id,
            receipt_id=receipt_id,
            link_role="media_transcribe_temporal",
            producer={"action_kind": spec["action_kind"], "engine": read["engine"]},
            metadata=metadata,
        )
        store._record_writer_evidence(
            {
                "kind": "media_transcribe_temporal_evidence_link",
                "sheet_id": source["sheet_id"],
                "row_id": row_id,
                "column_id": column_id,
                "artifact_id": artifact["id"],
                "artifact_stable_id": artifact["stable_id"],
                "evidence_link_id": link["id"],
                "stable_id": link["stable_id"],
            },
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
        )
