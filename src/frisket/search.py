"""Project-wide keyword search: SQLite FTS5 over all live text values.
The index lives in the rebuildable search sidecar.
Semantic ranking is the next layer (frisket.semantic); this module also owns
the SECOND-stage reranker both layers share: an ONNX cross-encoder over the
top-50 first-stage candidates.

Reranker contract: pure REORDER of the candidate set — same hits in, same
hits out (each annotated ``rerank_score``) — and on ANY failure (extra not
installed, model unfetchable, scorer error) the first-stage order returns
untouched. Never a 500. ``rerank="off"`` (search param) or
FRISKET_DISABLE_RERANK=1 (ops) skip the stage entirely.

The cross-encoder is English-only (ms-marco) while the semantic embedder is
deliberately multilingual (see ``frisket.semantic``). Measured on the test
fixture: out-of-language queries score near-flat (spread
0.02–0.16 logits, and the tiny preferences are WRONG — 'el coche está
averiado' put the garden row above the automobile row) while in-language
queries spread 4.7–6.1. So an uninformative (near-flat) score profile keeps
first-stage order instead of reordering on noise — RERANK_MIN_SPREAD below."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from typing import Any

from frisket.engine.store import Project

# ~80MB onnx cross-encoder, downloads on first use into the SAME fastembed
# cache as the semantic embedder (fastembed define_cache_dir: FASTEMBED_CACHE_PATH
# or $TMPDIR/fastembed_cache) — Dockerfile pre-bakes it like LOCAL_MODEL.
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANK_POOL = 50  # second stage runs over the top-50 first-stage candidates
RERANK_MIN_SPREAD = 1.0  # logits; flatter than this = uninformative, keep stage-1
FTS_INDEX_CONTENT_VERSION = "2"
_rerank_model: Any = None  # lazy fastembed TextCrossEncoder singleton

Scorer = Callable[[str, list[str]], list[float]]


def local_reranker() -> Scorer | None:
    """Cross-encoder scoring backend, if the installed fastembed exposes one
    (fastembed>=0.7 ships TextCrossEncoder in the base package — the existing
    ``semantic`` extra suffices, no new dependency). Returns
    ``score(query, docs) -> per-doc relevance`` or None. Set
    FRISKET_DISABLE_RERANK=1 to force first-stage order. The broader
    FRISKET_DISABLE_LOCAL_EMBED=1 switch also disables this FastEmbed model so
    lexical fallback cannot initialize a second local model behind the disabled
    embedding path."""
    if (
        os.environ.get("FRISKET_DISABLE_RERANK") == "1"
        or os.environ.get("FRISKET_DISABLE_LOCAL_EMBED") == "1"
    ):
        return None
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
    except ImportError:
        return None

    def score(query: str, docs: list[str]) -> list[float]:
        global _rerank_model
        if _rerank_model is None:
            _rerank_model = TextCrossEncoder(RERANK_MODEL)
        return [float(s) for s in _rerank_model.rerank(query, docs)]

    return score


def rerank_hits(
    query: str, hits: list[dict[str, Any]], texts: list[str]
) -> list[dict[str, Any]]:
    """Second-stage reorder of ``hits`` by cross-encoder relevance to
    ``query`` (``texts`` holds the cell content for each hit, same order).
    Reorders only — never adds, drops, or raises; every fallback path returns
    ``hits`` exactly as given."""
    if len(hits) < 2:
        return hits
    scorer = local_reranker()
    if scorer is None:
        return hits
    try:
        scores = scorer(query, [t[:1024] for t in texts])
    except Exception:
        return hits  # unreranked results beat a 500, always
    if len(scores) != len(hits):
        return hits
    for h, s in zip(hits, scores, strict=False):
        h["rerank_score"] = round(s, 4)
    if max(scores) - min(scores) < RERANK_MIN_SPREAD:
        # near-flat profile: the (English-only) cross-encoder cannot tell the
        # candidates apart — typical for non-English queries. Trust the
        # first stage.
        return hits
    order = sorted(range(len(hits)), key=lambda i: scores[i], reverse=True)
    return [hits[i] for i in order]


SIDECAR_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS cell_fts USING fts5(
  content, sheet_id UNINDEXED, row_id UNINDEXED, column_id UNINDEXED,
  column_name UNINDEXED
);
CREATE TABLE IF NOT EXISTS fts_state (key TEXT PRIMARY KEY, value TEXT);
-- semantic-search vector cache: key = sha1(model_id + content), vec = packed
-- float32. Content-addressed, so it never goes stale (edits make new keys) and
-- survives FTS rebuilds; the sidecar stays rebuildable by contract.
CREATE TABLE IF NOT EXISTS cell_vec (key TEXT PRIMARY KEY, vec BLOB NOT NULL);
"""


def _sidecar(project: Project) -> sqlite3.Connection:
    db = sqlite3.connect(project.path / "project.search.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SIDECAR_SCHEMA)
    return db


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("search was stopped")


def _cancel_progress(cancel_event: threading.Event | None) -> Callable[[], int] | None:
    if cancel_event is None:
        return None
    return lambda: int(cancel_event.is_set())


def rebuild_index(
    project: Project, *, cancel_event: threading.Event | None = None
) -> int:
    """Publish a complete-cell index and watermark from one read snapshot.

    Vector entries are content-addressed; rebuilding keyword search leaves them
    untouched. A failed rebuild retains the previous committed index.
    """
    db = _sidecar(project)
    try:
        _raise_if_cancelled(cancel_event)
        progress = _cancel_progress(cancel_event)
        if progress is not None:
            db.set_progress_handler(progress, 1_000)
        with closing(project.read_snapshot()) as snapshot:
            if progress is not None:
                snapshot.db.set_progress_handler(progress, 1_000)
            indexed_at_op = snapshot.op_cursor
            db.execute("BEGIN")
            db.execute("DELETE FROM cell_fts")
            n = 0
            for sheet in snapshot.sheets():
                if "(undone:" in sheet["name"]:
                    continue
                for column in snapshot.columns(sheet["id"]):
                    if column["type"] not in ("text", "category", "json", "link"):
                        continue

                    def rows() -> Iterator[tuple[str, int, int, int, str]]:
                        nonlocal n
                        source_rows = snapshot.db.execute(
                            "SELECT r.id, c.value, COALESCE(c.validity, 'missing') AS validity "
                            "FROM rows r LEFT JOIN current_cells c "
                            "ON c.column_id=? AND c.row_id=r.id "
                            "WHERE r.sheet_id=? AND r.hidden=0",
                            (column["id"], sheet["id"]),
                        )
                        for row in source_rows:
                            _raise_if_cancelled(cancel_event)
                            stored = None if row["validity"] == "invalid" else row["value"]
                            value = None if stored is None else json.loads(stored)
                            if value is None:
                                continue
                            text = str(value)
                            if not text.strip():
                                continue
                            n += 1
                            yield (
                                text,
                                sheet["id"],
                                int(row["id"]),
                                column["id"],
                                column["name"],
                            )

                    db.executemany(
                        "INSERT INTO cell_fts (content, sheet_id, row_id, column_id, "
                        "column_name) VALUES (?,?,?,?,?)",
                        rows(),
                    )
            for key, value in (
                ("indexed_at_op", str(indexed_at_op)),
                ("index_content_version", FTS_INDEX_CONTENT_VERSION),
            ):
                db.execute(
                    "INSERT INTO fts_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            db.commit()
            return n
    except sqlite3.OperationalError:
        db.rollback()
        _raise_if_cancelled(cancel_event)
        raise
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def fts_indexed_at_op(db: sqlite3.Connection) -> int | None:
    """The op-cursor watermark rebuild_index stamps into fts_state.

    One accessor for the copied ``SELECT value FROM fts_state`` reads (the
    writer stays inline in rebuild_index above — it is the only stamper)."""
    state = db.execute(
        "SELECT value FROM fts_state WHERE key='indexed_at_op'"
    ).fetchone()
    return int(state["value"]) if state is not None else None


def fts_index_content_version(db: sqlite3.Connection) -> str | None:
    """Version of the cell content format stored in this FTS sidecar."""
    state = db.execute(
        "SELECT value FROM fts_state WHERE key='index_content_version'"
    ).fetchone()
    return str(state["value"]) if state is not None else None


def fresh_sidecar(
    project: Project, *, cancel_event: threading.Event | None = None
) -> sqlite3.Connection:
    """An FTS sidecar connection whose index is current for project.op_cursor.

    The lazy pull-style staleness check (watermark != op_cursor -> rebuild)
    used to be copy-pasted at every reader; it lives only here now."""
    _raise_if_cancelled(cancel_event)
    db = _sidecar(project)
    if (
        fts_indexed_at_op(db) != project.op_cursor
        or fts_index_content_version(db) != FTS_INDEX_CONTENT_VERSION
    ):
        db.close()
        rebuild_index(project, cancel_event=cancel_event)
        db = _sidecar(project)
    _raise_if_cancelled(cancel_event)
    return db


def column_ai_flags(project: Project) -> dict[int, bool]:
    """Current AI-generated flag by column id for search result display."""
    return {
        int(c["id"]): bool(c["ai_generated"])
        for sheet in project.sheets()
        for c in project.columns(sheet["id"])
    }


_SEARCH_SQL = (
    "SELECT sheet_id, row_id, column_id, column_name, "
    "snippet(cell_fts, 0, '<b>', '</b>', '…', 12) AS snip, "
    "snippet(cell_fts, 0, '', '', '…', 64) AS rerank_text "
    "FROM cell_fts WHERE cell_fts MATCH ? ORDER BY rank LIMIT ?"
)


def search_project(
    project: Project, query: str, limit: int = 50, rerank: str = "auto"
) -> list[dict[str, Any]]:
    """FTS keyword search, cross-encoder second stage on top (``rerank``:
    "auto"/"on" = rerank when a backend resolves, silently fall back when not;
    "off" = first-stage order, no model touched)."""
    db = fresh_sidecar(project)
    # widen the first stage to the rerank pool; the unreranked path slices
    # back to ``limit`` in FTS order, identical to the pre-rerank contract
    pool = limit if rerank == "off" else max(limit, RERANK_POOL)
    try:
        rows = db.execute(_SEARCH_SQL, (query, pool)).fetchall()
    except sqlite3.OperationalError:
        # bad FTS syntax from user input — quote it and retry
        rows = db.execute(_SEARCH_SQL, (f'"{query}"', pool)).fetchall()
    out = [dict(r) for r in rows]
    db.close()
    # A match-centred FTS excerpt is bounded to the FTS5 maximum (64 tokens),
    # avoiding a full-cell Python copy while still letting a late match compete.
    texts = [h.pop("rerank_text") for h in out]
    ai_by_column = column_ai_flags(project)
    for h in out:
        h["ai_generated"] = ai_by_column.get(int(h["column_id"]), False)
    if rerank != "off":
        out = rerank_hits(query, out, texts)
    return out[:limit]


_SHEET_SEARCH_SQL = (
    "SELECT row_id FROM cell_fts WHERE cell_fts MATCH ? AND sheet_id=? ORDER BY rank "
    "LIMIT ?"
)


def search_sheet(
    project: Project, sheet_id: int, query: str, limit: int = 50
) -> list[int]:
    """Sheet-scoped FTS/BM25 keyword ranking -> ordered, de-duplicated row_ids (Stage 7
    Lane H, the keyword half of hybrid search). Reuses the project ``cell_fts`` index +
    its op-cursor staleness rebuild + the bad-syntax quote-retry. A row matching in
    multiple cells appears ONCE, at its best (first) rank. NEVER returns another sheet's
    rows (the ``AND sheet_id=?`` scope)."""
    db = fresh_sidecar(project)
    # over-fetch (a row can match in several cells) then de-dup down to ``limit``
    pool = max(int(limit) * 4, 200)
    try:
        rows = db.execute(_SHEET_SEARCH_SQL, (query, sheet_id, pool)).fetchall()
    except sqlite3.OperationalError:
        rows = db.execute(_SHEET_SEARCH_SQL, (f'"{query}"', sheet_id, pool)).fetchall()
    db.close()
    out: list[int] = []
    seen: set[int] = set()
    for r in rows:
        rid = int(r["row_id"])
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
        if len(out) >= int(limit):
            break
    return out


def search_cells_scoped(
    project: Project,
    sheet_id: int,
    query: str,
    row_ids: list[int] | None,
    cells: set[tuple[int, int]] | None = None,
    limit: int = 50,
    *,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Lexically rank authorized rows/cells before applying ``limit``."""

    _raise_if_cancelled(cancel_event)
    if row_ids is not None and not row_ids and not cells:
        return []
    db = fresh_sidecar(project, cancel_event=cancel_event)
    indexed_at_op = fts_indexed_at_op(db)
    authorized: list[str] = []
    params: list[Any] = [query, sheet_id]
    if row_ids:
        authorized.append("row_id IN (" + ",".join("?" for _ in row_ids) + ")")
        params.extend(row_ids)
    if cells:
        exact = []
        for row_id, column_id in sorted(cells):
            exact.append("(row_id=? AND column_id=?)")
            params.extend((row_id, column_id))
        authorized.append("(" + " OR ".join(exact) + ")")
    scope = "" if not authorized else " AND (" + " OR ".join(authorized) + ")"
    sql = (
        "SELECT sheet_id,row_id,column_id,column_name,"
        "snippet(cell_fts, 0, '<b>', '</b>', '…', 12) AS snip "
        "FROM cell_fts WHERE cell_fts MATCH ? AND sheet_id=?"
        + scope
        + " ORDER BY rank LIMIT ?"
    )
    progress = _cancel_progress(cancel_event)
    if progress is not None:
        db.set_progress_handler(progress, 1_000)
    try:
        try:
            rows = db.execute(sql, [*params, limit]).fetchall()
        except sqlite3.OperationalError:
            _raise_if_cancelled(cancel_event)
            rows = db.execute(sql, [f'"{query}"', *params[1:], limit]).fetchall()
    except sqlite3.OperationalError:
        _raise_if_cancelled(cancel_event)
        raise
    finally:
        db.close()
    return [{**dict(row), "_indexed_at_op": indexed_at_op} for row in rows]


def rrf_fuse(ranked_lists: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion of ranked id lists: ``score(id) = Σ 1/(k + rank)`` with a
    1-based rank; an id missing from a list contributes 0. Rank-based (NOT raw scores)
    so the incomparable BM25 (unbounded) and cosine (−1..1) scales fuse cleanly. Returns
    ``[(id, score)]`` sorted by score DESC, then id ASC (deterministic)."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank0, rid in enumerate(ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank0 + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
