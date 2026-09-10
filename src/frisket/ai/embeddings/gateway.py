"""Embedding gateway: resolve a metadata-rich embedder for a space's provider.

The refresh action calls ``gateway.embed(texts, provider=, model=, modality=)``
and gets an :class:`EmbeddingBatchResult` back — vectors plus the provider/model
facts the receipt and space need. This is a thin duck-typed seam: production uses
``EmbeddingGateway`` (local fastembed in-process, or remote via the router's
metadata-rich ``embed_batch``); tests inject a fake with the same ``embed``
shape and never touch a real model.

Only text/row payloads are embeddable in this slice. Media providers are in the
capability contract but raise ``EmbeddingBackendUnavailable`` here until their
engines land.
"""

from __future__ import annotations

from typing import Any, Literal

from .capabilities import EmbeddingBatchResult, build_batch_result

# Text providers known to L2-normalize their output (OpenAI/Gemini embeddings and
# the fastembed sentence-transformer models all return unit vectors). Used only
# to label the space honestly; unknown providers stay "none".
_L2_PROVIDERS = frozenset({"fastembed", "openai", "gemini"})
LocalEmbeddingCapability = Literal["providerless_classify"]


def provider_normalization(provider_id: str | None) -> str:
    """The normalization a provider applies, used to label a space honestly.
    Must agree between index_create (which records it on the space) and the
    gateway (which produces the vectors) so the same provider never forks
    spaces on a normalization mismatch."""
    return "l2" if provider_id in _L2_PROVIDERS else "none"


class EmbeddingBackendUnavailable(RuntimeError):
    """No usable embedder for the requested provider/model/modality."""


class EmbeddingProviderError(RuntimeError):
    """The embedder was reachable but failed to produce vectors."""


class EmbeddingGateway:
    """Default production gateway. ``router`` supplies remote providers."""

    def __init__(self, *, router: Any = None):
        self._router = router

    def embed(
        self,
        texts: list[str],
        *,
        provider: str | None,
        model: str | None,
        modality: str = "text",
        local_capability: LocalEmbeddingCapability | None = None,
    ) -> EmbeddingBatchResult:
        if local_capability is not None and (
            local_capability != "providerless_classify"
            or provider not in (None, "fastembed", "local")
            or modality != "text"
        ):
            raise ValueError(
                "providerless_classify is valid only for local text embeddings"
            )
        if modality == "image" and provider in (None, "fastembed", "local"):
            # REAL in-process image embeddings: fastembed's ImageEmbedding is light
            # ONNX (no torch). ``texts`` here carries resolved IMAGE INPUTS — on-disk
            # blob paths (str) or PIL images — threaded by the refresh handler, which
            # owns the project blob store the gateway lacks.
            return self._embed_local_image(texts, model)
        if modality not in ("text", "row"):
            # Other media (audio/video/file, or an image routed to the frisket-models
            # SIDECAR) belong in the frisket-models sidecar, where the heavy/different
            # models live — not in-process. The sidecar's embedding route is text-only
            # so far, so they are typed-unavailable until that route + the gateway
            # client land. Fail loud, never fake.
            raise EmbeddingBackendUnavailable(
                f"{modality!r} embeddings are served by the frisket-models sidecar, "
                f"which has no embedding route for this modality yet"
            )
        normalization = provider_normalization(provider)
        if provider in (None, "fastembed", "local"):
            return self._embed_local(
                texts,
                model,
                modality,
                normalization,
                local_capability=local_capability,
            )
        return self._embed_remote(texts, provider, model, modality)

    def _embed_local_image(
        self,
        images: list[Any],
        model: str | None,
    ) -> EmbeddingBatchResult:
        """In-process CLIP image embedding via fastembed.ImageEmbedding. ``images``
        are resolved image inputs (file paths / PIL images). Vectors are L2-labeled
        like other fastembed engines (CLIP image embeddings are unit-normalized)."""
        from frisket.semantic import local_image_embedder

        resolved = local_image_embedder(model if model else None)
        if resolved is None:
            raise EmbeddingBackendUnavailable(
                "local image embeddings are unavailable; FastEmbed is included "
                "with Frisket, so reinstall it or unset FRISKET_DISABLE_LOCAL_EMBED"
            )
        embed_fn, model_id = resolved
        try:
            vectors = embed_fn(list(images))
        except Exception as exc:  # noqa: BLE001 — provider/decoder failure is data
            raise EmbeddingProviderError(str(exc)) from exc
        return build_batch_result(
            vectors,
            provider_id="fastembed",
            provider_kind="local_process",
            requested_model=model or "local-image",
            actual_model_id=model_id,
            modality="image",
            normalization=provider_normalization("fastembed"),
            usage={"input_count": len(images)},
            provider_cost_usd=0.0,
            cost_source="free_local",
        )

    def _embed_local(
        self,
        texts: list[str],
        model: str | None,
        modality: str,
        normalization: str,
        *,
        local_capability: LocalEmbeddingCapability | None,
    ) -> EmbeddingBatchResult:
        from frisket.semantic import local_embedder

        resolved = (
            local_embedder(model)
            if local_capability is None
            else local_embedder(model, capability=local_capability)
        )
        if resolved is None:
            raise EmbeddingBackendUnavailable(
                "local text embeddings are unavailable; FastEmbed is included "
                "with Frisket, so reinstall it or unset FRISKET_DISABLE_LOCAL_EMBED"
            )
        embed_fn, model_id = resolved
        try:
            vectors = embed_fn(texts)
        except Exception as exc:  # noqa: BLE001 — provider failure is data
            raise EmbeddingProviderError(str(exc)) from exc
        return build_batch_result(
            vectors,
            provider_id="fastembed",
            provider_kind="local_process",
            requested_model=model or "local",
            actual_model_id=model_id,
            modality=modality,
            normalization=normalization,
            usage={"input_count": len(texts)},
            provider_cost_usd=0.0,
            cost_source="free_local",
        )

    def _embed_remote(
        self,
        texts: list[str],
        provider: str,
        model: str | None,
        modality: str,
    ) -> EmbeddingBatchResult:
        if self._router is None:
            raise EmbeddingBackendUnavailable(
                f"no router configured for remote provider {provider!r}"
            )
        import asyncio

        model_ref = f"{provider}/{model}" if model else provider
        try:
            return asyncio.run(
                self._router.embed_batch(texts, model=model_ref, modality=modality)
            )
        except EmbeddingBackendUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — provider failure is data
            raise EmbeddingProviderError(str(exc)) from exc
