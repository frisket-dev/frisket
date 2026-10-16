from __future__ import annotations

import math

import pytest

from frisket.retrieval.ranking import (
    RankingError,
    best_per_group,
    compose_weighted_vectors,
    excluded_by_similarity,
    reciprocal_rank_fusion,
)


def test_signed_weighted_composition_normalizes_terms_then_sum():
    vector = compose_weighted_vectors([[10.0, 0.0], [0.0, 2.0]], [1.0, -1.0])

    assert vector == pytest.approx((1 / math.sqrt(2), -1 / math.sqrt(2)))


def test_weighted_composition_rejects_cancellation():
    with pytest.raises(RankingError, match="cancel"):
        compose_weighted_vectors([[1.0, 0.0], [2.0, 0.0]], [1.0, -1.0])


def test_exclusions_have_independent_thresholds():
    candidate = [1.0, 0.0]

    assert excluded_by_similarity(candidate, [([0.8, 0.6], 0.75)])
    assert not excluded_by_similarity(candidate, [([0.8, 0.6], 0.9)])


def test_rrf_retains_method_ranks_and_deterministic_ties():
    fused = reciprocal_rank_fusion([["b", "a"], ["a", "c"]], k=10)

    assert [row.key for row in fused] == ["a", "b", "c"]
    assert fused[0].ranks == (2, 1)
    assert fused[1].ranks == (1, None)


def test_grouping_keeps_best_ranked_moment():
    ranked = [
        {"source": "video", "part": "12-16"},
        {"source": "video", "part": "13-17"},
        {"source": "other", "part": "2-4"},
    ]

    assert best_per_group(ranked, group_key=lambda row: row["source"], limit=2) == [
        ranked[0],
        ranked[2],
    ]
