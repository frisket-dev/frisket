from __future__ import annotations

from typing import Any

import pytest

from frisket.ai.embeddings import EmbeddingBackendUnavailable
from frisket.ai.embeddings.capabilities import build_batch_result
from frisket.semantic import LOCAL_MODEL
from frisket.features.topic_segmentation import (
    BetweenUnits,
    DeepTilingSegmenter,
    DialogueUnit,
    SegmentationCancelled,
    SegmentationContext,
    SegmentationEngineUnavailable,
    SegmentationExecutionError,
    SegmentationSnapshot,
)


def _snapshot(count: int = 8, *, language: str | None = "fr") -> SegmentationSnapshot:
    return SegmentationSnapshot(
        snapshot_hash="sha256:semantic-fixture",
        source_kind="untimed_transcript",
        language=language,
        units=tuple(
            DialogueUnit(id=f"u{index}", ordinal=index, text=f"unit {index}")
            for index in range(count)
        ),
    )


class _FakeGateway:
    def __init__(
        self,
        vectors: list[list[float]],
        *,
        provider_id: str = "fastembed",
        provider_kind: str = "local_process",
    ) -> None:
        self.vectors = vectors
        self.provider_id = provider_id
        self.provider_kind = provider_kind
        self.calls: list[dict[str, Any]] = []

    def embed(self, texts: list[str], **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"texts": texts, **kwargs})
        return build_batch_result(
            self.vectors,
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            requested_model=kwargs.get("model"),
            actual_model_id="fastembed/test-multilingual-model",
            normalization="l2",
            usage={"input_count": len(texts)},
        )


def test_deep_tiling_batches_local_embeddings_and_finds_the_semantic_gap() -> None:
    gateway = _FakeGateway([[1.0, 0.0]] * 4 + [[0.0, 1.0]] * 4)
    engine = DeepTilingSegmenter(gateway=gateway)
    snapshot = _snapshot(language="es")

    assert engine.preflight(snapshot, {}).ok
    result = engine.segment(snapshot, {}, SegmentationContext())

    assert gateway.calls == [
        {
            "texts": [f"unit {index}" for index in range(8)],
            "provider": "fastembed",
            "model": LOCAL_MODEL,
            "modality": "text",
        }
    ]
    assert tuple(candidate.locator for candidate in result.boundaries) == (
        BetweenUnits(left_unit_id="u3", right_unit_id="u4"),
    )
    embedding = result.diagnostics["embedding"]
    assert embedding["actual_model_id"] == "fastembed/test-multilingual-model"
    assert embedding["dimension"] == 2
    assert "vectors" not in embedding


def test_deep_tiling_balanced_keeps_two_equally_clear_topic_shifts() -> None:
    gateway = _FakeGateway(
        [[1.0, 0.0, 0.0]] * 4 + [[0.0, 1.0, 0.0]] * 4 + [[0.0, 0.0, 1.0]] * 4
    )

    result = DeepTilingSegmenter(gateway=gateway).segment(
        _snapshot(12), {"detail": "balanced"}, SegmentationContext()
    )

    assert tuple(candidate.locator for candidate in result.boundaries) == (
        BetweenUnits(left_unit_id="u3", right_unit_id="u4"),
        BetweenUnits(left_unit_id="u7", right_unit_id="u8"),
    )


def test_deep_tiling_detail_is_monotonic_and_does_not_change_models() -> None:
    snapshot = _snapshot()
    engine = DeepTilingSegmenter(
        gateway=_FakeGateway([[1.0, 0.0]] * 4 + [[0.0, 1.0]] * 4)
    )

    results = {
        detail: engine.segment(snapshot, {"detail": detail}, SegmentationContext())
        for detail in ("fewer", "balanced", "more")
    }

    assert (
        len(results["fewer"].boundaries)
        <= len(results["balanced"].boundaries)
        <= len(results["more"].boundaries)
    )
    assert all(
        result.diagnostics["embedding"]["requested_model"] == LOCAL_MODEL
        for result in results.values()
    )


def test_deep_tiling_rejects_a_gateway_that_returns_remote_facts() -> None:
    gateway = _FakeGateway(
        [[1.0, 0.0]] * 8,
        provider_id="openai",
        provider_kind="platform_api",
    )

    with pytest.raises(SegmentationExecutionError, match="non-local"):
        DeepTilingSegmenter(gateway=gateway).segment(
            _snapshot(), {}, SegmentationContext()
        )


def test_deep_tiling_surfaces_a_missing_local_backend_without_fallback() -> None:
    class MissingGateway:
        def embed(self, texts: list[str], **kwargs: Any) -> dict[str, Any]:
            raise EmbeddingBackendUnavailable("fastembed is not installed")

    with pytest.raises(SegmentationEngineUnavailable, match="fastembed"):
        DeepTilingSegmenter(gateway=MissingGateway()).segment(
            _snapshot(), {}, SegmentationContext()
        )


def test_deep_tiling_preflight_requires_enough_nonempty_units() -> None:
    engine = DeepTilingSegmenter(gateway=_FakeGateway([[1.0, 0.0]] * 5))

    result = engine.preflight(_snapshot(5, language=None), {})

    assert not result.ok
    assert result.error_code == "insufficient_text"


def test_deep_tiling_gateway_factory_is_a_complete_test_seam() -> None:
    gateway = _FakeGateway([[1.0, 0.0]] * 4 + [[0.0, 1.0]] * 4)
    engine = DeepTilingSegmenter(
        gateway_factory=lambda: gateway,
        availability_probe=lambda: (False, "host dependency deliberately absent"),
    )

    assert engine.definition.available
    assert (
        engine.segment(_snapshot(), {}, SegmentationContext()).engine_id
        == "deep_tiling"
    )
    assert len(gateway.calls) == 1


def test_deep_tiling_observes_cancellation_before_embedding() -> None:
    gateway = _FakeGateway([[1.0, 0.0]] * 8)

    with pytest.raises(SegmentationCancelled):
        DeepTilingSegmenter(gateway=gateway).segment(
            _snapshot(), {}, SegmentationContext(cancelled=lambda: True)
        )
    assert gateway.calls == []
