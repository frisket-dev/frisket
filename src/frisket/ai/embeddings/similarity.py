"""``embedding_similarity`` retrieval backend for finding similar rows.

A narrow query path over ONE embedding index / space. It is NOT a persisted
column and NOT a saved view: the result carries per-row distance/similarity as
query metadata. Two anchors:

- ``row`` / ``row_cell`` — "show similar to this row" using the row's ALREADY
  stored vector as the query. No provider call.
- ``manual_text_query`` — embed query text through the gateway. Remote egress is
  gated on the index provider policy, exactly like a refresh.

All comparison happens inside the index's single ``space_id``; a query that names
a different space, a mis-dimensioned text embedding, a missing/stale anchor, or
an unsupported modality fails with a typed error before ranking. Results are
constrained to the index's sheet and to currently-visible rows.

Not in scope here: UI, saved LensSpec/QuerySpec persistence, watchlists,
clustering, scheduled refresh, or export.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from frisket.engine.store import Project

from .gateway import (
    EmbeddingBackendUnavailable,
    EmbeddingGateway,
    EmbeddingProviderError,
)
from .source_payload import build_source_payloads, source_hash_for_row
from .store import REMOTE_PROVIDER_KINDS, EmbeddingStore
from .vector_backend import VectorBackend

EMBEDDING_SIMILARITY_QUERY_KIND = "embedding_similarity"
DEFAULT_SIMILARITY_LIMIT = 50
_EMBEDDABLE_MODALITIES = frozenset({"text", "row"})
_ROW_ANCHOR_KINDS = frozenset({"row", "row_cell", "vector_ref"})
# Composed manual_text_query: at most this many weighted terms per query (one
# gateway batch). Bounds egress + keeps the query a focused steer, not a corpus.
MAX_QUERY_TERMS = 16
# Hard NOT: a row whose cosine SCORE to an exclude term is >= this is
# REMOVED. The default is a moderate cutoff; each exclude term may override it.
DEFAULT_EXCLUDE_THRESHOLD = 0.5


def normalize_manual_text_excludes(anchor: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical ``[{text, threshold}]`` for a manual_text_query anchor's optional
    ``exclude`` (a hard gate that removes rows, distinct from the
    steering ``terms``). Absent -> []. Each threshold defaults to
    ``DEFAULT_EXCLUDE_THRESHOLD`` and must be in (0, 1]. Raises ``ValueError`` on a
    malformed exclude (callers map it to their typed error). Shared by the resolver
    and the lens normalizer."""
    raw = anchor.get("exclude")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("manual_text_query exclude must be a list")
    if len(raw) > MAX_QUERY_TERMS:
        raise ValueError(
            f"manual_text_query supports at most {MAX_QUERY_TERMS} exclude terms"
        )
    out: list[dict[str, Any]] = []
    for i, term in enumerate(raw):
        if not isinstance(term, dict):
            raise ValueError(f"exclude {i} must be an object {{text, threshold}}")
        t = term.get("text")
        if not isinstance(t, str) or not t.strip():
            raise ValueError(f"exclude {i} needs non-empty text")
        thr = term.get("threshold", DEFAULT_EXCLUDE_THRESHOLD)
        if (
            isinstance(thr, bool)
            or not isinstance(thr, (int, float))
            or not math.isfinite(thr)
        ):
            raise ValueError(f"exclude {i} threshold must be a finite number")
        thr = float(thr)
        if not (0.0 < thr <= 1.0):
            raise ValueError(f"exclude {i} threshold must be in (0, 1]")
        out.append({"text": t.strip(), "threshold": thr})
    return out


def normalize_manual_text_terms(anchor: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical ``[{text, weight}]`` for a ``manual_text_query`` anchor (Stage 7
    Lane F). Accepts EITHER a single ``text`` (-> one term, weight 1.0) or an
    explicit ``terms`` list of ``{text, weight}``; weight is a signed float
    (negative = steer AWAY). Raises ``ValueError`` on a malformed anchor — callers
    map it to their own typed error (the resolver -> ``SimilarityError``; the lens
    normalizer lets it propagate as an ``invalid_lens_spec`` ValueError). One
    definition shared by the resolver and the lens normalizer so they cannot drift.
    """
    raw_terms = anchor.get("terms")
    text = anchor.get("text")
    if raw_terms is not None and text is not None:
        raise ValueError("manual_text_query takes either text or terms, not both")
    if raw_terms is None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("manual_text_query requires non-empty text or terms")
        return [{"text": text.strip(), "weight": 1.0}]
    if not isinstance(raw_terms, list) or not raw_terms:
        raise ValueError("manual_text_query terms must be a non-empty list")
    if len(raw_terms) > MAX_QUERY_TERMS:
        raise ValueError(f"manual_text_query supports at most {MAX_QUERY_TERMS} terms")
    out: list[dict[str, Any]] = []
    for i, term in enumerate(raw_terms):
        if not isinstance(term, dict):
            raise ValueError(f"term {i} must be an object {{text, weight}}")
        t = term.get("text")
        if not isinstance(t, str) or not t.strip():
            raise ValueError(f"term {i} needs non-empty text")
        w = term.get("weight", 1.0)
        if (
            isinstance(w, bool)
            or not isinstance(w, (int, float))
            or not math.isfinite(w)
        ):
            raise ValueError(f"term {i} weight must be a finite number")
        out.append({"text": t.strip(), "weight": float(w)})
    return out


def _compose_query_vector(
    vectors: list[list[float]], weights: list[float], dim: int
) -> list[float]:
    """One query vector from weighted terms: NORMALIZE each term to unit length,
    scale by its signed weight, sum, then normalize the result. Normalizing each
    term first keeps term magnitude (sentence length) from skewing the lean; a
    negative weight subtracts (steers away). A composed vector that cancels to ~0
    (e.g. equal-and-opposite terms) is a degenerate query, not a silent all-equal
    ranking."""
    acc = [0.0] * dim
    for vec, weight in zip(vectors, weights):
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            continue  # a zero term vector contributes no direction
        scale = weight / norm
        for i in range(dim):
            acc[i] += vec[i] * scale
    total = math.sqrt(sum(x * x for x in acc))
    if total <= 1e-12:
        raise SimilarityError(
            "embedding_composed_query_degenerate",
            "the composed query vector cancels to zero (terms/weights offset each "
            "other); adjust the terms or weights",
            field="anchor.terms",
        )
    return [x / total for x in acc]


class SimilarityError(Exception):
    """A similarity query that cannot run. Carries a typed action error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = dict(details or {})


@dataclass(frozen=True)
class SimilarityHit:
    row_id: int
    source_key: str
    sheet_id: int | None
    distance: float
    score: float | None  # cosine similarity (1 - distance) for cosine spaces


@dataclass(frozen=True)
class QueryEmbeddingUse:
    provider_id: str
    provider_kind: str
    actual_model_id: str


@dataclass(frozen=True)
class EmbeddingQueryBinding:
    index_id: str
    index_metadata: tuple[Any, ...]
    space_metadata: tuple[Any, ...]
    sidecar_hash: str


@dataclass(frozen=True)
class SimilarityResult:
    index_id: str
    space_id: str
    sheet_id: int | None
    distance_metric: str
    anchor: dict[str, Any]
    limit: int
    hits: list[SimilarityHit]
    query_embedding_use: QueryEmbeddingUse | None
    binding: EmbeddingQueryBinding


_INDEX_BINDING_COLUMNS = (
    "id",
    "space_id",
    "sheet_id",
    "source_query_json",
    "source_columns_json",
    "source_policy_hash",
    "provider_policy_json",
    "status",
    "last_refreshed_at",
    "total_items",
    "ready_items",
    "stale_items",
    "error_items",
)
_SPACE_BINDING_COLUMNS = (
    "id",
    "descriptor_hash",
    "provider_id",
    "provider_kind",
    "actual_model_id",
    "modality",
    "dimension",
    "distance_metric",
)


def _embedding_query_binding(
    index: Any, space: Any, backend: VectorBackend
) -> EmbeddingQueryBinding:
    return EmbeddingQueryBinding(
        index_id=str(index["id"]),
        index_metadata=tuple(index[column] for column in _INDEX_BINDING_COLUMNS),
        space_metadata=tuple(space[column] for column in _SPACE_BINDING_COLUMNS),
        sidecar_hash=backend.index_binding(str(index["id"])),
    )


def current_embedding_query_binding(
    project: Project, index_id: str
) -> EmbeddingQueryBinding | None:
    """Read the current metadata + vector binding for an evaluated index."""
    store = EmbeddingStore(project)
    index = store.get_index(index_id)
    if index is None:
        return None
    space = store.get_space(index["space_id"])
    if space is None:
        return None
    backend = VectorBackend(project)
    if not backend.db_path.exists():
        return None
    try:
        backend.begin_read_snapshot()
        return _embedding_query_binding(index, space, backend)
    except Exception:
        return None
    finally:
        backend.close()


def _requested_limit(query: dict[str, Any]) -> int:
    raw = query.get("limit", DEFAULT_SIMILARITY_LIMIT)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SimilarityError(
            "invalid_query_spec", "limit must be an integer", field="limit"
        )
    if raw <= 0:
        raise SimilarityError(
            "invalid_query_spec", "limit must be a positive integer", field="limit"
        )
    return raw


def _score_for(metric: str, distance: float) -> float | None:
    # cosine distance = 1 - cosine similarity; only cosine spaces get a score.
    return round(1.0 - distance, 6) if metric == "cosine" else None


def resolve_embedding_similarity(
    project: Project,
    query: dict[str, Any],
    *,
    gateway: Any = None,
    backend: VectorBackend | None = None,
) -> SimilarityResult:
    """Resolve an ``embedding_similarity`` QuerySpec into ranked source rows.

    ``gateway`` is only used for a ``manual_text_query`` anchor (and only after
    the remote-egress gate passes); row anchors never touch it.
    """
    if not isinstance(query, dict):
        raise SimilarityError("invalid_query_spec", "query must be an object")
    # Lower-case to match the normalizer (specs.py), so a raw query reaching the
    # resolver directly (the preview path) accepts the same casing a saved spec does.
    kind = str(query.get("kind") or "").strip().lower()
    if kind != EMBEDDING_SIMILARITY_QUERY_KIND:
        raise SimilarityError(
            "invalid_query_spec",
            f"query kind must be {EMBEDDING_SIMILARITY_QUERY_KIND!r}",
            field="kind",
        )
    index_id = query.get("embedding_index_id")
    if not isinstance(index_id, str) or not index_id.strip():
        raise SimilarityError(
            "invalid_query_spec",
            "embedding_index_id is required",
            field="embedding_index_id",
        )

    store = EmbeddingStore(project)
    index = store.get_index(index_id)
    if index is None:
        raise SimilarityError(
            "embedding_index_not_found",
            f"no embedding index {index_id!r}",
            field="embedding_index_id",
        )
    space = store.get_space(index["space_id"])
    if space is None:
        raise SimilarityError(
            "embedding_index_not_found",
            f"embedding index {index_id!r} references a missing space",
            field="embedding_index_id",
        )
    space_id = space["id"]
    # An explicit space_id must match the index's space — never compare across
    # spaces (exactly one space_id per query).
    requested_space = query.get("space_id")
    if requested_space is not None and requested_space != space_id:
        raise SimilarityError(
            "embedding_space_mismatch",
            f"query space_id {requested_space!r} is not the index space {space_id!r}",
            field="space_id",
        )

    anchor = query.get("anchor")
    if not isinstance(anchor, dict):
        raise SimilarityError(
            "invalid_query_spec", "anchor must be an object", field="anchor"
        )
    anchor_kind = str(anchor.get("kind") or "").strip().lower()
    limit = _requested_limit(query)
    sheet_id = index["sheet_id"]
    metric = space["distance_metric"]
    space_dim = int(space["dimension"])
    source_columns = json.loads(index["source_columns_json"])

    owned_backend = backend is None
    backend = backend or VectorBackend(project)
    if owned_backend:
        backend.ensure_schema()
        backend.begin_read_snapshot()
    try:
        if anchor_kind in _ROW_ANCHOR_KINDS:
            qvec, exclude = _row_anchor_vector(
                project, backend, index, source_columns, space_dim, anchor
            )
            query_embedding_use = None
        elif anchor_kind == "manual_text_query":
            qvec, exclude, query_embedding_use = _manual_text_vector(
                project, index, space, anchor, gateway, backend
            )
        else:
            raise SimilarityError(
                "embedding_source_unsupported",
                f"unsupported anchor kind {anchor_kind!r}",
                field="anchor.kind",
            )

        hits = _ranked_hits(
            project,
            backend,
            index,
            space_id,
            qvec,
            exclude,
            sheet_id=sheet_id,
            source_columns=source_columns,
            limit=limit,
            threshold=query.get("threshold"),
            metric=metric,
        )
        binding = _embedding_query_binding(index, space, backend)
    finally:
        if owned_backend:
            backend.close()

    return SimilarityResult(
        index_id=index_id,
        space_id=space_id,
        sheet_id=sheet_id,
        distance_metric=metric,
        anchor=dict(anchor),
        limit=limit,
        hits=hits,
        query_embedding_use=query_embedding_use,
        binding=binding,
    )


def _ranked_hits(
    project: Project,
    backend: VectorBackend,
    index: Any,
    space_id: str,
    qvec: list[float],
    exclude: set[str],
    *,
    sheet_id: int | None,
    source_columns: list[str],
    limit: int,
    threshold: Any,
    metric: str,
) -> list[SimilarityHit]:
    """Top-``limit`` nearest visible, NON-stale rows.

    The candidate pool grows adaptively (instead of a fixed ``limit + N``) so a
    run can still return ``limit`` visible rows when many higher-ranked rows are
    hidden or stale — never starved by a small cap. query_similar already filters
    to the query's exact dimension, so every candidate vector matches the space.
    """
    index_id = index["id"]
    visible = set(project.visible_row_ids(sheet_id)) if sheet_id is not None else None
    ready = backend.item_counts(index_id).get("ready", 0)
    cap = ready + len(exclude)  # the most rows query_similar can return
    if cap <= 0:
        return []
    k = min(max(limit * 4, limit + len(exclude) + 1), cap)
    while True:
        pool = backend.query_similar(
            index_id, space_id, qvec, k=k, exclude_source_keys=exclude
        )
        hits = _accept_candidates(
            project,
            backend,
            index,
            pool,
            visible=visible,
            sheet_id=sheet_id,
            source_columns=source_columns,
            limit=limit,
            threshold=threshold,
            metric=metric,
        )
        # enough accepted, or the pool is exhausted (no more ready rows to pull)
        if len(hits) >= limit or len(pool) < k or k >= cap:
            return hits[:limit]
        k = min(k * 4, cap)


def _accept_candidates(
    project: Project,
    backend: VectorBackend,
    index: Any,
    pool: list[tuple[str, float]],
    *,
    visible: set[int] | None,
    sheet_id: int | None,
    source_columns: list[str],
    limit: int,
    threshold: Any,
    metric: str,
) -> list[SimilarityHit]:
    candidates: list[tuple[str, int, float]] = []
    for source_key, distance in pool:
        try:
            row_id = int(source_key)
        except (TypeError, ValueError):
            continue
        if visible is not None and row_id not in visible:
            continue
        if isinstance(threshold, int | float) and not isinstance(threshold, bool):
            if distance > float(threshold):
                continue
        candidates.append((source_key, row_id, distance))
    if not candidates:
        return []
    # Staleness: a sidecar 'ready' row is only valid if its stored source_hash
    # still matches the row's CURRENT content (same builder as refresh). Stale or
    # content-removed candidates are dropped — never returned as nearest matches.
    stored = backend.get_source_hashes(index["id"], [sk for sk, _, _ in candidates])
    current = {
        p["source_key"]: p["source_hash"]
        for p in build_source_payloads(
            project, index, source_columns, [rid for _, rid, _ in candidates]
        )
    }
    hits: list[SimilarityHit] = []
    for source_key, row_id, distance in candidates:
        if stored.get(source_key) != current.get(source_key):
            continue
        hits.append(
            SimilarityHit(
                row_id=row_id,
                source_key=source_key,
                sheet_id=sheet_id,
                distance=distance,
                score=_score_for(metric, distance),
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _row_anchor_vector(
    project: Project,
    backend: VectorBackend,
    index: Any,
    source_columns: list[str],
    space_dim: int,
    anchor: dict[str, Any],
) -> tuple[list[float], set[str]]:
    index_id = index["id"]
    row_id = anchor.get("row_id")
    if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0:
        raise SimilarityError(
            "invalid_query_spec",
            "row anchor requires a positive integer row_id",
            field="anchor.row_id",
        )
    source_key = str(row_id)
    item = backend.get_item(index_id, source_key)
    if item is None or item["vector_id"] is None:
        raise SimilarityError(
            "embedding_anchor_not_found",
            f"row {row_id} has no embedding in this index; refresh it first",
            field="anchor.row_id",
        )
    if item["status"] != "ready":
        raise SimilarityError(
            "embedding_source_stale",
            f"row {row_id}'s embedding is {item['status']!r}, not ready",
            field="anchor.row_id",
        )
    # 'ready' is not enough: recompute the row's current source hash (same builder
    # as refresh). If it differs, the cell changed since the vector was written —
    # the stored vector is stale and must not anchor a search.
    current_hash = source_hash_for_row(project, index, source_columns, row_id)
    if current_hash is None or current_hash != item["source_hash"]:
        raise SimilarityError(
            "embedding_source_stale",
            f"row {row_id}'s source content changed since it was embedded; "
            "refresh the index before anchoring on it",
            field="anchor.row_id",
        )
    qvec = backend.get_vector(index_id, source_key)
    if not qvec:
        raise SimilarityError(
            "embedding_anchor_not_found",
            f"row {row_id}'s stored vector is missing",
            field="anchor.row_id",
        )
    if len(qvec) != space_dim:
        raise SimilarityError(
            "embedding_space_mismatch",
            f"row {row_id}'s stored vector has dimension {len(qvec)}, "
            f"not the space dimension {space_dim}",
            field="anchor.row_id",
        )
    return qvec, {source_key}


def _manual_text_vector(
    project: Project,
    index: Any,
    space: Any,
    anchor: dict[str, Any],
    gateway: Any,
    backend: Any,
) -> tuple[list[float], set[str], QueryEmbeddingUse]:
    modality = space["modality"]
    if modality not in _EMBEDDABLE_MODALITIES:
        raise SimilarityError(
            "embedding_source_unsupported",
            f"manual text query needs a text space, not {modality!r}",
            field="anchor.kind",
        )
    # Steering terms + hard-exclude terms. A single `text` is one
    # term weight 1.0; `terms` composes a steered vector; `exclude` REMOVES rows about
    # a concept. Malformed -> typed 400.
    try:
        terms = normalize_manual_text_terms(anchor)
        excludes = normalize_manual_text_excludes(anchor)
    except ValueError as exc:
        raise SimilarityError(
            "invalid_query_spec", str(exc), field="anchor.terms"
        ) from exc
    # Remote egress gate — BEFORE constructing/using a gateway, mirroring refresh. ALL
    # terms AND exclude terms ride the SAME index policy: one gate covers one batch.
    provider_kind = space["provider_kind"]
    try:
        provider_policy = json.loads(index["provider_policy_json"] or "{}")
    except (TypeError, ValueError):
        provider_policy = {}
    if provider_kind in REMOTE_PROVIDER_KINDS and gateway is None:
        raise SimilarityError(
            "embedding_remote_preview_unsupported",
            "this preview path does not send manual or hybrid query text to remote "
            "embedding providers; use the dedicated embedding preview route",
            field="anchor.text",
        )
    if provider_kind in REMOTE_PROVIDER_KINDS and not provider_policy.get(
        "allow_remote"
    ):
        raise SimilarityError(
            "embedding_remote_confirmation_required",
            (
                f"index provider_kind={provider_kind!r} is remote; its provider "
                "policy must set allow_remote=true before a manual text query can "
                "embed remotely"
            ),
            field="anchor.text",
        )
    gw = gateway or EmbeddingGateway()
    if provider_kind in REMOTE_PROVIDER_KINDS:
        # ``allow_remote`` is egress consent, not permission to ignore the
        # independent bound on the project-owned key this provider will use.
        # This is the last effect-site check before the gateway call, matching
        # remote index create/refresh rather than relying on a route caller to
        # remember a preview-specific preflight.
        from frisket.engine.runner.validation import (
            ProviderKeyRefusal,
            assert_provider_spend_cap,
        )

        try:
            assert_provider_spend_cap(project, str(space["provider_id"]))
        except ProviderKeyRefusal as exc:
            raise SimilarityError(
                exc.error_code,
                exc.action_message(),
                field="anchor.text",
                details=exc.details,
            ) from exc
    try:
        # ONE batch embed for ALL term + exclude texts — one egress, not per term.
        texts = [t["text"] for t in terms] + [e["text"] for e in excludes]
        result = gw.embed(
            texts,
            provider=space["provider_id"],
            model=space["actual_model_id"],
            modality=modality,
        )
    except EmbeddingBackendUnavailable as exc:
        raise SimilarityError("embedding_backend_unavailable", str(exc)) from exc
    except EmbeddingProviderError as exc:
        raise SimilarityError("embedding_provider_error", str(exc)) from exc
    if provider_kind in REMOTE_PROVIDER_KINDS:
        # Persist before validation: malformed paid responses still count toward spend.
        _persist_manual_text_provider_call(project, result, input_count=len(texts))
    vectors = result["vectors"]
    space_dim = int(space["dimension"])
    expected = len(terms) + len(excludes)
    # One vector per (term + exclude) — a wrong count is a provider contract violation.
    if len(vectors) != expected:
        raise SimilarityError(
            "embedding_provider_error",
            f"provider returned {len(vectors)} vectors for {expected} terms",
            field="anchor.terms",
        )
    # The declared dimension AND every vector width must match the space.
    if result["dimension"] != space_dim or any(len(v) != space_dim for v in vectors):
        raise SimilarityError(
            "embedding_space_mismatch",
            f"query embedding dimension {result.get('dimension')} does not match "
            f"space {space['id']} dimension {space_dim}",
            field="anchor.text",
        )
    term_vecs = vectors[: len(terms)]
    exclude_vecs = vectors[len(terms) :]
    composed = _compose_query_vector(term_vecs, [t["weight"] for t in terms], space_dim)
    # HARD NOT: drop rows whose cosine SCORE to an exclude concept >= its threshold.
    # query_similar returns (source_key, distance) nearest-first; score = 1 - distance,
    # so exclude when distance <= 1 - threshold — and we can stop at the first row past
    # the cutoff. These keys feed the existing _ranked_hits exclude set (removal).
    exclude_keys: set[str] = set()
    ready_items = backend.item_counts(index["id"]).get("ready", 0)
    for evec, espec in zip(exclude_vecs, excludes):
        max_distance = 1.0 - espec["threshold"]
        for source_key, distance in backend.query_similar(
            index["id"], space["id"], evec, k=ready_items
        ):
            if distance <= max_distance:
                exclude_keys.add(source_key)
            else:
                break
    return (
        composed,
        exclude_keys,
        QueryEmbeddingUse(
            provider_id=str(result["provider_id"]),
            provider_kind=str(result["provider_kind"]),
            actual_model_id=str(result["actual_model_id"]),
        ),
    )


def _persist_manual_text_provider_call(
    project: Project,
    result: dict[str, Any],
    *,
    input_count: int,
) -> None:
    """Write one remote manual-query batch as an unscoped neutral fact.

    These direct similarity/hybrid preview routes do not own an execution run
    or action receipt. The neutral ledger is still the authority for provider
    spend, and its writer commits the fact and matching project-key accrual in
    one transaction.
    """
    from frisket.ai.models.metadata import ModelCallMeta
    from frisket.engine.store.runs import RunResultStore

    units = dict(result.get("usage") or {})
    units.setdefault("input_count", input_count)
    fact = ModelCallMeta.provider_call(
        capability="llm.embed",
        engine=f"{result['provider_id']}/{result['actual_model_id']}",
        provider=result["provider_id"],
        provider_kind=result["provider_kind"],
        model_ids=[result["actual_model_id"]],
        credential_source=result.get("credential_source", "none"),
        provider_reported_cost_usd=result.get("provider_reported_cost_usd"),
        provider_cost_usd=result.get("provider_cost_usd"),
        units=units,
        cost_source=result.get("cost_source", "unknown"),
        request_id=result.get("provider_request_id"),
        duration_ms=None,
    ).as_dict()
    try:
        RunResultStore(project).write_unscoped_model_calls(
            [fact],
            row_id=None,
            column_id=None,
        )
    except Exception as exc:
        # Returning ranked rows would falsely imply the paid preview completed
        # cleanly while its accounting vanished. Refuse the response and name
        # the retry ambiguity; this legacy direct route has no idempotency key
        # with which to reconcile another request.
        raise SimilarityError(
            "embedding_accounting_failed",
            "the embedding provider returned a result, but its usage facts could "
            "not be recorded; retrying may make another paid provider call",
            field="anchor.text",
        ) from exc
