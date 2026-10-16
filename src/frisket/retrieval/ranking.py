"""Deterministic ranking primitives shared by retrieval query modes."""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar


DEFAULT_RRF_K = 60
ZERO_NORM_EPSILON = 1e-12


class RankingError(ValueError):
    """A ranking policy cannot be applied without changing its semantics."""


def normalize_vector(vector: Sequence[float]) -> tuple[float, ...]:
    """Return a finite unit vector, rejecting empty or directionless inputs."""

    if not vector:
        raise RankingError("vector must not be empty")
    values = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in values):
        raise RankingError("vector values must be finite")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= ZERO_NORM_EPSILON:
        raise RankingError("vector has no direction")
    return tuple(value / norm for value in values)


def compose_weighted_vectors(
    vectors: Sequence[Sequence[float]], weights: Sequence[float]
) -> tuple[float, ...]:
    """Normalize terms, apply signed weights, add, then normalize the result."""

    if not vectors or len(vectors) != len(weights):
        raise RankingError("vectors and weights must be non-empty and aligned")
    dimension = len(vectors[0])
    if dimension == 0 or any(len(vector) != dimension for vector in vectors):
        raise RankingError("all vectors must have the same positive dimension")
    numeric_weights = tuple(float(weight) for weight in weights)
    if not all(math.isfinite(weight) for weight in numeric_weights):
        raise RankingError("weights must be finite")
    accumulator = [0.0] * dimension
    for vector, weight in zip(vectors, numeric_weights, strict=True):
        unit = normalize_vector(vector)
        for index, value in enumerate(unit):
            accumulator[index] += value * weight
    try:
        return normalize_vector(accumulator)
    except RankingError as exc:
        raise RankingError("weighted vectors cancel to zero") from exc


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity used only for bounded composition/exclusion validation."""

    if len(left) != len(right):
        raise RankingError("vectors have different dimensions")
    left_unit = normalize_vector(left)
    right_unit = normalize_vector(right)
    return sum(a * b for a, b in zip(left_unit, right_unit, strict=True))


def excluded_by_similarity(
    candidate: Sequence[float],
    exclusions: Sequence[tuple[Sequence[float], float]],
) -> bool:
    """Apply each hard exclusion in its compatible cosine space."""

    for vector, threshold in exclusions:
        cutoff = float(threshold)
        if not math.isfinite(cutoff) or not -1.0 <= cutoff <= 1.0:
            raise RankingError("exclusion threshold must be within [-1, 1]")
        if cosine_similarity(candidate, vector) >= cutoff:
            return True
    return False


Key = TypeVar("Key", bound=Hashable)


@dataclass(frozen=True)
class FusedRank:
    key: Hashable
    score: float
    ranks: tuple[int | None, ...]


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[Key]], *, k: int = DEFAULT_RRF_K
) -> list[FusedRank]:
    """Fuse incomparable ranked methods and retain each method's contribution."""

    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise RankingError("k_rrf must be a positive integer")
    scores: dict[Key, float] = {}
    positions: dict[Key, list[int | None]] = {}
    method_count = len(ranked_lists)
    for method, ranked in enumerate(ranked_lists):
        seen: set[Key] = set()
        for rank, key in enumerate(ranked, start=1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            positions.setdefault(key, [None] * method_count)[method] = rank
    return [
        FusedRank(key=key, score=score, ranks=tuple(positions[key]))
        for key, score in sorted(
            scores.items(), key=lambda item: (-item[1], _stable_key(item[0]))
        )
    ]


Item = TypeVar("Item")


def best_per_group(
    ranked: Iterable[Item],
    *,
    group_key: Callable[[Item], Hashable],
    limit: int,
) -> list[Item]:
    """Keep the first (best) item for each group from an already ranked stream."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise RankingError("limit must be a positive integer")
    seen: set[Hashable] = set()
    result: list[Item] = []
    for item in ranked:
        key = group_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) == limit:
            break
    return result


def _stable_key(value: object) -> tuple[str, str]:
    return type(value).__name__, repr(value)
