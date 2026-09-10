"""Composed semantic text search with weighted toward/away terms.

The embedding_similarity ``manual_text_query`` anchor gains an optional
``terms: [{text, weight}]`` that composes ONE query vector
(normalize-each -> weight -> sum -> normalize) and ranks via the existing cosine
path. A negative weight is STEERING (lean away / demote), NOT a hard exclude.
Backward compatible: a single ``text`` is one term, weight 1.0.

Geometry (3-d, padded to the fastembed space dim): drone=[1,0,0],
defense=[0,1,0], agriculture=[0,0,1]; rows are blends so steering is observable.
"""

from __future__ import annotations

import pytest

from frisket.ai.embeddings import build_batch_result, resolve_embedding_similarity
from frisket.ai.embeddings.similarity import SimilarityError
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project

PROJECT_ID = "p"

# concept + row strings -> 3-d direction (padded to the space dim at embed time)
_DIRS = {
    "drone": [1.0, 0.0, 0.0],
    "defense": [0.0, 1.0, 0.0],
    "agriculture": [0.0, 0.0, 1.0],
    # rows
    "plain_drone": [1.0, 0.0, 0.0],
    "military_drone": [1.0, 1.0, 0.0],  # drone + defense
    "ag_drone": [1.0, 0.0, 1.0],  # drone + agriculture
    "tank": [0.0, 1.0, 0.0],  # defense
}
_ROWS = ["plain_drone", "military_drone", "ag_drone", "tank"]


class RecordingGateway:
    """Maps known strings to padded vectors AND records each embed() call, so a
    composed query can prove it embeds ALL terms in ONE batch (not per term)."""

    def __init__(self, dim: int = 384):
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append(list(texts))
        out = [
            list(_DIRS.get(t, [0.0, 0.0, 0.0])) + [0.0] * (self.dim - 3) for t in texts
        ]
        return build_batch_result(
            out,
            provider_id=provider or "fastembed",
            provider_kind="local_process",
            actual_model_id=model or "fake",
            modality=modality,
        )


def _create_index(project, sheet, *, provider="fastembed", policy=None, key="c"):
    res = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": provider,
                "source_policy": {"kind": "text_cell"},
                "provider_policy": policy
                if policy is not None
                else {"allow_remote": False},
            },
            "idempotency_key": f"cts_create@{key}",
        },
        project_id=PROJECT_ID,
    )
    assert res.status == "completed", res.errors
    return res.outputs[0].ref["index_id"]


def _refresh(project, index_id, gateway, *, key="r"):
    res = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "full"},
            "idempotency_key": f"cts_refresh@{key}",
        },
        project_id=PROJECT_ID,
        deps=ExecutorDeps(embedding_gateway=gateway),
    )
    assert res.status == "completed", res.errors


@pytest.fixture
def env(tmp_path):
    project = Project.create(tmp_path / "cts.frisket", name="cts")
    sheet = project.add_sheet("contracts")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": r} for r in _ROWS], cols)
    index_id = _create_index(project, sheet)
    _refresh(project, index_id, RecordingGateway())
    yield project, sheet, cols, index_id
    project.close()


def _row(project, sheet, cols, text):
    return next(
        r for r, v in project.get_values(sheet, cols["headline"]).items() if v == text
    )


def _query(index_id, *, text=None, terms=None, limit=10):
    anchor: dict = {"kind": "manual_text_query"}
    if terms is not None:
        anchor["terms"] = terms
    if text is not None:
        anchor["text"] = text
    return {
        "kind": "embedding_similarity",
        "embedding_index_id": index_id,
        "anchor": anchor,
        "limit": limit,
    }


def _scores(result):
    return {hit.row_id: hit.score for hit in result.hits}


# --------------------------------------------------------------------------


def test_terms_accepted_and_equivalent_to_single_text(env):
    project, sheet, cols, index_id = env
    by_text = resolve_embedding_similarity(
        project, _query(index_id, text="drone"), gateway=RecordingGateway()
    )
    by_terms = resolve_embedding_similarity(
        project,
        _query(index_id, terms=[{"text": "drone", "weight": 1.0}]),
        gateway=RecordingGateway(),
    )
    # same ranking + same scores: cosine is scale-invariant, so a single term
    # weight 1.0 is exactly the single-text query.
    assert [h.row_id for h in by_text.hits] == [h.row_id for h in by_terms.hits]
    assert _scores(by_text) == pytest.approx(_scores(by_terms))


def test_positive_multi_term_ranks_both_aspects(env):
    project, sheet, cols, index_id = env
    res = resolve_embedding_similarity(
        project,
        _query(
            index_id,
            terms=[
                {"text": "drone", "weight": 1.0},
                {"text": "agriculture", "weight": 1.0},
            ],
        ),
        gateway=RecordingGateway(),
    )
    order = [h.row_id for h in res.hits]
    ag = _row(project, sheet, cols, "ag_drone")
    mil = _row(project, sheet, cols, "military_drone")
    tank = _row(project, sheet, cols, "tank")
    # ag_drone (drone + agriculture) is the best match for "drone + agriculture"
    assert order[0] == ag
    # and it beats both the defense-y rows
    assert order.index(ag) < order.index(mil)
    assert order.index(ag) < order.index(tank)


def test_negative_term_demotes_without_removing(env):
    project, sheet, cols, index_id = env
    mil = _row(project, sheet, cols, "military_drone")
    plain = _query(index_id, terms=[{"text": "drone", "weight": 1.0}])
    steered = _query(
        index_id,
        terms=[{"text": "drone", "weight": 1.0}, {"text": "defense", "weight": -1.0}],
    )
    base = resolve_embedding_similarity(project, plain, gateway=RecordingGateway())
    away = resolve_embedding_similarity(project, steered, gateway=RecordingGateway())
    base_scores, away_scores = _scores(base), _scores(away)
    # STEERING: the defense-heavy drone is demoted (lower score) by -defense...
    assert away_scores[mil] < base_scores[mil]
    # ...but NOT removed — steering is a soft lean, not a hard exclude/filter.
    assert mil in away_scores


def test_all_terms_embed_in_a_single_gateway_batch(env):
    project, sheet, cols, index_id = env
    gw = RecordingGateway()
    resolve_embedding_similarity(
        project,
        _query(
            index_id,
            terms=[
                {"text": "drone", "weight": 1.0},
                {"text": "defense", "weight": -0.5},
                {"text": "agriculture", "weight": 0.5},
            ],
        ),
        gateway=gw,
    )
    # exactly one embed call carrying ALL three term texts (one egress, not per term)
    assert len(gw.calls) == 1
    assert gw.calls[0] == ["drone", "defense", "agriculture"]


def test_remote_without_allow_remote_blocks_before_any_embed(tmp_path):
    project = Project.create(tmp_path / "remote.frisket", name="r")
    sheet = project.add_sheet("s")
    cols = {"headline": project.add_column(sheet, "headline")}
    project.add_rows(sheet, [{"headline": r} for r in _ROWS], cols)
    # a remote (openai) index whose policy does NOT allow remote egress
    index_id = _create_index(
        project, sheet, provider="openai", policy={"allow_remote": False}, key="remote"
    )
    gw = RecordingGateway()
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(
            project,
            _query(
                index_id,
                terms=[
                    {"text": "drone", "weight": 1.0},
                    {"text": "defense", "weight": -1.0},
                ],
            ),
            gateway=gw,
        )
    assert exc.value.code == "embedding_remote_confirmation_required"
    assert gw.calls == []  # gated BEFORE any embed
    project.close()


def test_single_text_and_one_term_lens_hash_identically():
    # The composed query saves as a manual_text_query lens; a single `text` and a
    # single one-weight term canonicalize to the SAME spec hash (identity/replay).
    # query_spec_hash normalizes internally, so pass the RAW query (allow_row_cell
    # = the lens path) — double-normalizing would re-reject manual_text_query.
    from frisket.features.watchlists.specs import query_spec_hash

    base = {
        "kind": "embedding_similarity",
        "embedding_index_id": "embidx_1",
        "sheet_id": 7,
    }
    h_text = query_spec_hash(
        {**base, "anchor": {"kind": "manual_text_query", "text": "drones"}},
        allow_row_cell=True,
    )
    h_term = query_spec_hash(
        {
            **base,
            "anchor": {
                "kind": "manual_text_query",
                "terms": [{"text": "drones", "weight": 1.0}],
            },
        },
        allow_row_cell=True,
    )
    assert h_text == h_term


def test_manual_text_query_still_rejected_for_watches():
    # Watches re-evaluate on a schedule, so a re-embedding manual_text_query stays
    # NOT watchable (unchanged contract) even though lenses now accept it.
    from frisket.features.watchlists.specs import normalize_query_spec

    with pytest.raises(ValueError):
        normalize_query_spec(
            {
                "kind": "embedding_similarity",
                "embedding_index_id": "embidx_1",
                "sheet_id": 7,
                "anchor": {"kind": "manual_text_query", "text": "drones"},
            },
            scope={"kind": "sheet", "sheet_id": 7},
            allow_row_cell=False,  # watch path
        )


def test_cancelling_terms_raise_typed_degenerate_error(env):
    # Equal-and-opposite terms compose to the zero vector — a degenerate query, not a
    # silent all-equal ranking. Surfaces a typed error (review LOW: lock the contract).
    project, sheet, cols, index_id = env
    with pytest.raises(SimilarityError) as exc:
        resolve_embedding_similarity(
            project,
            _query(
                index_id,
                terms=[
                    {"text": "drone", "weight": 1.0},
                    {"text": "drone", "weight": -1.0},
                ],
            ),
            gateway=RecordingGateway(),
        )
    assert exc.value.code == "embedding_composed_query_degenerate"


def test_anchor_kind_is_case_insensitive(env):
    # the resolver lowercases kind/anchor.kind like the normalizer, so a
    # raw query with mixed-case "Manual_Text_Query" resolves identically (the preview
    # path bypasses the normalizer and used to fail with unsupported anchor kind).
    project, sheet, cols, index_id = env
    lower = resolve_embedding_similarity(
        project, _query(index_id, text="drone"), gateway=RecordingGateway()
    )
    mixed_query = {
        "kind": "Embedding_Similarity",
        "embedding_index_id": index_id,
        "anchor": {"kind": "Manual_Text_Query", "text": "drone"},
        "limit": 10,
    }
    mixed = resolve_embedding_similarity(
        project, mixed_query, gateway=RecordingGateway()
    )
    assert [h.row_id for h in mixed.hits] == [h.row_id for h in lower.hits]
