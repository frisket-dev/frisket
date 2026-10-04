"""Read-only recovery previews for stale text evidence.

The stored citation remains the historical record.  This module only decorates
one freshly-built viewer response after an explicit user request.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from frisket.engine.store.citation_text import resolve_current_native_texts
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
    db: sqlite3.Connection, viewer: dict[str, Any]
) -> dict[str, Any]:
    """Add current-text quote locations to a viewer response without writes.

    All exact and whitespace-equivalent matches are returned.  An artifact can
    therefore contain more than one range for the same saved span.  Media and
    other non-text sources remain on their original saved context and anchors.
    """

    artifacts = viewer.get("artifacts")
    if not isinstance(artifacts, list):
        return viewer
    current_texts = resolve_current_native_texts(db, artifacts)
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_id = artifact.get("id")
        current_text = (
            current_texts.get(artifact_id)
            if isinstance(artifact_id, int) and not isinstance(artifact_id, bool)
            else None
        )
        if not isinstance(current_text, str):
            artifact["recovery_status"] = "unsupported"
            artifact["recovery_notice"] = (
                "This citation cannot be located in the current source."
            )
            continue

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


__all__ = ["locate_current_evidence_text"]
