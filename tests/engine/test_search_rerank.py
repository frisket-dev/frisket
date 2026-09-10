"""Cross-encoder second-stage reranking.

Contract under test (src/frisket/search.py): rerank REORDERS the first-stage
candidate set — never adds, drops, or errors. Plumbing/fallback tests use a
deterministic stub scorer (monkeypatched ``local_reranker``) so they run
offline and fast. The real ms-marco model tests run ONLY when the model is
already in the shared fastembed cache (Docker pre-bakes it; dev boxes get it
on first live use) — never trigger the ~80MB download from the suite.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

import frisket.search as search_mod
from frisket.search import RERANK_MODEL, local_reranker, rerank_hits, search_project
from frisket.semantic import semantic_search
from frisket.engine.store import Project

# ---------------------------------------------------------------- fixtures


def _seed(tmp_path, notes: list[str]) -> Project:
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {"note": p.add_column(sheet, "note")}
    p.add_rows(sheet, [{"note": n} for n in notes], cols)
    return p


# FTS bm25 ranks the term-stuffed decoy first; a relevance reranker must put
# the genuinely-relevant row on top. Both match the query term "battery".
DECOY = "battery battery battery battery sale flyer mentions battery again"
RELEVANT = "the car battery was dead so the engine would not start"


def _stub_scorer(scores_by_text: dict[str, float], spread_filler: float = 0.0):
    """Deterministic scorer: looks up each doc's score by substring match."""

    def scorer(query: str, docs: list[str]) -> list[float]:
        out = []
        for d in docs:
            for frag, s in scores_by_text.items():
                if frag in d:
                    out.append(s)
                    break
            else:
                out.append(spread_filler)
        return out

    return scorer


# ----------------------------------------------------- stubbed plumbing


def test_rerank_reorders_keyword_hits(tmp_path, monkeypatch):
    p = _seed(tmp_path, [DECOY, RELEVANT, "unrelated gardening note"])
    monkeypatch.setattr(
        search_mod,
        "local_reranker",
        lambda: _stub_scorer({"engine": 9.0, "flyer": -9.0}),
    )
    baseline = search_project(p, "battery", rerank="off")
    assert len(baseline) == 2 and "flyer" in baseline[0]["snip"], (
        f"fixture broken: bm25 should rank the stuffed decoy first: {baseline}"
    )
    hits = search_project(p, "battery")
    # pure reorder: same candidate set, relevant row promoted, score attached
    assert len(hits) == 2
    assert "engine" in hits[0]["snip"] and hits[0]["rerank_score"] == 9.0
    assert {h["row_id"] for h in hits} == {h["row_id"] for h in baseline}
    p.close()


def test_rerank_off_param_keeps_first_stage_order(tmp_path, monkeypatch):
    p = _seed(tmp_path, [DECOY, RELEVANT])
    monkeypatch.setattr(
        search_mod, "local_reranker", lambda: _stub_scorer({"engine": 9.0})
    )
    hits = search_project(p, "battery", rerank="off")
    assert "flyer" in hits[0]["snip"]  # untouched bm25 order
    assert all("rerank_score" not in h for h in hits)
    p.close()


def test_rerank_pool_widens_beyond_limit(tmp_path, monkeypatch):
    """limit=1 still reranks over the wider candidate pool, so the relevant
    row outside the FTS top-1 can win — then slices back to the limit."""
    p = _seed(tmp_path, [DECOY, RELEVANT])
    monkeypatch.setattr(
        search_mod, "local_reranker", lambda: _stub_scorer({"engine": 9.0})
    )
    hits = search_project(p, "battery", limit=1)
    assert len(hits) == 1 and "engine" in hits[0]["snip"]
    p.close()


def test_scorer_failure_falls_back_to_first_stage(tmp_path, monkeypatch):
    def broken(query, docs):
        raise RuntimeError("onnx exploded")

    p = _seed(tmp_path, [DECOY, RELEVANT])
    monkeypatch.setattr(search_mod, "local_reranker", lambda: broken)
    hits = search_project(p, "battery")  # must not raise
    assert len(hits) == 2 and "flyer" in hits[0]["snip"]
    assert all("rerank_score" not in h for h in hits)
    p.close()


def test_no_backend_falls_back_unreranked(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "local_reranker", lambda: None)
    p = _seed(tmp_path, [DECOY, RELEVANT])
    hits = search_project(p, "battery")
    assert len(hits) == 2 and "flyer" in hits[0]["snip"]
    p.close()


def test_env_escape_hatch_disables_backend(monkeypatch):
    monkeypatch.setenv("FRISKET_DISABLE_RERANK", "1")
    assert local_reranker() is None


def test_flat_score_profile_keeps_first_stage_order(monkeypatch):
    """Uninformative (near-flat) cross-encoder profile — measured behavior of
    the English-only model on non-English queries — must NOT reorder; the
    multilingual first stage stays authoritative. Scores still annotated."""

    def flat(query: str, docs: list[str]) -> list[float]:
        return [-11.34, -11.36][: len(docs)]

    monkeypatch.setattr(search_mod, "local_reranker", lambda: flat)
    hits = [{"snip": "a"}, {"snip": "b"}]
    out = rerank_hits("el coche está averiado", hits, ["a", "b"])
    assert [h["snip"] for h in out] == ["a", "b"]
    assert out[0]["rerank_score"] == -11.34


def test_semantic_path_reranks(tmp_path, monkeypatch):
    """Cosine first stage + stub cross-encoder second stage: the reranker
    reorders semantic hits without dropping any; rerank=off pins cosine."""

    def embed(texts: list[str]) -> list[list[float]]:
        # query ~ DECOY direction, so cosine ranks the decoy first
        return [
            [1.0, 0.1] if "flyer" in t or "query" in t else [0.1, 1.0] for t in texts
        ]

    p = _seed(tmp_path, [DECOY, RELEVANT])
    monkeypatch.setattr(
        search_mod, "local_reranker", lambda: _stub_scorer({"engine": 9.0})
    )
    cosine_only = semantic_search(p, "query", embed=embed, rerank="off")
    assert "flyer" in cosine_only[0]["snip"]
    hits = semantic_search(p, "query", embed=embed)
    assert "engine" in hits[0]["snip"] and hits[0]["semantic"] is True
    assert hits[0]["rerank_score"] == 9.0
    assert {h["row_id"] for h in hits} == {h["row_id"] for h in cosine_only}
    p.close()


def test_single_hit_never_touches_backend(tmp_path, monkeypatch):
    """<2 candidates: nothing to reorder, the model must not even resolve
    (keeps `frisket doctor` and tiny projects model-free)."""

    def boom():
        raise AssertionError("backend resolved for a single hit")

    monkeypatch.setattr(search_mod, "local_reranker", boom)
    p = _seed(tmp_path, ["only the zebra row mentions zebra"])
    hits = search_project(p, "zebra")
    assert len(hits) == 1
    p.close()


# ----------------------------------------------------- real model (cached)


def _model_cached() -> bool:
    cache = Path(
        os.getenv(
            "FASTEMBED_CACHE_PATH",
            os.path.join(tempfile.gettempdir(), "fastembed_cache"),
        )
    )
    tag = "models--" + RERANK_MODEL.replace("/", "--")
    return cache.is_dir() and any(cache.glob(f"{tag}*"))


needs_model = pytest.mark.skipif(
    not _model_cached(),
    reason=f"{RERANK_MODEL} not in the local fastembed cache; "
    "tests never trigger the ~80MB download",
)


@needs_model
def test_real_model_ranks_by_relevance():
    pytest.importorskip("fastembed.rerank.cross_encoder")
    hits = [{"snip": "garden"}, {"snip": "car"}]
    out = rerank_hits(
        "car trouble",
        hits,
        [
            "she planted tomatoes in the garden",
            "the automobile would not start this morning",
        ],
    )
    assert out[0]["snip"] == "car", f"real cross-encoder misranked: {out}"
    assert "rerank_score" in out[0]


@needs_model
def test_real_model_flat_guard_preserves_multilingual_first_stage():
    """Referent for RERANK_MIN_SPREAD: the English-only cross-encoder scores
    near-flat (and slightly WRONG) on non-English queries; the guard must keep
    the multilingual first-stage order. Companion to the cross-lingual
    assertions in tests/test_semantic_local.py."""
    pytest.importorskip("fastembed.rerank.cross_encoder")
    hits = [{"snip": "car"}, {"snip": "garden"}]
    out = rerank_hits(
        "el coche está averiado",
        hits,
        [
            "the automobile would not start this morning",
            "she planted tomatoes in the garden",
        ],
    )
    assert out[0]["snip"] == "car", (
        "flat-profile guard failed: English-only reranker overrode the "
        f"multilingual first stage on a Spanish query: {out}"
    )
