"""Public embedding backend resolution shared by retrieval and analytics."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, TypeAlias


EmbeddingResult: TypeAlias = list[list[float]] | dict[str, Any]
Embedder: TypeAlias = Callable[[list[str]], EmbeddingResult]
LocalEmbeddingCapability = Literal["providerless_classify"]

LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
LOCAL_IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"
RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
PROVIDERLESS_CLASSIFY_MODEL = "BAAI/bge-small-en-v1.5"
PROVIDERLESS_CLASSIFY_CAPABILITY = "providerless_classify"
PROVIDERLESS_CLASSIFY_ENABLE_ENV = "FRISKET_ENABLE_PROVIDERLESS_CLASSIFY"
PROVIDERLESS_CLASSIFY_THREADS_ENV = "FRISKET_PROVIDERLESS_CLASSIFY_THREADS"
PROVIDERLESS_CLASSIFY_MAX_THREADS = 8
REMOTE_EMBED_MODEL = "openai/text-embedding-3-small"

_FASTEMBED_ALIASES = {"paraphrase-multilingual-MiniLM-L12-v2": LOCAL_MODEL}
_local_models: dict[tuple[str, int | None, LocalEmbeddingCapability | None], Any] = {}
_local_image_models: dict[str, Any] = {}
_local_rerank_model: Any = None
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
        from huggingface_hub import snapshot_download

        from frisket.ai.models import artifact_manifest
        from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache

        entry = artifact_manifest.providerless_classify_artifact()
        if entry is None or entry.hf_snapshot is None:
            raise ProviderlessClassifierProvisionError(
                "Local semantic has no pinned model artifact."
            )
        source = entry.hf_snapshot
        try:
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
    """Resolve one lazy in-process FastEmbed text backend when installed."""

    runtime_env = os.environ if env is None else env
    providerless = capability == PROVIDERLESS_CLASSIFY_CAPABILITY
    providerless_opt_in = (
        providerless and runtime_env.get(PROVIDERLESS_CLASSIFY_ENABLE_ENV) == "1"
    )
    if capability is not None and capability != PROVIDERLESS_CLASSIFY_CAPABILITY:
        raise ValueError(f"unknown local embedding capability: {capability!r}")
    if providerless and model_id != PROVIDERLESS_CLASSIFY_MODEL:
        raise ValueError(
            f"{PROVIDERLESS_CLASSIFY_CAPABILITY} requires {PROVIDERLESS_CLASSIFY_MODEL}"
        )
    if (
        runtime_env.get("FRISKET_DISABLE_LOCAL_EMBED") == "1"
        and not providerless_opt_in
    ):
        return None

    threads: int | None = None
    if providerless:
        raw_threads = runtime_env.get(PROVIDERLESS_CLASSIFY_THREADS_ENV)
        if raw_threads is not None:
            try:
                threads = int(raw_threads)
            except ValueError as exc:
                raise ValueError(
                    f"{PROVIDERLESS_CLASSIFY_THREADS_ENV} must be an integer from "
                    f"1 to {PROVIDERLESS_CLASSIFY_MAX_THREADS}"
                ) from exc
            if not 1 <= threads <= PROVIDERLESS_CLASSIFY_MAX_THREADS:
                raise ValueError(
                    f"{PROVIDERLESS_CLASSIFY_THREADS_ENV} must be an integer from "
                    f"1 to {PROVIDERLESS_CLASSIFY_MAX_THREADS}"
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
            options: dict[str, Any] = {"local_files_only": True}
            if threads:
                options["threads"] = threads
            if providerless:
                options.update(
                    specific_model_path=str(_providerless_classify_snapshot()),
                )
            model = TextEmbedding(resolved, **options)
            _local_models[cache_key] = model
        return [vector.tolist() for vector in model.embed(texts)]

    return embed, f"fastembed/{resolved}"


def local_image_embedder(
    model_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[Callable[[list[Any]], list[list[float]]], str] | None:
    """Resolve one lazy in-process FastEmbed image backend when installed."""

    runtime_env = os.environ if env is None else env
    if runtime_env.get("FRISKET_DISABLE_LOCAL_EMBED") == "1":
        return None
    try:
        from fastembed import ImageEmbedding
        import PIL  # noqa: F401
    except ImportError:
        return None
    resolved = model_id or LOCAL_IMAGE_MODEL

    def embed(images: list[Any]) -> list[list[float]]:
        model = _local_image_models.get(resolved)
        if model is None:
            model = ImageEmbedding(resolved, local_files_only=True)
            _local_image_models[resolved] = model
        return [vector.tolist() for vector in model.embed(images)]

    return embed, f"fastembed/{resolved}"


def embedder_is_remote(model_id: str) -> bool:
    """Treat every backend except the known in-process FastEmbed ids as remote."""

    return not model_id.startswith("fastembed/")


def local_reranker(
    *, env: Mapping[str, str] | None = None
) -> Callable[[str, list[str]], list[float]] | None:
    """Resolve the cached local cross-encoder without downloading a model."""

    runtime_env = os.environ if env is None else env
    if (
        runtime_env.get("FRISKET_DISABLE_RERANK") == "1"
        or runtime_env.get("FRISKET_DISABLE_LOCAL_EMBED") == "1"
    ):
        return None
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
    except ImportError:
        return None

    def score(query: str, documents: list[str]) -> list[float]:
        global _local_rerank_model
        if _local_rerank_model is None:
            _local_rerank_model = TextCrossEncoder(RERANK_MODEL, local_files_only=True)
        return [float(value) for value in _local_rerank_model.rerank(query, documents)]

    return score


class RouterRemoteEmbedder:
    """Metadata-preserving router adapter for sync and async consumers."""

    embedding_dimension = 1536

    def __init__(self, router: Any) -> None:
        self._router = router

    async def embed_batch_async(self, texts: list[str]) -> EmbeddingResult:
        return await self._router.embed_batch(texts, model=REMOTE_EMBED_MODEL)

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return embedding_vectors(asyncio.run(self.embed_batch_async(texts)))


def resolve_embedder(
    router: Any = None, *, allow_remote: bool = False
) -> tuple[Embedder, str] | None:
    """Resolve local text embedding first, with explicit remote opt-in."""

    local = local_embedder()
    if local is not None:
        return local
    if allow_remote and router is not None and router.has_embedding_backend():
        return RouterRemoteEmbedder(router), REMOTE_EMBED_MODEL
    return None


def embedding_vectors(result: EmbeddingResult) -> list[list[float]]:
    """Project a metadata-rich embedding batch to its vectors."""

    if isinstance(result, dict):
        vectors = result.get("vectors")
        if not isinstance(vectors, list):
            raise RuntimeError("embedding provider returned no vectors")
        return vectors
    return result


__all__ = [
    "Embedder",
    "EmbeddingResult",
    "LOCAL_IMAGE_MODEL",
    "LOCAL_MODEL",
    "LocalEmbeddingCapability",
    "PROVIDERLESS_CLASSIFY_CAPABILITY",
    "PROVIDERLESS_CLASSIFY_ENABLE_ENV",
    "PROVIDERLESS_CLASSIFY_MAX_THREADS",
    "PROVIDERLESS_CLASSIFY_MODEL",
    "PROVIDERLESS_CLASSIFY_THREADS_ENV",
    "ProviderlessClassifierProvisionError",
    "REMOTE_EMBED_MODEL",
    "RERANK_MODEL",
    "RouterRemoteEmbedder",
    "embedder_is_remote",
    "embedding_vectors",
    "ensure_providerless_classifier_installed",
    "local_embedder",
    "local_image_embedder",
    "local_reranker",
    "resolve_embedder",
]
