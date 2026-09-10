"""Semantic dedupe: the ``cluster`` op supports ``method: semantic``.

The fingerprint method (ops/cluster_fingerprint.py) only collides values that share
normalized tokens — "Jon Smith" / "Smith, Jon" — and misses meaning-level
duplicates like "WHO" / "World Health Organization". ``method: semantic``
embeds the column's distinct values via frisket/semantic.py's
``resolve_embedder`` (local fastembed model first, API embeddings via the
router second — remote cost flows through the router as usual) and groups
values whose pairwise cosine similarity is >= ``threshold`` (default
``DEFAULT_THRESHOLD``, spec-overridable) into single-linkage connected
components.

Output contract: IDENTICAL to the fingerprint cluster panel shape —
``{sheet_id, column, clusters: [{key, canonical, size, values: [{value,
count}], row_ids}], count}`` — so the existing OpenRefine-style cluster panel
renders semantic clusters with zero web changes. Extra top-level envelope keys
(``method``, ``threshold``, ``semantic``, ``distinct_values``, ``considered``)
are additive only.

Scale: pairwise cosine is O(n²·d) over *distinct values* (not rows). Solo
considers the complete value set by default while keeping embedding provider
calls in bounded batches. ``FRISKET_CLUSTER_SEMANTIC_CAP`` remains an explicit
deployment escape hatch; when set, the envelope reports ``distinct_values`` vs
``considered`` so that truncation is visible.

Vectors are NOT re-embedded per run: they come from the content-addressed
``cell_vec`` sidecar cache (sha1(model_id + content)), shared with semantic
search — a value embedded for search is free here and vice versa.

No embedding backend at all → honest fallback to fingerprint clustering,
flagged ``semantic: False`` / ``method: "fingerprint"`` in the envelope (the
same never-fake-meaning rule as semantic search's FTS fallback).
"""

from __future__ import annotations

import hashlib
import math
import os
from collections import Counter
from typing import Any, Callable

from frisket.ops.cluster_fingerprint import canonical, column_values
from frisket.semantic import Embedder, _doc_vectors
from frisket.engine.store import Project

DEFAULT_THRESHOLD = 0.85
SEMANTIC_EMBED_BATCH_SIZE = 500


def _value_cap() -> int | None:
    raw = os.environ.get("FRISKET_CLUSTER_SEMANTIC_CAP")
    if raw is None:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def _unit(vec: list[float]) -> list[float] | None:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return None  # degenerate vector: never matches anything
    return [x / norm for x in vec]


def _components(units: list[list[float] | None], threshold: float) -> list[list[int]]:
    """Single-linkage connected components over pairwise cosine >= threshold.
    Union-find with path compression; O(n²) dot products on unit vectors."""
    n = len(units)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        ui = units[i]
        if ui is None:
            continue
        for j in range(i + 1, n):
            uj = units[j]
            if uj is None or find(i) == find(j):
                continue
            cos = sum(a * b for a, b in zip(ui, uj, strict=False))
            if cos >= threshold:
                parent[find(j)] = find(i)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _cluster_key(values: list[str]) -> str:
    """Stable key for a semantic cluster: content-addressed over its members
    (same members → same key across runs, regardless of ordering)."""
    digest = hashlib.sha1(
        "\0".join(sorted(values)).encode("utf-8", "replace")
    ).hexdigest()
    return f"sem:{digest[:16]}"


def _semantic_surface_order(
    values: dict[int, str],
) -> tuple[dict[str, list[int]], list[str]]:
    """Return the exact surfaces and ordered corpus semantic clustering embeds."""
    surfaces: dict[str, list[int]] = {}
    for row_id, surface in values.items():
        surfaces.setdefault(surface, []).append(row_id)
    ordered = sorted(surfaces, key=lambda s: (-len(surfaces[s]), s))
    cap = _value_cap()
    if cap is not None:
        ordered = ordered[:cap]
    return surfaces, ordered


def _embed_planned_batch(
    embed: Embedder, batch_index: int, texts: list[str]
) -> list[list[float]]:
    stable_batch_embed = getattr(embed, "semantic_batch", None)
    batch_vectors = (
        stable_batch_embed(batch_index, texts)
        if callable(stable_batch_embed)
        else embed(texts)
    )
    if len(batch_vectors) != len(texts):
        raise RuntimeError(
            "embedding provider returned "
            f"{len(batch_vectors)} vectors for {len(texts)} inputs — "
            "refusing the batch rather than caching vectors bound to the "
            "wrong documents"
        )
    return batch_vectors


def semantic_embedding_inputs(
    values: dict[int, str],
    *,
    derive_key: Callable[[str], str] | None = None,
) -> list[str]:
    """The exact provider/cache inputs a semantic cluster run would embed.

    The cost/consent seam calls this before provider egress, while the cluster
    implementation below calls the same ordering helper for execution. Keeping
    the frequency-first corpus in one place prevents quote/execution drift when
    duplicate counts or an explicit semantic value cap change.
    """
    _, ordered = _semantic_surface_order(values)
    if derive_key is None:
        return ordered
    return [derive_key(surface) for surface in ordered]


def compute_semantic_clusters(
    project: Project,
    sheet_id: int,
    column: str,
    embed: Embedder,
    embed_id: str,
    threshold: float = DEFAULT_THRESHOLD,
    min_size: int = 2,
    *,
    values: dict[int, str] | None = None,
    derive_key: Callable[[str], str] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Group a column's values by pairwise cosine >= ``threshold``.

    Returns (clusters, distinct_values, considered). Cluster entries match
    cluster_fingerprint.compute_clusters exactly: ``{key, canonical, size,
    values: [{value, count}], row_ids}``, largest-first; ``min_size`` filters
    on distinct surface forms, same as the fingerprint method.

    ``derive_key`` (cluster-by-key) transforms each ORIGINAL surface into the
    text that is EMBEDDED — so meaning is compared over the derived form while
    the cluster's members/canonical/row_ids stay the original surfaces. ``None``
    embeds the surface directly.

    ``values`` (row_id -> non-empty stripped surface) may be a pre-read
    snapshot so the caller reads the column exactly once (shared with the
    value-hash), instead of a second read a concurrent edit could tear.
    """
    raw = column_values(project, sheet_id, column) if values is None else values
    surfaces, ordered = _semantic_surface_order(raw)
    distinct = len(surfaces)
    considered = len(ordered)
    if not ordered:
        return [], distinct, considered

    # cached, content-addressed vectors — shared with semantic search. When a
    # key is derived, the DERIVED text is embedded (identical derived text ->
    # identical vector -> guaranteed to cluster), never the original surface.
    embed_texts = (
        [derive_key(s) for s in ordered] if derive_key is not None else ordered
    )
    vectors: list[list[float]] = []
    for batch_index, offset in enumerate(
        range(0, len(embed_texts), SEMANTIC_EMBED_BATCH_SIZE)
    ):
        planned_batch = embed_texts[offset : offset + SEMANTIC_EMBED_BATCH_SIZE]
        vectors.extend(
            _doc_vectors(
                project,
                [{"content": text} for text in planned_batch],
                lambda texts, index=batch_index: _embed_planned_batch(
                    embed, index, texts
                ),
                embed_id,
            )
        )
    units = [_unit(v) for v in vectors]

    clusters: list[dict[str, Any]] = []
    for idxs in _components(units, threshold):
        members = [ordered[i] for i in idxs]
        if len(members) < min_size:
            continue
        counts: Counter[str] = Counter({s: len(surfaces[s]) for s in members})
        row_ids = sorted(rid for s in members for rid in surfaces[s])
        clusters.append(
            {
                "key": _cluster_key(members),
                "canonical": canonical(counts),
                "size": len(row_ids),
                "values": [
                    {"value": s, "count": counts[s]}
                    for s in sorted(counts, key=lambda s: (-counts[s], s))
                ],
                "row_ids": row_ids,
            }
        )
    clusters.sort(key=lambda c: (-c["size"], c["key"]))
    return clusters, distinct, considered
