"""Semantic-window topic segmentation using only Frisket's local embedder.

The algorithm is a small first-party implementation of DeepTiling's useful
core: embed ordered dialogue units, compare the mean embeddings of local left
and right windows at every gap, calculate TextTiling valley depths, and retain
the strongest separated valleys.  The external DeepTiling repository is a
research reference, not a runtime dependency.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, Protocol

import numpy as np
from pydantic import JsonValue

from frisket.ai.embeddings import (
    EmbeddingBackendUnavailable,
    EmbeddingProviderError,
)
from frisket.semantic import LOCAL_MODEL

from .contracts import (
    BetweenUnits,
    BoundaryCandidate,
    EngineDefinition,
    PreflightResult,
    SegmentationContext,
    SegmentationEngineUnavailable,
    SegmentationExecutionError,
    SegmentationResult,
    SegmentationSnapshot,
    validate_detail_settings,
)
from .texttiling import _depth_cutoff, _depth_scores, _select_local_maxima


_MIN_UNITS = 6
_WINDOW_UNITS = 5
_BOUNDARY_CLIP = 2


class _EmbeddingGateway(Protocol):
    def embed(
        self,
        texts: list[str],
        *,
        provider: str | None,
        model: str | None,
        modality: str = "text",
    ) -> Mapping[str, Any]: ...


AvailabilityProbe = Callable[[], tuple[bool, str | None]]
GatewayFactory = Callable[[], _EmbeddingGateway]


def _local_embedding_availability() -> tuple[bool, str | None]:
    if os.environ.get("FRISKET_DISABLE_LOCAL_EMBED") == "1":
        return False, "Local embeddings are disabled by FRISKET_DISABLE_LOCAL_EMBED."
    try:
        installed = importlib.util.find_spec("fastembed") is not None
    except (ImportError, ValueError):
        installed = False
    if not installed:
        return (
            False,
            "Semantic windows requires the bundled FastEmbed runtime; reinstall Frisket.",
        )
    return True, None


def _default_gateway() -> _EmbeddingGateway:
    # No router is supplied. Even before the explicit provider argument below,
    # this prevents this engine from gaining a remote fallback by composition.
    from frisket.ai.embeddings import EmbeddingGateway

    return EmbeddingGateway()


def _vector_cosine(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(left, right) / denominator)


def _semantic_cohesion(vectors: np.ndarray, window: int) -> list[float]:
    scores: list[float] = []
    for gap in range(vectors.shape[0] - 1):
        left = vectors[max(0, gap - window + 1) : gap + 1].mean(axis=0)
        right = vectors[gap + 1 : min(vectors.shape[0], gap + 1 + window)].mean(axis=0)
        scores.append(_vector_cosine(left, right))
    return scores


def _validated_vectors(batch: Mapping[str, Any], unit_count: int) -> np.ndarray:
    if (
        batch.get("provider_id") != "fastembed"
        or batch.get("provider_kind") != "local_process"
    ):
        raise SegmentationExecutionError(
            "semantic windows refused a non-local embedding result"
        )
    try:
        vectors = np.asarray(batch["vectors"], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise SegmentationExecutionError(
            "local embedding result did not contain numeric vectors"
        ) from exc
    if vectors.ndim != 2 or vectors.shape[0] != unit_count or vectors.shape[1] == 0:
        raise SegmentationExecutionError(
            "local embedding result shape does not match the transcript units"
        )
    if not bool(np.isfinite(vectors).all()):
        raise SegmentationExecutionError(
            "local embedding result contains nonfinite values"
        )
    return vectors


def _embedding_diagnostics(batch: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Keep model identity and usage, never vectors or provider credentials."""

    usage = batch.get("usage")
    return {
        "provider_id": str(batch.get("provider_id") or "fastembed"),
        "provider_kind": str(batch.get("provider_kind") or "local_process"),
        "requested_model": (
            str(batch["requested_model"])
            if batch.get("requested_model") is not None
            else None
        ),
        "actual_model_id": str(batch.get("actual_model_id") or "unknown"),
        "model_revision": str(batch.get("model_revision") or "unknown"),
        "dimension": int(batch.get("dimension") or 0),
        "normalization": str(batch.get("normalization") or "none"),
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "fact_version": str(batch.get("fact_version") or "unknown"),
    }


class DeepTilingSegmenter:
    """Multilingual semantic segmentation through a pinned local model."""

    _catalog_definition = EngineDefinition(
        id="deep_tiling",
        version="1",
        label="Semantic windows",
        description=(
            "Find topic shifts by comparing multilingual local embeddings "
            "across nearby transcript units."
        ),
        recommended=True,
    )

    def __init__(
        self,
        *,
        gateway: _EmbeddingGateway | None = None,
        gateway_factory: GatewayFactory | None = None,
        availability_probe: AvailabilityProbe | None = None,
        model_id: str = LOCAL_MODEL,
    ) -> None:
        if gateway is not None and gateway_factory is not None:
            raise ValueError("provide a gateway or gateway_factory, not both")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("semantic windows model_id must be non-empty")
        self._injected_gateway = gateway
        self._dependency_injected = gateway is not None or gateway_factory is not None
        self._gateway_factory = gateway_factory or _default_gateway
        self._availability_probe = availability_probe or _local_embedding_availability
        self._model_id = model_id

    @property
    def catalog_definition(self) -> EngineDefinition:
        """Return stable identity metadata without probing this host runtime."""

        return self._catalog_definition

    @property
    def definition(self) -> EngineDefinition:
        # An injected gateway is an explicit test/composition seam and does not
        # depend on the host having fastembed importable.
        available, error = (
            (True, None) if self._dependency_injected else self._availability_probe()
        )
        return replace(
            self.catalog_definition,
            available=available,
            error=error,
        )

    def validate_settings(
        self,
        settings: Mapping[str, JsonValue] | None,
    ) -> Mapping[str, JsonValue]:
        return validate_detail_settings(settings)

    def preflight(
        self,
        snapshot: SegmentationSnapshot,
        settings: Mapping[str, JsonValue] | None,
    ) -> PreflightResult:
        self.validate_settings(settings)
        definition = self.definition
        if not definition.available:
            return PreflightResult.failed(
                "engine_unavailable",
                definition.error or "Semantic windows is unavailable.",
                engine_id=definition.id,
            )
        nonempty_units = sum(bool(unit.text.strip()) for unit in snapshot.units)
        if nonempty_units < _MIN_UNITS or nonempty_units != len(snapshot.units):
            return PreflightResult.failed(
                "insufficient_text",
                (
                    f"Semantic windows needs at least {_MIN_UNITS} non-empty "
                    "transcript units."
                ),
                unit_count=len(snapshot.units),
                nonempty_unit_count=nonempty_units,
                minimum_unit_count=_MIN_UNITS,
            )
        return PreflightResult.passed(
            unit_count=len(snapshot.units),
            language=snapshot.language,
        )

    def _gateway(self) -> _EmbeddingGateway:
        if self._injected_gateway is not None:
            return self._injected_gateway
        return self._gateway_factory()

    def segment(
        self,
        snapshot: SegmentationSnapshot,
        settings: Mapping[str, JsonValue] | None,
        context: SegmentationContext,
    ) -> SegmentationResult:
        resolved_settings = self.validate_settings(settings)
        preflight = self.preflight(snapshot, resolved_settings)
        if not preflight.ok and preflight.error_code == "engine_unavailable":
            raise SegmentationEngineUnavailable(
                preflight.message or "Semantic windows is unavailable."
            )
        preflight.require()
        context.raise_if_cancelled()

        try:
            batch = self._gateway().embed(
                [unit.text for unit in snapshot.units],
                provider="fastembed",
                model=self._model_id,
                modality="text",
            )
        except EmbeddingBackendUnavailable as exc:
            raise SegmentationEngineUnavailable(str(exc)) from exc
        except EmbeddingProviderError as exc:
            raise SegmentationExecutionError(
                f"local semantic embedding failed: {exc}"
            ) from exc
        context.raise_if_cancelled()

        vectors = _validated_vectors(batch, len(snapshot.units))
        scores = _semantic_cohesion(vectors, _WINDOW_UNITS)
        depths = _depth_scores(scores, clip=_BOUNDARY_CLIP)
        detail = str(resolved_settings["detail"])
        cutoff = _depth_cutoff(depths, detail, semantic=True)
        selected = _select_local_maxima(depths, cutoff=cutoff)

        candidates: list[BoundaryCandidate] = []
        for gap in selected:
            context.raise_if_cancelled()
            candidates.append(
                BoundaryCandidate(
                    id=f"deep-tiling-gap-{gap:04d}",
                    locator=BetweenUnits(
                        left_unit_id=snapshot.units[gap].id,
                        right_unit_id=snapshot.units[gap + 1].id,
                    ),
                    strength=float(depths[gap]),
                    diagnostics={
                        "unit_gap": gap,
                        "cohesion": float(scores[gap]),
                        "cutoff": cutoff,
                    },
                )
            )

        context.raise_if_cancelled()
        return SegmentationResult(
            engine_id=self.definition.id,
            engine_version=self.definition.version,
            boundaries=tuple(candidates),
            resolved_settings=resolved_settings,
            diagnostics={
                "algorithm": "deep_tiling",
                "unit_count": len(snapshot.units),
                "window_units": _WINDOW_UNITS,
                "cohesion_gap_count": len(scores),
                "cutoff": cutoff,
                "embedding": _embedding_diagnostics(batch),
            },
        )


__all__ = ["DeepTilingSegmenter"]
