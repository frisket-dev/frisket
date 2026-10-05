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
import threading
import uuid
from collections.abc import Callable
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.project import ProjectReadSnapshot
from frisket.search_hydration import hydrate_candidates, native_snippets
from frisket.search_storage import configure_search_connection, ensure_search_schema
from frisket.search_index import (
    SearchIndexNotReady,
    drain_index,  # noqa: F401 -- public explicit maintenance entry point
    index_batch,  # noqa: F401 -- public background maintenance entry point
    index_is_complete,
    index_needs_work,  # noqa: F401 -- public scheduling hint
)

# ~80MB onnx cross-encoder, downloads on first use into the SAME fastembed
# cache as the semantic embedder (fastembed define_cache_dir: FASTEMBED_CACHE_PATH
# or $TMPDIR/fastembed_cache) — Dockerfile pre-bakes it like LOCAL_MODEL.
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
RERANK_POOL = 50  # second stage runs over the top-50 first-stage candidates
RERANK_MIN_SPREAD = 1.0  # logits; flatter than this = uninformative, keep stage-1
FTS_INDEX_CONTENT_VERSION = "5"
_rerank_model: Any = None  # lazy fastembed TextCrossEncoder singleton

Scorer = Callable[[str, list[str]], list[float]]
SearchProject = Project | ProjectReadSnapshot


def _snapshot_for(project: SearchProject) -> tuple[ProjectReadSnapshot, bool]:
    if isinstance(project, ProjectReadSnapshot):
        return project, False
    return project.read_snapshot(), True


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


def _sidecar(project: SearchProject) -> sqlite3.Connection:
    db = sqlite3.connect(project.path / "project.search.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    configure_search_connection(db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    ensure_search_schema(db)
    return db


def _read_sidecar(project: SearchProject) -> sqlite3.Connection:
    """Open a pinned existing index without foreground DDL or writer waits."""
    path = project.path / "project.search.db"
    try:
        db = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=0,
            check_same_thread=False,
        )
    except sqlite3.OperationalError as exc:
        raise SearchIndexNotReady() from exc
    db.row_factory = sqlite3.Row
    configure_search_connection(db)
    try:
        db.execute("BEGIN")
        # Pin and validate this read transaction even when the caller intends
        # to serve an explicitly incomplete set of results.
        if fts_index_content_version(db) != FTS_INDEX_CONTENT_VERSION:
            raise SearchIndexNotReady()
        return db
    except (sqlite3.OperationalError, SearchIndexNotReady) as exc:
        db.close()
        raise SearchIndexNotReady() from exc


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("search was stopped")


def _cancel_progress(cancel_event: threading.Event | None) -> Callable[[], int] | None:
    if cancel_event is None:
        return None
    return lambda: int(cancel_event.is_set())


def rebuild_index(
    project: SearchProject, *, cancel_event: threading.Event | None = None
) -> int:
    """Explicitly refresh the complete latest index through bounded maintenance."""
    if not isinstance(project, Project):
        raise SearchIndexNotReady()
    if project.db.in_transaction:
        raise RuntimeError("index maintenance requires its own source transaction")
    from frisket.engine.store.search_index_work import enqueue_dirty_scope

    _raise_if_cancelled(cancel_event)
    project.db.execute("BEGIN IMMEDIATE")
    try:
        enqueue_dirty_scope(project.db)
        project.db.commit()
    except BaseException:
        project.db.rollback()
        raise
    return drain_index(project, cancel_event=cancel_event)


def fts_indexed_at_op(db: sqlite3.Connection) -> int | None:
    """The source op cursor at the last complete index publication."""
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
    project: SearchProject, *, cancel_event: threading.Event | None = None
) -> sqlite3.Connection:
    """Pin a complete matching index; readers never run index maintenance."""
    _raise_if_cancelled(cancel_event)
    db = _read_sidecar(project)
    try:
        if not index_is_complete(db, project):
            raise SearchIndexNotReady()
        _raise_if_cancelled(cancel_event)
        return db
    except BaseException:
        db.close()
        raise


def column_ai_flags(project: SearchProject) -> dict[int, bool]:
    """Current AI-generated flag by column id for search result display."""
    return {
        int(column["id"]): bool(column["ai_generated"])
        for sheet in project.sheets()
        for column in project.columns(sheet["id"])
    }


_SEARCH_SQL = (
    "SELECT cell_fts.rowid AS index_id, sc.sheet_id, sc.row_id, sc.column_id, "
    "sc.column_name,sc.source_hash "
    "FROM cell_fts "
    "JOIN search_cells AS sc ON sc.id=cell_fts.rowid "
    "WHERE cell_fts MATCH ? ORDER BY rank LIMIT ?"
)


def _ranked_candidates(
    db: sqlite3.Connection, query: str, limit: int
) -> tuple[list[sqlite3.Row], str]:
    try:
        return db.execute(_SEARCH_SQL, (query, limit)).fetchall(), query
    except sqlite3.OperationalError:
        quoted = f'"{query}"'
        return db.execute(_SEARCH_SQL, (quoted, limit)).fetchall(), quoted


def _snippet_hits(
    db: sqlite3.Connection,
    snapshot: ProjectReadSnapshot,
    rows: list[sqlite3.Row],
    query: str,
    *,
    pool: int,
    rerank: bool,
    start_marker: str = "<b>",
    end_marker: str = "</b>",
) -> tuple[list[dict[str, Any]], list[str]]:
    hydrated = hydrate_candidates(snapshot, rows)[:pool]
    snippets = native_snippets(
        db,
        query,
        hydrated,
        start_marker=start_marker,
        end_marker=end_marker,
        include_rerank=rerank,
    )
    hits: list[dict[str, Any]] = []
    texts: list[str] = []
    for candidate in hydrated:
        rendered = snippets.get(int(candidate["index_id"]))
        if rendered is None:
            continue
        snip, rerank_text = rendered
        hits.append(
            {
                "sheet_id": int(candidate["sheet_id"]),
                "row_id": int(candidate["row_id"]),
                "column_id": int(candidate["column_id"]),
                "column_name": candidate["column_name"],
                "snip": snip,
                "ai_generated": bool(candidate["ai_generated"]),
            }
        )
        if rerank:
            texts.append(rerank_text or "")
    return hits, texts


def search_project_page(
    project: Project, query: str, limit: int = 50, rerank: str = "auto"
) -> dict[str, Any]:
    """Bounded interactive results; discard stale cells before exposing snippets."""
    if not 1 <= limit <= 500:
        raise ValueError("search limit must be between 1 and 500")
    try:
        db = _read_sidecar(project)
    except SearchIndexNotReady:
        return {"hits": [], "complete": False}
    snapshot = project.read_snapshot()
    try:
        complete = index_is_complete(db, snapshot)
        pool = max(limit, RERANK_POOL) if rerank != "off" else limit
        # A bounded overfetch tolerates recently changed candidates. Incomplete
        # coverage is explicit; never scan the whole ranked result set to fill a page.
        candidate_limit = pool if complete else min(1000, pool * 4)
        rows, effective_query = _ranked_candidates(db, query, candidate_limit)
        hits, texts = _snippet_hits(
            db,
            snapshot,
            rows,
            effective_query,
            pool=pool,
            rerank=rerank != "off",
        )
        if rerank != "off":
            hits = rerank_hits(query, hits, texts)
        return {"hits": hits[:limit], "complete": complete}
    finally:
        snapshot.close()
        db.close()


def search_project(
    project: SearchProject, query: str, limit: int = 50, rerank: str = "auto"
) -> list[dict[str, Any]]:
    """FTS keyword search, cross-encoder second stage on top (``rerank``:
    "auto"/"on" = rerank when a backend resolves, silently fall back when not;
    "off" = first-stage order, no model touched)."""
    snapshot, owns_snapshot = _snapshot_for(project)
    db = None
    try:
        db = fresh_sidecar(snapshot)
        # Widen the first stage to the rerank pool; the unreranked path slices
        # back to ``limit`` in FTS order, identical to the prior contract.
        pool = limit if rerank == "off" else max(limit, RERANK_POOL)
        rows, effective_query = _ranked_candidates(db, query, pool)
        out, texts = _snippet_hits(
            db,
            snapshot,
            rows,
            effective_query,
            pool=pool,
            rerank=rerank != "off",
        )
        if rerank != "off":
            out = rerank_hits(query, out, texts)
        return out[:limit]
    finally:
        if db is not None:
            db.close()
        if owns_snapshot:
            snapshot.close()


_SHEET_SEARCH_SQL = (
    "SELECT sc.row_id FROM cell_fts "
    "JOIN search_cells AS sc ON sc.id=cell_fts.rowid "
    "WHERE cell_fts MATCH ? AND sc.sheet_id=? ORDER BY rank LIMIT ?"
)


def search_sheet(
    project: SearchProject, sheet_id: int, query: str, limit: int = 50
) -> list[int]:
    """Sheet-scoped FTS/BM25 keyword ranking -> ordered, de-duplicated row_ids (Stage 7
    Lane H, the keyword half of hybrid search). Requires the matching complete
    project ``cell_fts`` index and retains bad-syntax quote-retry. A row matching in
    multiple cells appears ONCE, at its best (first) rank. NEVER returns another sheet's
    rows (the ``AND sheet_id=?`` scope)."""
    snapshot, owns_snapshot = _snapshot_for(project)
    try:
        db = fresh_sidecar(snapshot)
        try:
            # A row can match in several cells, so over-fetch before de-duplication.
            pool = max(int(limit) * 4, 200)
            try:
                rows = db.execute(_SHEET_SEARCH_SQL, (query, sheet_id, pool)).fetchall()
            except sqlite3.OperationalError:
                rows = db.execute(
                    _SHEET_SEARCH_SQL, (f'"{query}"', sheet_id, pool)
                ).fetchall()
        finally:
            db.close()
    finally:
        if owns_snapshot:
            snapshot.close()
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
    project: SearchProject,
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
    snapshot, owns_snapshot = _snapshot_for(project)
    db = None
    try:
        db = fresh_sidecar(snapshot, cancel_event=cancel_event)
    except BaseException:
        if owns_snapshot:
            snapshot.close()
        raise
    indexed_at_op = fts_indexed_at_op(db)
    indexed_revision = int(
        db.execute(
            "SELECT value FROM fts_state WHERE key='complete_revision'"
        ).fetchone()[0]
    )
    marker = uuid.uuid4().hex
    anchor_start = f"__frisket_fts_{marker}_start__"
    anchor_end = f"__frisket_fts_{marker}_end__"
    authorized: list[str] = []
    params: list[Any] = [query, sheet_id]
    if row_ids:
        authorized.append("sc.row_id IN (" + ",".join("?" for _ in row_ids) + ")")
        params.extend(row_ids)
    if cells:
        exact = []
        for row_id, column_id in sorted(cells):
            exact.append("(sc.row_id=? AND sc.column_id=?)")
            params.extend((row_id, column_id))
        authorized.append("(" + " OR ".join(exact) + ")")
    scope = "" if not authorized else " AND (" + " OR ".join(authorized) + ")"
    sql = (
        "SELECT cell_fts.rowid AS index_id,sc.sheet_id,sc.row_id,sc.column_id,"
        "sc.column_name,sc.source_hash "
        "FROM cell_fts "
        "JOIN search_cells AS sc ON sc.id=cell_fts.rowid "
        "WHERE cell_fts MATCH ? AND sc.sheet_id=?" + scope + " ORDER BY rank LIMIT ?"
    )
    progress = _cancel_progress(cancel_event)
    if progress is not None:
        db.set_progress_handler(progress, 1_000)
    try:
        try:
            rows = db.execute(sql, [*params, limit]).fetchall()
            effective_query = query
        except sqlite3.OperationalError:
            _raise_if_cancelled(cancel_event)
            effective_query = f'"{query}"'
            rows = db.execute(
                sql,
                [effective_query, *params[1:], limit],
            ).fetchall()
        hydrated_hits, _texts = _snippet_hits(
            db,
            snapshot,
            rows,
            effective_query,
            pool=limit,
            rerank=False,
            start_marker=anchor_start,
            end_marker=anchor_end,
        )
    except sqlite3.OperationalError:
        _raise_if_cancelled(cancel_event)
        raise
    finally:
        db.close()
        if owns_snapshot:
            snapshot.close()
    hits = []
    for hit in hydrated_hits:
        hit.pop("ai_generated", None)
        snippet = str(hit["snip"])
        _, found, remainder = snippet.partition(anchor_start)
        anchor, closed, _ = remainder.partition(anchor_end)
        hit["snip"] = snippet.replace(anchor_start, "<b>").replace(anchor_end, "</b>")
        if found and closed and anchor:
            hit["fts_anchor"] = anchor
        hits.append(
            {
                **hit,
                "_indexed_at_op": indexed_at_op,
                "_indexed_revision": indexed_revision,
            }
        )
    return hits


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
