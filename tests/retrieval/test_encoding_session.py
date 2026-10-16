from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.engine._workers.embeddinggemma_session import EmbeddingGemma2Session
from frisket.engine._workers.embeddinggemma_worker import SCHEMA_VERSION
from frisket.retrieval.encoding import (
    EMBEDDINGGEMMA2_MODEL_REVISION,
    AudioEncodingInput,
    EmbeddingGemma2Space,
    EncodingError,
    EncodingPurpose,
    ImageEncodingInput,
    VideoClipEncodingInput,
)
from frisket.runtime.launch import PythonRuntime


def _runtime(tmp_path: Path) -> PythonRuntime:
    return PythonRuntime(tmp_path / "missing-python", tmp_path)


@pytest.mark.asyncio
async def test_missing_snapshot_is_an_actionable_preparation_error(tmp_path):
    snapshot = tmp_path / EMBEDDINGGEMMA2_MODEL_REVISION
    session = EmbeddingGemma2Session(snapshot, _runtime(tmp_path))
    try:
        with pytest.raises(EncodingError) as caught:
            await session.encode(
                [ImageEncodingInput(b"encoded image")],
                purpose=EncodingPurpose.DOCUMENT,
                space=EmbeddingGemma2Space(),
            )
        assert caught.value.code == "model_unavailable"
        assert "Prepare the pinned EmbeddingGemma 2 model" in str(caught.value)
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_native_clip_uses_request_owned_staging_and_cleans_it(
    monkeypatch, tmp_path
):
    session = EmbeddingGemma2Session(
        tmp_path / EMBEDDINGGEMMA2_MODEL_REVISION, _runtime(tmp_path)
    )
    seen_paths: list[Path] = []

    class Handle:
        async def exchange_frame(self, payload, **kwargs):
            frame = json.loads(payload)
            assert frame["type"] == "encode"
            assert frame["items"][0]["kind"] == "video"
            assert frame["image_soft_tokens"] == 70
            assert frame["video_soft_tokens_per_frame"] == 140
            for raw_path, expected in zip(
                frame["items"][0]["frames"], (b"frame one", b"frame two")
            ):
                path = Path(raw_path)
                seen_paths.append(path)
                assert path.read_bytes() == expected
            return json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "result",
                    "model_revision": EMBEDDINGGEMMA2_MODEL_REVISION,
                    "vectors": [[1.0, *([0.0] * 127)]],
                }
            ).encode()

        async def close(self, *args, **kwargs):
            pass

        async def abort(self):
            pass

    async def start():
        session._handle = Handle()

    monkeypatch.setattr(session, "_start", start)
    clip = VideoClipEncodingInput(
        (ImageEncodingInput(b"frame one"), ImageEncodingInput(b"frame two"))
    )
    vector = await session.encode(
        [clip],
        purpose=EncodingPurpose.DOCUMENT,
        space=EmbeddingGemma2Space(128, image_soft_tokens=70),
    )

    assert vector == [tuple([1.0, *([0.0] * 127)])]
    assert seen_paths and not any(path.exists() for path in seen_paths)
    await session.aclose()


@pytest.mark.asyncio
async def test_audio_is_explicit_and_does_not_share_video_request(
    monkeypatch, tmp_path
):
    session = EmbeddingGemma2Session(
        tmp_path / EMBEDDINGGEMMA2_MODEL_REVISION, _runtime(tmp_path)
    )

    class Handle:
        async def exchange_frame(self, payload, **kwargs):
            frame = json.loads(payload)
            item = frame["items"][0]
            assert item["kind"] == "audio"
            assert item["sample_rate"] == 16_000
            assert Path(item["path"]).read_bytes() == b"\x01\x00\xff\xff"
            return json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "result",
                    "model_revision": EMBEDDINGGEMMA2_MODEL_REVISION,
                    "vectors": [[1.0, *([0.0] * 127)]],
                }
            ).encode()

        async def close(self, *args, **kwargs):
            pass

        async def abort(self):
            pass

    async def start():
        session._handle = Handle()

    monkeypatch.setattr(session, "_start", start)
    await session.encode(
        [AudioEncodingInput(b"\x01\x00\xff\xff")],
        purpose=EncodingPurpose.DOCUMENT,
        space=EmbeddingGemma2Space(128),
    )
    await session.aclose()
