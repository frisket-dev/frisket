"""Bounded authoritative text hydration for the contentless search index."""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from frisket.engine.store.project import ProjectReadSnapshot


MAX_SNIPPET_CANDIDATES = 1_000
_SNIPPET_TABLE = "temp.frisket_search_snippets"


def searchable_text(value: Any) -> str | None:
    """Return the exact logical string indexed for a current cell value."""

    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def source_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hydrate_candidates(
    snapshot: ProjectReadSnapshot,
    candidates: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Hydrate ranked identities from one pinned authoritative snapshot.

    Missing, renamed, hidden, invalid, or hash-mismatched candidates are
    discarded. The returned order is the candidate order supplied by FTS.
    """

    pending = [dict(candidate) for candidate in candidates]
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for candidate in pending:
        grouped[(int(candidate["sheet_id"]), int(candidate["column_id"]))].append(
            candidate
        )

    hydrated_by_id: dict[int, dict[str, Any]] = {}
    for (sheet_id, column_id), group in grouped.items():
        descriptor = snapshot.db.execute(
            "SELECT c.name,c.ai_generated,c.type "
            "FROM columns c JOIN sheets s ON s.id=c.sheet_id "
            "WHERE c.id=? AND c.sheet_id=? AND c.hidden=0 AND s.hidden=0 "
            "AND c.type IN ('text','category','json','link')",
            (column_id, sheet_id),
        ).fetchone()
        if descriptor is None:
            continue
        row_ids = [int(candidate["row_id"]) for candidate in group]
        values = snapshot.get_values(sheet_id, column_id, row_ids=row_ids)
        for candidate in group:
            row_id = int(candidate["row_id"])
            text = searchable_text(values.get(row_id))
            if (
                text is None
                or descriptor["name"] != candidate["column_name"]
                or source_hash(text) != candidate["source_hash"]
            ):
                continue
            hydrated = dict(candidate)
            hydrated["content"] = text
            hydrated["ai_generated"] = bool(descriptor["ai_generated"])
            hydrated_by_id[int(candidate["index_id"])] = hydrated

    return [
        hydrated_by_id[int(candidate["index_id"])]
        for candidate in pending
        if int(candidate["index_id"]) in hydrated_by_id
    ]


def native_snippets(
    db: sqlite3.Connection,
    query: str,
    candidates: list[Mapping[str, Any]],
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
    db.execute(f"DROP TABLE IF EXISTS {_SNIPPET_TABLE}")
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
        db.execute(f"DROP TABLE IF EXISTS {_SNIPPET_TABLE}")
