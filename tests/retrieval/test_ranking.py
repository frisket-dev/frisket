from __future__ import annotations

import math

import pytest

from frisket.retrieval.ranking import (
    RankingError,
    compose_weighted_vectors,
    reciprocal_rank_fusion,
)


def test_signed_weighted_composition_normalizes_terms_then_sum():
    vector = compose_weighted_vectors([[10.0, 0.0], [0.0, 2.0]], [1.0, -1.0])

    assert vector == pytest.approx((1 / math.sqrt(2), -1 / math.sqrt(2)))


def test_weighted_composition_rejects_cancellation():
    with pytest.raises(RankingError, match="cancel"):
        compose_weighted_vectors([[1.0, 0.0], [2.0, 0.0]], [1.0, -1.0])


def test_rrf_retains_method_ranks_and_deterministic_ties():
    fused = reciprocal_rank_fusion([["b", "a"], ["a", "c"]], k=10)

    assert [row.key for row in fused] == ["a", "b", "c"]
    assert fused[0].ranks == (2, 1)
    assert fused[1].ranks == (1, None)
