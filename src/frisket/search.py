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

import os
import sqlite3
from collections.abc import Callable
from typing import Any

from frisket.engine.store import Project

# ~80MB onnx cross-encoder, downloads on first use into the SAME fastembed
# cache as the semantic embedder (fastembed define_cache_dir: FASTEMBED_CACHE_PATH
# or $TMPDIR/fastembed_cache) — Dockerfile pre-bakes it like LOCAL_MODEL.
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANK_POOL = 50  # second stage runs over the top-50 first-stage candidates
RERANK_MIN_SPREAD = 1.0  # logits; flatter than this = uninformative, keep stage-1
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


def rebuild_index(project: Project) -> int:
    """Full rebuild — the sidecar is rebuildable by contract."""
    db = _sidecar(project)
    db.execute("DELETE FROM cell_fts")
    n = 0
    for s in project.sheets():
        if "(undone:" in s["name"]:
            continue
        for c in project.columns(s["id"]):
            if c["type"] not in ("text", "category", "json", "link"):
                continue
            vals = project.get_values(s["id"], c["id"])
            rows = [
                (str(v)[:50000], s["id"], rid, c["id"], c["name"])
                for rid, v in vals.items()
                if v is not None and str(v).strip()
            ]
            db.executemany(
                "INSERT INTO cell_fts (content, sheet_id, row_id, column_id, "
                "column_name) VALUES (?,?,?,?,?)",
                rows,
            )
            n += len(rows)
    db.execute(
        "INSERT INTO fts_state (key, value) VALUES ('indexed_at_op', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(project.op_cursor),),
    )
    db.commit()
    db.close()
    return n


def fts_indexed_at_op(db: sqlite3.Connection) -> int | None:
    """The op-cursor watermark rebuild_index stamps into fts_state.

    One accessor for the copied ``SELECT value FROM fts_state`` reads (the
    writer stays inline in rebuild_index above — it is the only stamper)."""
    state = db.execute(
        "SELECT value FROM fts_state WHERE key='indexed_at_op'"
    ).fetchone()
    return int(state["value"]) if state is not None else None


def fresh_sidecar(project: Project) -> sqlite3.Connection:
    """An FTS sidecar connection whose index is current for project.op_cursor.

    The lazy pull-style staleness check (watermark != op_cursor -> rebuild)
    used to be copy-pasted at every reader; it lives only here now."""
    db = _sidecar(project)
    if fts_indexed_at_op(db) != project.op_cursor:
        db.close()
        rebuild_index(project)
        db = _sidecar(project)
    return db


def column_ai_flags(project: Project) -> dict[int, bool]:
    """Current AI-generated flag by column id for search result display."""
    return {
        int(c["id"]): bool(c["ai_generated"])
        for sheet in project.sheets()
        for c in project.columns(sheet["id"])
    }


_SEARCH_SQL = (
    "SELECT sheet_id, row_id, column_id, column_name, content, "
    "snippet(cell_fts, 0, '<b>', '</b>', '…', 12) AS snip "
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
    # full cell content feeds the cross-encoder but stays out of the response
    texts = [h.pop("content") for h in out]
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
