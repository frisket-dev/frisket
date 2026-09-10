"""The routed ``Classifier`` capability's ``local_semantic`` engine.

FastEmbed cosine winner, no model call. The algorithm is the retired
``ClassifyRecipe.execute`` local path, moved verbatim: bounded deterministic
chunking, a normalized mean row vector, and the highest-cosine label with the
lowest index winning ties. The retired recipe ran with ``max_concurrency=1``;
one admitted instance serializes its rows the same way so the label-vector
cache is filled exactly once per label set.

The host binds this class when the admitted engine is ``local_semantic``. For
``llm`` it binds a model-backed ``Classifier`` instead (host-owned, not here)
that sends the host-rendered ``classify_prompt`` for the row and folds the
structured reply into one ``Outcome`` per field; that prompt projection is the
engine's estimate/consent/cache input before egress.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Sequence

from frisket.actions.classify_types import ClassifyField
from frisket.actions.types import Outcome, Row, RowError
from frisket.semantic import PROVIDERLESS_CLASSIFY_MODEL
from frisket.semantic import (
    ProviderlessClassifierProvisionError,
    ensure_providerless_classifier_installed,
)


# Moved from frisket.sdk.ops.classify, which sits above the executor boundary.
LOCAL_SEMANTIC_MODEL = PROVIDERLESS_CLASSIFY_MODEL
LOCAL_SEMANTIC_CHUNK_WORDS = 384
LOCAL_SEMANTIC_CHUNK_CHARS = 1_600
LOCAL_SEMANTIC_MAX_CHUNKS = 8


def local_semantic_chunks(text: str) -> list[str]:
    """Deterministically bound long inputs before FastEmbed's 512-token limit.

    Whitespace is normalized, then the first eight greedy chunks of at most
    384 whitespace terms and 1,600 Unicode characters are retained. FastEmbed
    still applies its tokenizer's own hard 512-subword truncation to each
    chunk; the explicit chunking makes which source prefix participates stable
    across runs and keeps ordinary prose comfortably below that boundary.
    """

    words = re.findall(r"\S+", str(text))
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for word in words:
        pieces = [
            word[offset : offset + LOCAL_SEMANTIC_CHUNK_CHARS]
            for offset in range(0, len(word), LOCAL_SEMANTIC_CHUNK_CHARS)
        ] or [word]
        for piece in pieces:
            added = len(piece) + (1 if current else 0)
            if current and (
                len(current) >= LOCAL_SEMANTIC_CHUNK_WORDS
                or current_chars + added > LOCAL_SEMANTIC_CHUNK_CHARS
            ):
                chunks.append(" ".join(current))
                if len(chunks) >= LOCAL_SEMANTIC_MAX_CHUNKS:
                    return chunks
                current = []
                current_chars = 0
            current.append(piece)
            current_chars += len(piece) + (1 if len(current) > 1 else 0)
    if current and len(chunks) < LOCAL_SEMANTIC_MAX_CHUNKS:
        chunks.append(" ".join(current))
    return chunks


def _normalized_mean(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dimension = len(vectors[0])
    if any(len(vector) != dimension for vector in vectors):
        raise ValueError("local semantic embedder returned inconsistent dimensions")
    mean = [
        sum(vector[index] for vector in vectors) / len(vectors)
        for index in range(dimension)
    ]
    norm = math.sqrt(sum(value * value for value in mean))
    return [value / norm for value in mean] if norm else mean


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("local semantic embeddings have inconsistent dimensions")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )


class AdmittedClassifier:
    """Invocation-owned ``Classifier`` for the ``local_semantic`` engine.

    Free and provider-less: it embeds locally through the gateway's
    ``providerless_classify`` capability and never touches the model router.
    """

    def __init__(self, *, cancelled: Callable[[], bool] | None = None) -> None:
        self._cancelled = cancelled
        self._closed = False
        self._lock = asyncio.Lock()
        self._label_vector_cache: dict[tuple[str, ...], list[list[float]]] = {}

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("classifier is closed")
        if self._cancelled is not None and self._cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row: Row, **_admission: object) -> _BoundClassifier:
        self._check_open()
        return _BoundClassifier(self, row)

    async def classify(
        self, row: Row, text: str, fields: Sequence[ClassifyField]
    ) -> dict[str, Outcome[str]]:
        del row
        self._check_open()
        if len(fields) != 1 or fields[0].type != "category":
            raise RowError(
                "invalid_classify_field",
                "local_semantic classifies exactly one category field.",
            )
        field = fields[0]
        labels = list(field.labels)
        descriptions = field.label_descriptions or {}
        label_texts = tuple(str(descriptions.get(label) or label) for label in labels)
        chunks = local_semantic_chunks(text)
        if not chunks:
            return {field.name: Outcome.ok(labels[0])}

        try:
            await asyncio.to_thread(ensure_providerless_classifier_installed)
        except ProviderlessClassifierProvisionError as exc:
            raise RowError("model_unavailable", str(exc)) from exc
        self._check_open()

        from frisket.ai.embeddings import EmbeddingGateway

        async with self._lock:
            self._check_open()
            gateway = EmbeddingGateway()
            label_vectors = self._label_vector_cache.get(label_texts)
            if label_vectors is None:
                label_vectors = gateway.embed(
                    list(label_texts),
                    provider="fastembed",
                    model=LOCAL_SEMANTIC_MODEL,
                    modality="text",
                    local_capability="providerless_classify",
                )["vectors"]
                self._label_vector_cache[label_texts] = label_vectors
            row_vectors = gateway.embed(
                chunks,
                provider="fastembed",
                model=LOCAL_SEMANTIC_MODEL,
                modality="text",
                local_capability="providerless_classify",
            )["vectors"]
        row_vector = _normalized_mean(row_vectors)
        winner_index, _similarity = max(
            enumerate(_cosine(row_vector, vector) for vector in label_vectors),
            key=lambda item: (item[1], -item[0]),
        )
        return {field.name: Outcome.ok(labels[winner_index])}

    async def aclose(self) -> None:
        self._closed = True


class _BoundClassifier:
    """A classifier admitted for exactly one row of its owning invocation."""

    def __init__(self, owner: AdmittedClassifier, row: Row) -> None:
        self._owner = owner
        self._row = row

    async def classify(
        self, row: Row, text: str, fields: Sequence[ClassifyField]
    ) -> dict[str, Outcome[str]]:
        if row is not self._row:
            raise RowError("invalid_input_ref", "Classifier requires its admitted row.")
        return await self._owner.classify(row, text, fields)
