"""Semantic (meaning-ranked) search over the live text cells.

Real meaning ranking embeds the query and every candidate cell into the same
learned vector
space and ranks by cosine similarity, so "car trouble" ranks "the automobile
would not start" above an unrelated row with zero shared keywords.

Backend resolution, best first:

1. **Local model** (base install → fastembed/ONNX).
   Default-on in the Docker image. Keeps cell text on the user's machine —
   the right default for a tool whose users have sensitive sources.
2. **API embeddings** via the model router (openai/gemini key configured).
3. **Honest lexical fallback**: no backend at all → FTS, every hit flagged
   ``semantic: False``. Never fabricate meaning with hand-wired synonym tables.

Cell vectors are cached in the rebuildable search sidecar, keyed by
``sha1(model_id + content)`` — content-addressed, so edits re-embed only what
changed and switching backends never serves stale vectors. A query embeds only
itself; the corpus is embedded once.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import sqlite3
import threading
from array import array
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from frisket.search import (
    RERANK_POOL,
    _sidecar,
    column_ai_flags,
    fresh_sidecar,
    rerank_hits,
    search_project,
)
from frisket.engine.store import Project

EmbeddingResult = list[list[float]] | dict[str, Any]
Embedder = Callable[[list[str]], EmbeddingResult]

# Multilingual by default: journalists monitor non-English sources (the
# Manosphere pattern won't stay English-only). ~50 languages, cross-lingual
# (a Spanish query ranks English cells and vice versa), 0.22GB.
LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
PROVIDERLESS_CLASSIFY_MODEL = "BAAI/bge-small-en-v1.5"
PROVIDERLESS_CLASSIFY_CAPABILITY = "providerless_classify"
PROVIDERLESS_CLASSIFY_ENABLE_ENV = "FRISKET_ENABLE_PROVIDERLESS_CLASSIFY"
PROVIDERLESS_CLASSIFY_THREADS_ENV = "FRISKET_PROVIDERLESS_CLASSIFY_THREADS"
PROVIDERLESS_CLASSIFY_MAX_THREADS = 8

LocalEmbeddingCapability = Literal["providerless_classify"]

# Lazy fastembed.TextEmbedding per (model id, explicit thread bound, capability).
# The capability is part of runtime identity so providerless classification can
# never reuse a generic model instance that was not bound to its pinned snapshot.
_local_models: dict[tuple[str, int | None, LocalEmbeddingCapability | None], Any] = {}
# the catalog default carries the short id; fastembed wants the full HF path.
_FASTEMBED_ALIASES = {"paraphrase-multilingual-MiniLM-L12-v2": LOCAL_MODEL}
_providerless_classify_install_lock = threading.Lock()


class ProviderlessClassifierProvisionError(RuntimeError):
    """The pinned local classifier could not be installed on first use."""


def _providerless_classify_snapshot() -> Path:
    from frisket.ai.models import artifact_manifest
    from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache

    entry = artifact_manifest.providerless_classify_artifact()
    if entry is None or entry.hf_snapshot is None:
        raise RuntimeError("providerless classifier has no pinned artifact")
    source = entry.hf_snapshot
    snapshot = (
        huggingface_hub_cache()
        / f"models--{source.repo_id.replace('/', '--')}"
        / "snapshots"
        / source.revision
    )
    missing = [name for name in source.files if not (snapshot / name).is_file()]
    if missing:
        raise RuntimeError(
            f"pinned providerless classifier snapshot {source.revision} is not "
            f"installed; missing {', '.join(missing)}"
        )
    return snapshot


def ensure_providerless_classifier_installed() -> Path:
    """Return the pinned BGE snapshot, downloading it once when absent."""

    try:
        return _providerless_classify_snapshot()
    except RuntimeError:
        pass

    with _providerless_classify_install_lock:
        try:
            return _providerless_classify_snapshot()
        except RuntimeError:
            pass

        from frisket.ai.models import artifact_manifest
        from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache

        entry = artifact_manifest.providerless_classify_artifact()
        if entry is None or entry.hf_snapshot is None:
            raise ProviderlessClassifierProvisionError(
                "Local semantic has no pinned model artifact."
            )
        source = entry.hf_snapshot
        try:
            from huggingface_hub import snapshot_download

            snapshot_download(
                source.repo_id,
                revision=source.revision,
                cache_dir=huggingface_hub_cache(),
                allow_patterns=list(source.files),
                token=False,
            )
            return _providerless_classify_snapshot()
        except Exception as exc:
            raise ProviderlessClassifierProvisionError(
                "The Local semantic model could not be downloaded. Check the "
                "network connection and retry."
            ) from exc


def local_embedder(
    model_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    capability: LocalEmbeddingCapability | None = None,
) -> tuple[Embedder, str] | None:
    """The in-process embedding backend for a CHOSEN fastembed model (default when
    None), when the bundled FastEmbed runtime is available. Returns (embed_fn,
    model_id) or None. The picked model is HONORED here (previously hardcoded). Set
    FRISKET_DISABLE_LOCAL_EMBED=1 to force the API/fallback path (ops escape hatch;
    also how tests exercise the non-local paths deterministically).

    The sole exception is the explicit ``providerless_classify`` capability
    together with ``FRISKET_ENABLE_PROVIDERLESS_CLASSIFY=1``. It is deliberately
    typed and model-fixed: hosted editions can offer this one bounded recipe
    without making generic embedding/search/image consumers available.
    """
    runtime_env = os.environ if env is None else env
    providerless_classify = capability == PROVIDERLESS_CLASSIFY_CAPABILITY
    providerless_opt_in = (
        providerless_classify
        and runtime_env.get(PROVIDERLESS_CLASSIFY_ENABLE_ENV) == "1"
    )
    if capability is not None and capability != PROVIDERLESS_CLASSIFY_CAPABILITY:
        raise ValueError(f"unknown local embedding capability: {capability!r}")
    if capability == PROVIDERLESS_CLASSIFY_CAPABILITY and (
        model_id != PROVIDERLESS_CLASSIFY_MODEL
    ):
        raise ValueError(
            f"{PROVIDERLESS_CLASSIFY_CAPABILITY} requires {PROVIDERLESS_CLASSIFY_MODEL}"
        )
    if (
        runtime_env.get("FRISKET_DISABLE_LOCAL_EMBED") == "1"
        and not providerless_opt_in
    ):
        return None

    threads: int | None = None
    if providerless_classify:
        raw_threads = runtime_env.get(PROVIDERLESS_CLASSIFY_THREADS_ENV)
        if raw_threads is not None:
            try:
                threads = int(raw_threads)
            except ValueError as exc:
                raise ValueError(
                    f"{PROVIDERLESS_CLASSIFY_THREADS_ENV} must be an integer "
                    f"from 1 to {PROVIDERLESS_CLASSIFY_MAX_THREADS}"
                ) from exc
            if not 1 <= threads <= PROVIDERLESS_CLASSIFY_MAX_THREADS:
                raise ValueError(
                    f"{PROVIDERLESS_CLASSIFY_THREADS_ENV} must be an integer "
                    f"from 1 to {PROVIDERLESS_CLASSIFY_MAX_THREADS}"
                )
    try:
        from fastembed import TextEmbedding
    except ImportError:
        return None

    resolved = _FASTEMBED_ALIASES.get(model_id or "", model_id) or LOCAL_MODEL

    def embed(texts: list[str]) -> list[list[float]]:
        cache_key = (resolved, threads, capability)
        model = _local_models.get(cache_key)
        if model is None:
            options: dict[str, Any] = {"threads": threads} if threads else {}
            if providerless_classify:
                options.update(
                    specific_model_path=str(_providerless_classify_snapshot()),
                    local_files_only=True,
                )
            model = TextEmbedding(resolved, **options)
            _local_models[cache_key] = model
        return [v.tolist() for v in model.embed(texts)]

    return embed, f"fastembed/{resolved}"


# IMAGE is the one media modality embedded IN-PROCESS: fastembed's ImageEmbedding is
# light ONNX (onnxruntime, NO torch — same class as the text embedder), so the CLIP
# image tower runs on the box like local text. Audio/video/file still need heavy or
# different models and stay disabled-with-reason (see gateway.py / capabilities.py).
LOCAL_IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"  # CLIP ViT-B/32 vision tower, 512-d
_local_image_models: dict[str, Any] = {}  # lazy fastembed.ImageEmbedding per model id

# fastembed.ImageEmbedding accepts file paths or PIL.Image inputs. A path may be a
# str or pathlib.Path; a PIL image is a duck-typed object — both pass straight
# through, so callers can hand it whatever the blob resolver produced.
ImageInput = Any
ImageEmbedder = Callable[[list["ImageInput"]], list[list[float]]]


def local_image_embedder(
    model_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[ImageEmbedder, str] | None:
    """The in-process IMAGE embedding backend (default ``Qdrant/clip-ViT-B-32-vision``,
    512-d), if fastembed's ``ImageEmbedding`` and PIL are installed. Returns
    (embed_fn, model_id) or None. ``embed_fn`` takes IMAGE INPUTS — on-disk file
    paths (str/Path) or PIL images — and returns real CLIP-ONNX vectors. Mirrors
    ``local_embedder`` (lazy per-model, content-neutral). Set
    FRISKET_DISABLE_LOCAL_EMBED=1 to force this off (the same ops/test escape hatch
    the text embedder honors)."""
    runtime_env = os.environ if env is None else env
    if runtime_env.get("FRISKET_DISABLE_LOCAL_EMBED") == "1":
        return None
    try:
        from fastembed import ImageEmbedding  # light ONNX, no torch
        import PIL  # noqa: F401 — fastembed loads images via Pillow; require it too
    except ImportError:
        return None

    resolved = model_id or LOCAL_IMAGE_MODEL

    def embed(images: list[ImageInput]) -> list[list[float]]:
        model = _local_image_models.get(resolved)
        if model is None:
            model = _local_image_models[resolved] = ImageEmbedding(resolved)
        return [v.tolist() for v in model.embed(images)]

    return embed, f"fastembed/{resolved}"


# NOTE: audio/video/file embeddings are NOT done in-process — they belong in the
# frisket-models sidecar (heavy models live there).


# The one remote fallback resolve_embedder can hand out. Named so cost
# estimators (SemanticJoinRecipe.estimate) price the SAME model execution
# would use instead of guessing.
REMOTE_EMBED_MODEL = "openai/text-embedding-3-small"


def embedder_is_remote(model_id: str) -> bool:
    """Fail-closed billing classification for a resolved embedder id: only
    the in-process fastembed backends are known-free; any other id (the
    router's remote API model, injected/unknown backends) is treated as
    remote/billable, so gates that consult this can never price an unknown
    backend as free."""
    return not model_id.startswith("fastembed/")


def resolve_embedder(
    router: Any = None, *, allow_remote: bool = False
) -> tuple[Embedder, str] | None:
    """Best available embedding backend: local model > API (opt-in) > None.

    The local -> remote-API fallback used to be implicit —
    whether a run billed a provider was inferred from whether fastembed was
    installed. Remote now requires the caller to opt in with
    ``allow_remote=True``, and an opting-in caller owns surfacing the spend
    through its cost gate BEFORE any embed call
    (``SemanticJoinRecipe.estimate`` is the pattern: a non-local backend
    estimates ``cost: None``, which forces the explicit confirmation)."""
    local = local_embedder()
    if local is not None:
        return local
    if allow_remote and router is not None and router.has_embedding_backend():
        return _RouterRemoteEmbedder(router), REMOTE_EMBED_MODEL
    return None


class _RouterRemoteEmbedder:
    """Compatibility bridge for the legacy synchronous semantic consumers.

    ``join.semantic`` already runs inside MapRunner's event loop, so it uses
    :meth:`embed_batch_async` and retains the provider's complete batch fact.
    Older synchronous consumers can keep calling this object and receive bare
    vectors; their own accounting migrations are deliberately separate.
    """

    def __init__(self, router: Any) -> None:
        self._router = router

    async def embed_batch_async(self, texts: list[str]) -> EmbeddingResult:
        return await self._router.embed_batch(texts, model=REMOTE_EMBED_MODEL)

    def __call__(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        # Synchronous endpoints run in worker threads, with no live loop.
        result = asyncio.run(self.embed_batch_async(texts))
        return _embedding_vectors(result)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    if dot == 0:
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _corpus(project: Project) -> list[dict[str, Any]]:
    """Indexed live text cells (reuses the rebuildable FTS sidecar)."""
    db = fresh_sidecar(project)
    rows = db.execute(
        "SELECT content, sheet_id, row_id, column_id, column_name FROM cell_fts"
    ).fetchall()
    db.close()
    ai_by_column = column_ai_flags(project)
    out = []
    for r in rows:
        hit = dict(r)
        hit["ai_generated"] = ai_by_column.get(int(hit["column_id"]), False)
        out.append(hit)
    return out


def _vec_key(model_id: str, content: str) -> str:
    return hashlib.sha1(
        (model_id + "\0" + content).encode("utf-8", "replace")
    ).hexdigest()


def _cached_vectors(db: sqlite3.Connection, keys: list[str]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    CHUNK = 500  # stay under SQLite's bound-parameter cap
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i : i + CHUNK]
        q = ",".join("?" * len(chunk))
        for r in db.execute(f"SELECT key, vec FROM cell_vec WHERE key IN ({q})", chunk):
            a = array("f")
            a.frombytes(r["vec"])
            out[r["key"]] = list(a)
    return out


def _doc_vectors(
    project: Project,
    corpus: list[dict[str, Any]],
    embed: Embedder,
    model_id: str,
) -> list[list[float]]:
    """Vectors for every corpus cell, via the sidecar cache. Only cells whose
    (model, content) pair is unseen get embedded — the content-addressed key
    makes edits incremental and model switches stale-proof."""
    keys = [_vec_key(model_id, r["content"]) for r in corpus]
    db = _sidecar(project)
    cached = _cached_vectors(db, keys)
    missing_idx = [i for i, k in enumerate(keys) if k not in cached]
    if missing_idx:
        result = embed([corpus[i]["content"] for i in missing_idx])
        fresh = _embedding_vectors(result)
        _refuse_misaligned_batch(missing_idx, fresh)
        rows = []
        for i, vec in zip(missing_idx, fresh, strict=True):
            cached[keys[i]] = vec
            rows.append((keys[i], array("f", vec).tobytes()))
        db.executemany("INSERT OR REPLACE INTO cell_vec (key, vec) VALUES (?, ?)", rows)
        db.commit()
    db.close()
    return [cached[k] for k in keys]


def _refuse_misaligned_batch(missing_idx: list[int], fresh: list[list[float]]) -> None:
    """Refuse a batch whose vector count disagrees with its input count.

    A provider that returns a different number of vectors than it was handed
    inputs has FAILED the batch, not partially succeeded: nothing in the
    response says WHICH input each surviving vector belongs to, so binding
    them positionally shifts every vector after the gap onto the wrong
    document. That mistake is permanent. The cache key is
    ``sha1(model_id + content)``, so a misaligned write is content-addressed
    to the wrong content forever, and ``rebuild_index`` clears only
    ``cell_fts`` — the vector cache deliberately survives FTS rebuilds, so it
    never self-heals. Refuse here, before a single row is written.
    """

    if len(fresh) != len(missing_idx):
        raise RuntimeError(
            "embedding provider returned "
            f"{len(fresh)} vectors for {len(missing_idx)} inputs — refusing "
            "the batch rather than caching vectors bound to the wrong "
            "documents"
        )


def _embedding_vectors(result: EmbeddingResult) -> list[list[float]]:
    """Project a dual-shape embedding response to its vectors.

    Local and legacy injected embedders return vectors directly. Remote
    bridges can return the metadata-rich batch mapping so a run-scoped caller
    records the paid provider fact before this projection discards it.
    """

    if isinstance(result, dict):
        vectors = result.get("vectors")
        if not isinstance(vectors, list):
            raise RuntimeError("embedding provider returned no vectors")
        return vectors
    return result


async def _doc_vectors_async(
    project: Project,
    corpus: list[dict[str, Any]],
    embed: Embedder,
    model_id: str,
    *,
    before_fresh_batch: Callable[[list[str]], None] | None = None,
    on_fresh_batch: Callable[[EmbeddingResult, list[str]], None] | None = None,
) -> list[list[float]]:
    """Async twin of :func:`_doc_vectors` for MapRunner-native embedders.

    The callback runs after provider success but before the sidecar is
    populated. A paid call therefore becomes durable before its vectors can
    turn a crash replay into a cache hit that would otherwise erase the call.
    """

    keys = [_vec_key(model_id, row["content"]) for row in corpus]
    db = _sidecar(project)
    try:
        cached = _cached_vectors(db, keys)
        missing_idx = [i for i, key in enumerate(keys) if key not in cached]
        if missing_idx:
            texts = [corpus[i]["content"] for i in missing_idx]
            if before_fresh_batch is not None:
                before_fresh_batch(texts)
            async_embed = getattr(embed, "embed_batch_async", None)
            result = async_embed(texts) if callable(async_embed) else embed(texts)
            if inspect.isawaitable(result):
                result = await result
            if on_fresh_batch is not None:
                on_fresh_batch(result, texts)
            # AFTER the paid fact, never before it: a provider that answered
            # with malformed vectors still answered, and still
            # billed. Recording the call and then refusing keeps the ledger
            # honest; refusing first would eat a real charge silently.
            fresh = _embedding_vectors(result)
            _refuse_misaligned_batch(missing_idx, fresh)
            rows = []
            for i, vec in zip(missing_idx, fresh, strict=True):
                cached[keys[i]] = vec
                rows.append((keys[i], array("f", vec).tobytes()))
            db.executemany(
                "INSERT OR REPLACE INTO cell_vec (key, vec) VALUES (?, ?)", rows
            )
            db.commit()
        return [cached[key] for key in keys]
    finally:
        db.close()


def semantic_search(
    project: Project,
    query: str,
    limit: int = 50,
    embed: Embedder | None = None,
    embed_id: str | None = None,
    rerank: str = "auto",
) -> list[dict[str, Any]]:
    """Rank live text cells by meaning-similarity to ``query``.

    With an ``embed`` backend: embed the query, rank the corpus by cosine
    (corpus vectors come from the sidecar cache when ``embed_id`` is given).
    Without one: honest lexical fallback (FTS), every hit flagged
    ``semantic: False`` — no faked meaning.

    Either way the cross-encoder second stage (frisket.search.rerank_hits)
    reorders the top candidates unless ``rerank="off"``; it never drops hits
    and falls back to first-stage order when no rerank backend resolves. Its
    flat-profile guard keeps the multilingual cosine order for non-English
    queries the English-only cross-encoder can't actually read.
    """
    if embed is None:
        hits = search_project(project, query, limit=limit, rerank=rerank)
        for h in hits:
            h["semantic"] = False
        return hits

    corpus = _corpus(project)
    if not corpus:
        return []

    if embed_id:
        qvec = embed([query])[0]
        doc_vecs = _doc_vectors(project, corpus, embed, embed_id)
    else:
        # no cache key — one batched call: [query, doc0, doc1, ...]
        vectors = embed([query] + [r["content"] for r in corpus])
        qvec, doc_vecs = vectors[0], vectors[1:]

    scored: list[tuple[float, dict[str, Any], str]] = []
    for r, dvec in zip(corpus, doc_vecs, strict=False):
        score = _cosine(qvec, dvec)
        if score <= 0:
            continue
        scored.append(
            (
                score,
                {
                    "sheet_id": r["sheet_id"],
                    "row_id": r["row_id"],
                    "column_id": r["column_id"],
                    "column_name": r["column_name"],
                    "ai_generated": r.get("ai_generated", False),
                    "snip": r["content"][:200],
                    "score": round(score, 4),
                    "semantic": True,
                },
                r["content"],
            )
        )
    scored.sort(key=lambda t: t[0], reverse=True)
    if rerank == "off":
        return [h for _, h, _ in scored[:limit]]
    top = scored[: max(limit, RERANK_POOL)]
    hits = rerank_hits(query, [h for _, h, _ in top], [c for _, _, c in top])
    return hits[:limit]
