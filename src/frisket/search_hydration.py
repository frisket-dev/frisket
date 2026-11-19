"""Bounded authoritative text hydration for the contentless search index."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


MAX_SNIPPET_CANDIDATES = 1_000
MAX_SNIPPET_CHARACTERS = 512
_SNIPPET_TABLE = "temp.frisket_search_snippets"


def searchable_text(value: Any) -> str | None:
    """Return the exact logical string indexed for a current cell value."""

    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def source_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bounded_snippet(snippet: str, *, start_marker: str, end_marker: str) -> str:
    """Bound display context while keeping generated highlights balanced.

    Input markers must not occur in source text. Full-source match locations
    are derived before this display-only crop. Source remains plain text for
    consumers that render text nodes; only supplied markers become bold tags.
    """
    first = snippet.find(start_marker)
    left = max(0, first - MAX_SNIPPET_CHARACTERS // 4) if first >= 0 else 0
    if len(snippet) <= MAX_SNIPPET_CHARACTERS:
        left = 0
    right = min(len(snippet), left + MAX_SNIPPET_CHARACTERS)
    parts = ["…"] if left else []
    cursor = left
    highlighted = False
    while cursor < right:
        start = snippet.find(start_marker, cursor)
        end = snippet.find(end_marker, cursor)
        marker = min(
            (index for index in (start, end) if index >= 0), default=len(snippet)
        )
        if marker >= right:
            parts.append(snippet[cursor:right])
            break
        parts.append(snippet[cursor:marker])
        highlighted = marker == start
        parts.append("<b>" if highlighted else "</b>")
        cursor = marker + len(start_marker if highlighted else end_marker)
    if highlighted:
        parts.append("</b>")
    if right < len(snippet):
        parts.append("…")
    return "".join(parts)


def native_snippets(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    start_marker: str = "<b>",
    end_marker: str = "</b>",
    include_rerank: bool = True,
) -> dict[int, tuple[str, str | None]]:
    """Use SQLite's tokenizer/snippet implementation on a bounded text set."""

    if len(candidates) > MAX_SNIPPET_CANDIDATES:
        raise ValueError(
            f"snippet hydration is bounded to {MAX_SNIPPET_CANDIDATES} candidates"
        )
    if not candidates:
        return {}
    # The source reader may be a pinned query-only connection. Tokenization is
    # disposable work over already-hydrated text, never part of project storage.
    db = sqlite3.connect(":memory:")
    try:
        db.execute(f"CREATE VIRTUAL TABLE {_SNIPPET_TABLE} USING fts5(content)")
        db.executemany(
            f"INSERT INTO {_SNIPPET_TABLE}(rowid,content) VALUES (?,?)",
            [
                (int(candidate["index_id"]), str(candidate["content"]))
                for candidate in candidates
            ],
        )
        rerank_sql = (
            ",snippet(frisket_search_snippets,0,'','','…',64)"
            if include_rerank
            else ",NULL"
        )
        rows = db.execute(
            "SELECT rowid,snippet(frisket_search_snippets,0,?,?, '…',12)"
            + rerank_sql
            + " FROM frisket_search_snippets "
            "WHERE frisket_search_snippets MATCH ?",
            (start_marker, end_marker, query),
        ).fetchall()
        return {
            int(row[0]): (str(row[1]), None if row[2] is None else str(row[2]))
            for row in rows
            if row[1] is not None
        }
    finally:
        db.close()
