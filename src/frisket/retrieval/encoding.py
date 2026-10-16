"""Dependency-light public boundary for retrieval encoders.

The source producers own locations and timing.  Encoding inputs contain only
the bytes that affect the representation; in particular, video presentation
timestamps remain on the located part and are not guessed or embedded here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeAlias


EMBEDDINGGEMMA2_MODEL_ID = "google/embeddinggemma-2"
EMBEDDINGGEMMA2_MODEL_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
EMBEDDINGGEMMA2_TRANSFORMERS_SERIES = "5.19"
EMBEDDINGGEMMA2_SUPPORTED_DIMENSIONS = frozenset({768, 512, 256, 128})
EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES = 32
EMBEDDINGGEMMA2_CONTEXT_TOKENS = 8192
EMBEDDINGGEMMA2_IMAGE_SOFT_TOKENS = 280
EMBEDDINGGEMMA2_VIDEO_SOFT_TOKENS_PER_FRAME = 140
EMBEDDINGGEMMA2_SUPPORTED_VISUAL_SOFT_TOKENS = frozenset({70, 140, 280})
EMBEDDINGGEMMA2_MAX_ENCODED_IMAGE_BYTES = 64 * 1024 * 1024
EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE = 16_000
EMBEDDINGGEMMA2_QUERY_PREFIX = "task: search result | query: "
EMBEDDINGGEMMA2_DOCUMENT_PREFIX = "title: none | text: "

# This identity names every choice made after a producer emits input bytes.
# Sampling interval, shot detection and JPEG encoding remain producer-recipe
# facts, so changing those recipes does not incorrectly create another model
# space for unrelated representations.
EMBEDDINGGEMMA2_PREPROCESSING_BASE_ID = (
    "transformers-5.19:auto-processor;"
    "text=query-document-prefixes,max-length-8192;"
    "image=pillow-decoded-rgb;"
    "video=pillow-decoded-rgb,predecoded,no-sampling,max-frames-32,"
    "no-timestamps;"
    "audio=pcm16le-mono-16000hz;pool=attention-mask-mean-float32;"
    "truncate-prefix-then-l2"
)


class EncodingPurpose(StrEnum):
    QUERY = "query"
    DOCUMENT = "document"


class EncodingError(RuntimeError):
    """Actionable failure at the public encoder boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validate_image_bytes(value: bytes, *, name: str) -> None:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > EMBEDDINGGEMMA2_MAX_ENCODED_IMAGE_BYTES:
        raise ValueError(f"{name} exceeds the 64 MiB encoded-image limit")


@dataclass(frozen=True, slots=True)
class TextEncodingInput:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        if not self.text.strip():
            raise ValueError("text encoding input must not be empty")


@dataclass(frozen=True, slots=True)
class ImageEncodingInput:
    image_bytes: bytes

    def __post_init__(self) -> None:
        _validate_image_bytes(self.image_bytes, name="image_bytes")


@dataclass(frozen=True, slots=True)
class VideoClipEncodingInput:
    frames: tuple[ImageEncodingInput, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.frames, tuple) or not all(
            isinstance(frame, ImageEncodingInput) for frame in self.frames
        ):
            raise TypeError("video frames must be a tuple of ImageEncodingInput")
        if not 1 <= len(self.frames) <= EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES:
            raise ValueError(
                "a native video clip must contain between 1 and 32 decoded frames"
            )


@dataclass(frozen=True, slots=True)
class AudioEncodingInput:
    pcm16le_bytes: bytes
    sample_rate: int = EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE

    def __post_init__(self) -> None:
        if not isinstance(self.pcm16le_bytes, bytes):
            raise TypeError("audio pcm16le_bytes must be bytes")
        if not self.pcm16le_bytes or len(self.pcm16le_bytes) % 2:
            raise ValueError("audio must contain complete signed PCM16 samples")
        if self.sample_rate != EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE:
            raise ValueError("EmbeddingGemma 2 audio must be mono PCM16 at 16000 Hz")


EncodingInput: TypeAlias = (
    TextEncodingInput
    | ImageEncodingInput
    | VideoClipEncodingInput
    | AudioEncodingInput
)
EmbeddingVector: TypeAlias = tuple[float, ...]


@dataclass(frozen=True, slots=True)
class EmbeddingGemma2Space:
    """The complete comparable-space identity for the pinned local model."""

    dimensions: int = 768
    image_soft_tokens: int = EMBEDDINGGEMMA2_IMAGE_SOFT_TOKENS
    video_soft_tokens_per_frame: int = (
        EMBEDDINGGEMMA2_VIDEO_SOFT_TOKENS_PER_FRAME
    )
    model_id: str = field(default=EMBEDDINGGEMMA2_MODEL_ID, init=False)
    model_revision: str = field(
        default=EMBEDDINGGEMMA2_MODEL_REVISION, init=False
    )
    metric: str = field(default="cosine", init=False)
    normalized: bool = field(default=True, init=False)
    precision: str = field(default="float32", init=False)

    def __post_init__(self) -> None:
        if self.dimensions not in EMBEDDINGGEMMA2_SUPPORTED_DIMENSIONS:
            raise ValueError(
                "EmbeddingGemma 2 dimensions must be one of 768, 512, 256, or 128"
            )
        if self.image_soft_tokens not in EMBEDDINGGEMMA2_SUPPORTED_VISUAL_SOFT_TOKENS:
            raise ValueError("image soft-token budget must be 70, 140, or 280")
        if (
            self.video_soft_tokens_per_frame
            not in EMBEDDINGGEMMA2_SUPPORTED_VISUAL_SOFT_TOKENS
        ):
            raise ValueError("video soft-token budget must be 70, 140, or 280")

    @property
    def preprocessing_id(self) -> str:
        return (
            f"{EMBEDDINGGEMMA2_PREPROCESSING_BASE_ID};"
            f"image-soft-tokens={self.image_soft_tokens};"
            f"video-soft-tokens-per-frame={self.video_soft_tokens_per_frame}"
        )

    @property
    def identity(self) -> str:
        return (
            f"{self.model_id}@{self.model_revision}:"
            f"{self.preprocessing_id}:dim={self.dimensions}:"
            f"{self.metric}:{self.precision}:normalized"
        )


class EmbeddingGemma2SessionProtocol(Protocol):
    async def encode(
        self,
        inputs: Sequence[EncodingInput],
        *,
        purpose: EncodingPurpose,
        space: EmbeddingGemma2Space,
    ) -> list[EmbeddingVector]: ...

    async def aclose(self) -> None: ...


def _batch_limit(value: EncodingInput) -> int:
    # The bounded phase-1 runtime measured text batching but processed every
    # media sample individually.  Keep that proven memory posture: native clips
    # can already consume 32 * 140 visual soft tokens in one model sequence.
    return 32 if isinstance(value, TextEncodingInput) else 1


class EmbeddingGemma2Encoder:
    """Public adapter over one run-owned optional EmbeddingGemma 2 session."""

    def __init__(self, session: EmbeddingGemma2SessionProtocol) -> None:
        self._session = session

    async def encode(
        self,
        inputs: Sequence[EncodingInput],
        *,
        purpose: EncodingPurpose,
        space: EmbeddingGemma2Space,
    ) -> list[EmbeddingVector]:
        values = list(inputs)
        if not values:
            return []
        if not isinstance(purpose, EncodingPurpose):
            raise TypeError("purpose must be EncodingPurpose.QUERY or DOCUMENT")
        if not isinstance(space, EmbeddingGemma2Space):
            raise TypeError("space must be an EmbeddingGemma2Space")
        allowed = (
            TextEncodingInput,
            ImageEncodingInput,
            VideoClipEncodingInput,
            AudioEncodingInput,
        )
        if not all(isinstance(value, allowed) for value in values):
            raise TypeError("unsupported retrieval encoding input")

        vectors: list[EmbeddingVector] = []
        offset = 0
        while offset < len(values):
            input_type = type(values[offset])
            limit = _batch_limit(values[offset])
            end = offset
            while (
                end < len(values)
                and type(values[end]) is input_type
                and end - offset < limit
            ):
                end += 1
            batch = values[offset:end]
            encoded = await self._session.encode(
                batch, purpose=purpose, space=space
            )
            self._validate_vectors(encoded, expected=len(batch), space=space)
            vectors.extend(encoded)
            offset = end
        return vectors

    @staticmethod
    def _validate_vectors(
        vectors: Sequence[Sequence[float]],
        *,
        expected: int,
        space: EmbeddingGemma2Space,
    ) -> None:
        if len(vectors) != expected:
            raise EncodingError(
                "invalid_encoder_result",
                "The local embedding model returned the wrong number of vectors.",
            )
        for vector in vectors:
            if len(vector) != space.dimensions or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in vector
            ):
                raise EncodingError(
                    "invalid_encoder_result",
                    "The local embedding model returned an invalid vector.",
                )
            norm = math.sqrt(sum(float(value) ** 2 for value in vector))
            if not math.isclose(norm, 1.0, rel_tol=2e-4, abs_tol=2e-4):
                raise EncodingError(
                    "invalid_encoder_result",
                    "The local embedding model returned an unnormalized vector.",
                )

    async def aclose(self) -> None:
        await self._session.aclose()


__all__ = [
    "AudioEncodingInput",
    "EMBEDDINGGEMMA2_AUDIO_SAMPLE_RATE",
    "EMBEDDINGGEMMA2_CONTEXT_TOKENS",
    "EMBEDDINGGEMMA2_DOCUMENT_PREFIX",
    "EMBEDDINGGEMMA2_IMAGE_SOFT_TOKENS",
    "EMBEDDINGGEMMA2_MAX_VIDEO_FRAMES",
    "EMBEDDINGGEMMA2_MODEL_ID",
    "EMBEDDINGGEMMA2_MODEL_REVISION",
    "EMBEDDINGGEMMA2_MAX_ENCODED_IMAGE_BYTES",
    "EMBEDDINGGEMMA2_PREPROCESSING_BASE_ID",
    "EMBEDDINGGEMMA2_QUERY_PREFIX",
    "EMBEDDINGGEMMA2_SUPPORTED_DIMENSIONS",
    "EMBEDDINGGEMMA2_SUPPORTED_VISUAL_SOFT_TOKENS",
    "EMBEDDINGGEMMA2_TRANSFORMERS_SERIES",
    "EMBEDDINGGEMMA2_VIDEO_SOFT_TOKENS_PER_FRAME",
    "EmbeddingGemma2Encoder",
    "EmbeddingGemma2Space",
    "EmbeddingVector",
    "EncodingError",
    "EncodingInput",
    "EncodingPurpose",
    "ImageEncodingInput",
    "TextEncodingInput",
    "VideoClipEncodingInput",
]
