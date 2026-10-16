from __future__ import annotations

import math

import pytest

from frisket.retrieval.encoding import (
    AudioEncodingInput,
    EmbeddingGemma2Encoder,
    EmbeddingGemma2Space,
    EncodingError,
    EncodingPurpose,
    ImageEncodingInput,
    TextEncodingInput,
    VideoClipEncodingInput,
)


def _unit_vector(dimensions: int, index: int = 0) -> tuple[float, ...]:
    return tuple(1.0 if offset == index else 0.0 for offset in range(dimensions))


def test_media_inputs_are_bounded_and_clip_reuses_generic_images():
    png = ImageEncodingInput(b"a complete format is validated in the worker")
    assert VideoClipEncodingInput((png,)).frames == (png,)

    with pytest.raises(ValueError, match="between 1 and 32"):
        VideoClipEncodingInput(())
    with pytest.raises(ValueError, match="between 1 and 32"):
        VideoClipEncodingInput((png,) * 33)
    with pytest.raises(ValueError, match="complete signed PCM16"):
        AudioEncodingInput(b"odd")
    with pytest.raises(ValueError, match="16000 Hz"):
        AudioEncodingInput(b"\x00\x00", sample_rate=48_000)


def test_space_identity_includes_embedding_affecting_visual_budgets():
    compact = EmbeddingGemma2Space(
        256, image_soft_tokens=70, video_soft_tokens_per_frame=140
    )
    detailed = EmbeddingGemma2Space(
        256, image_soft_tokens=280, video_soft_tokens_per_frame=280
    )

    assert compact.identity != detailed.identity
    assert "image-soft-tokens=70" in compact.identity
    assert "video-soft-tokens-per-frame=140" in compact.identity
    assert compact.max_video_frames == 32
    assert detailed.max_video_frames == 29
    with pytest.raises(ValueError, match="70, 140, or 280"):
        EmbeddingGemma2Space(image_soft_tokens=560)


@pytest.mark.asyncio
async def test_encoder_rejects_clip_that_exceeds_selected_context_budget():
    class UnreachedSession:
        async def encode(self, inputs, *, purpose, space):
            raise AssertionError("over-budget clip reached the runtime")

        async def aclose(self):
            pass

    frame = ImageEncodingInput(b"encoded image")
    with pytest.raises(EncodingError, match="visual-token budget"):
        await EmbeddingGemma2Encoder(UnreachedSession()).encode(
            [VideoClipEncodingInput((frame,) * 30)],
            purpose=EncodingPurpose.DOCUMENT,
            space=EmbeddingGemma2Space(video_soft_tokens_per_frame=280),
        )


@pytest.mark.asyncio
async def test_encoder_batches_text_and_serializes_media_without_reordering():
    class Session:
        def __init__(self) -> None:
            self.calls = []

        async def encode(self, inputs, *, purpose, space):
            self.calls.append((list(inputs), purpose, space))
            return [_unit_vector(space.dimensions) for _ in inputs]

        async def aclose(self):
            pass

    session = Session()
    encoder = EmbeddingGemma2Encoder(session)
    image = ImageEncodingInput(b"encoded image")
    inputs = [
        *[TextEncodingInput(f"passage {index}") for index in range(33)],
        image,
        VideoClipEncodingInput((image, image)),
        AudioEncodingInput(b"\x00\x00"),
    ]
    space = EmbeddingGemma2Space(128)

    vectors = await encoder.encode(
        inputs, purpose=EncodingPurpose.DOCUMENT, space=space
    )

    assert len(vectors) == len(inputs)
    assert [len(call[0]) for call in session.calls] == [32, 1, 1, 1, 1]
    assert [type(call[0][0]) for call in session.calls] == [
        TextEncodingInput,
        TextEncodingInput,
        ImageEncodingInput,
        VideoClipEncodingInput,
        AudioEncodingInput,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vector",
    [
        (1.0,),
        (math.nan,) + (0.0,) * 127,
        (0.5,) + (0.0,) * 127,
    ],
)
async def test_encoder_rejects_invalid_worker_vectors(vector):
    class Session:
        async def encode(self, inputs, *, purpose, space):
            return [vector]

        async def aclose(self):
            pass

    with pytest.raises(EncodingError, match="invalid vector|unnormalized vector"):
        await EmbeddingGemma2Encoder(Session()).encode(
            [TextEncodingInput("query")],
            purpose=EncodingPurpose.QUERY,
            space=EmbeddingGemma2Space(128),
        )
