"""Read-only recovery previews for stale text evidence.

The stored citation remains the historical record.  This module only decorates
one freshly-built viewer response after an explicit user request.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from frisket.engine.store.citation_text import read_artifact_passage
from frisket.engine.store.current_value_windows import (
    CurrentValueUnavailable,
    CurrentValueNotTextual,
    current_text_metadata,
    read_current_text,
    MANUAL_EDIT_BYTE_LIMIT,
    TEXT_WINDOW_CODEPOINT_LIMIT,
)
from frisket.engine.store.text_quote_match import quote_ranges


def _utf16_offset(text: str, codepoint_offset: int) -> int:
    return len(text[:codepoint_offset].encode("utf-16-le")) // 2


def _notice(*, matched_spans: int, total_spans: int, repeated: bool) -> str:
    if matched_spans < total_spans:
        return "Found some saved quotes in the current source."
    if repeated:
        return "Found saved quotes in multiple places in the current source."
    if total_spans == 1:
        return "Found the saved quote in the current source."
    return "Found the saved quotes in the current source."


def locate_current_evidence_text(
    db: sqlite3.Connection,
    viewer: dict[str, Any],
    *,
    text_artifact_id: int | None = None,
    text_start: int | None = None,
    text_limit: int | None = None,
    text_revision: int | None = None,
    text_span_id: str | None = None,
) -> dict[str, Any]:
    """Add current-text quote locations to a viewer response without writes.

    Small native contexts retain every exact/whitespace-equivalent match.
    Large contexts search bounded sections and return one passage with explicit
    partial coverage. Media and other non-text sources keep their saved anchors.
    """

    artifacts = viewer.get("artifacts")
    if not isinstance(artifacts, list):
        return viewer
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        cell = artifact.get("source_cell") or {}
        capture = artifact.get("metadata") or {}
        native = isinstance(capture.get("captured_text_hash"), str) and not capture.get(
            "captured_text_frozen", False
        )
        coordinate = {key: cell.get(key) for key in ("sheet_id", "row_id", "column_id")}
        try:
            meta = (
                current_text_metadata(db, **coordinate)
                if native and all(type(value) is int for value in coordinate.values())
                else None
            )
        except (CurrentValueUnavailable, CurrentValueNotTextual):
            meta = None
        if meta is None or meta.value_kind != "text":
            artifact["recovery_status"] = "unsupported"
            artifact["recovery_notice"] = (
                "This citation cannot be located in the current source."
            )
            continue

        if meta.total_bytes > MANUAL_EDIT_BYTE_LIMIT:
            selected = text_artifact_id is None or artifact["id"] == text_artifact_id
            _locate_bounded(
                db,
                viewer,
                artifact,
                coordinate,
                meta,
                start=text_start if selected else None,
                limit=text_limit if selected else None,
                revision=text_revision if selected else None,
                span_id=text_span_id if selected else None,
            )
            continue
        current_text = read_current_text(
            db, **coordinate, metadata=meta, start=0, limit=meta.total_characters
        )

        spans = artifact.get("spans")
        quoted_spans = (
            [
                span
                for span in spans
                if isinstance(span, dict)
                and isinstance(span.get("quote"), str)
                and span["quote"]
            ]
            if isinstance(spans, list)
            else []
        )
        for index, span in enumerate(quoted_spans):
            if span.get("quote_truncated"):
                row = db.execute(
                    "SELECT CASE WHEN length(CAST(quote AS BLOB))<=? THEN quote END FROM source_spans WHERE id=?",
                    (MANUAL_EDIT_BYTE_LIMIT, span["id"]),
                ).fetchone()
                quoted_spans[index] = {
                    **span,
                    "quote": row[0] if row is not None else None,
                    "quote_truncated": False,
                }
        quoted_spans = [
            span
            for span in quoted_spans
            if span.get("quote") and not span.get("quote_truncated")
        ]
        if not quoted_spans:
            artifact["recovery_status"] = "unsupported"
            artifact["recovery_notice"] = "This citation has no saved quote to locate."
            continue

        ranges: list[dict[str, Any]] = []
        matched_spans = 0
        repeated = False
        for span in quoted_spans:
            matches = quote_ranges(current_text, span["quote"])
            if matches:
                matched_spans += 1
                repeated = repeated or len(matches) > 1
            span_id = str(span.get("stable_id") or span.get("id") or "")
            for start, end in matches:
                ranges.append(
                    {
                        "span_id": span_id,
                        "start": _utf16_offset(current_text, start),
                        "end": _utf16_offset(current_text, end),
                    }
                )

        artifact["text_context"] = {
            "text": current_text,
            "offset_unit": "utf16_code_unit",
            "ranges": ranges,
        }
        if ranges:
            artifact["recovery_status"] = "located"
            artifact["recovery_notice"] = _notice(
                matched_spans=matched_spans,
                total_spans=len(quoted_spans),
                repeated=repeated,
            )
        else:
            artifact["recovery_status"] = "not_found"
            artifact["recovery_notice"] = (
                "The saved quote was not found in the current source."
            )
    return viewer


def _locate_bounded(
    db, viewer, artifact, coordinate, meta, *, start, limit, revision, span_id
):
    """Explicit recovery scans bounded sections; it never promises all matches."""
    quoted = [
        span
        for span in artifact.get("spans", [])
        if isinstance(span.get("quote"), str)
        and span["quote"]
        and not span.get("quote_truncated")
    ]
    if span_id is not None:
        quoted.sort(key=lambda span: span.get("stable_id") != span_id)
    if not quoted:
        artifact.update(
            recovery_status="unsupported",
            recovery_notice="This citation has no bounded saved quote to locate.",
        )
        return
    size = limit or TEXT_WINDOW_CODEPOINT_LIMIT
    located_start = start
    scanned = 0
    if located_start is None:
        anchor = (quoted[0].get("selector") or {}).get("char_start", 0)
        anchor = max(0, int(anchor) - 500) if type(anchor) is int else 0
        positions = [min(anchor, meta.total_characters)]
        deadline = time.monotonic() + 2.0
        offset = 0
        while positions or offset < meta.total_characters:
            position = positions.pop() if positions else offset
            offset = (
                offset
                if position != offset
                else offset + TEXT_WINDOW_CODEPOINT_LIMIT - 1024
            )
            text = read_current_text(
                db,
                **coordinate,
                metadata=meta,
                start=position,
                limit=TEXT_WINDOW_CODEPOINT_LIMIT,
            )
            scanned += len(text)
            matches = next(
                (
                    found
                    for span in quoted
                    if (found := quote_ranges(text, span["quote"]))
                ),
                None,
            )
            if matches:
                located_start = max(0, position + matches[0][0] - min(500, size // 2))
                break
            if scanned >= 1_048_576 or time.monotonic() >= deadline:
                break
        if located_start is None:
            artifact.update(
                recovery_status="incomplete",
                recovery_scanned_characters=scanned,
                recovery_notice="No saved quote was located in the bounded search. Other sections or spacing changes may contain it.",
            )
            return
    passage, _stale = read_artifact_passage(
        db,
        artifact,
        start=located_start,
        limit=size,
        revision=revision if revision is not None else meta.retrieval_revision,
        ignore_hash=True,
    )
    if passage is None:
        artifact.update(
            recovery_status="unsupported",
            recovery_notice="This citation cannot be located in the current source.",
        )
        return
    text = passage["text"]
    base = passage["window"]["start"]
    ranges = []
    for span in quoted:
        for left, right in quote_ranges(text, span["quote"])[:100]:
            ranges.append(
                {
                    "span_id": span["stable_id"],
                    "start": _utf16_offset(text, left),
                    "end": _utf16_offset(text, right),
                    "source_start": base + left,
                    "source_end": base + right,
                }
            )
    artifact["text_context"] = {
        **passage,
        "offset_unit": "utf16_code_unit",
        "ranges": ranges,
        "evidence_link_id": viewer["link"]["stable_id"],
        "artifact_stable_id": artifact["stable_id"],
        "recovered": True,
    }
    artifact.update(
        recovery_status="located",
        recovery_scanned_characters=scanned,
        recovery_notice="Showing a bounded section of the current source. Other matches may exist.",
    )


__all__ = ["locate_current_evidence_text"]
