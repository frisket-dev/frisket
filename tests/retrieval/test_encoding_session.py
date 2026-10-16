from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from frisket.engine._workers.embeddinggemma_model import REQUIRED_SNAPSHOT_FILES
from frisket.engine._workers.embeddinggemma_session import (
    EmbeddingGemma2Session,
    _snapshot_read_paths,
)
from frisket.engine._workers.embeddinggemma_worker import SCHEMA_VERSION
from frisket.engine.sandbox.shim import SandboxPolicy, open_sandboxed_process
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


_CODEC_CHILD = r"""
import sys

from frisket.engine._workers.embeddinggemma_worker import CODEC, SCHEMA_VERSION

source = sys.stdin.buffer
sink = sys.stdout.buffer
request = CODEC.read_frame(source)
if request != {"schema_version": SCHEMA_VERSION, "type": "probe"}:
    raise SystemExit(2)
CODEC.write_frame(
    sink,
    {"schema_version": SCHEMA_VERSION, "type": "ack"},
)
close = CODEC.read_frame(source)
if close != {"schema_version": SCHEMA_VERSION, "type": "close"}:
    raise SystemExit(3)
"""


@pytest.mark.asyncio
@pytest.mark.realtime
async def test_session_json_uses_sandbox_length_framing_with_actual_child(tmp_path):
    """Exercise the real session/transport/worker-codec boundary in both directions."""

    session = EmbeddingGemma2Session(
        tmp_path / EMBEDDINGGEMMA2_MODEL_REVISION, _runtime(tmp_path)
    )
    session._handle = await open_sandboxed_process(
        [
            sys.executable,
            "-c",
            _CODEC_CHILD,
        ],  # subprocess-boundary: actual framed worker transport
        policy=SandboxPolicy(allowed_extra_env=["PYTHONPATH"]),
        extra_env={"PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        should_cancel=session._cancelled,
    )

    response = await session._exchange(
        {"schema_version": SCHEMA_VERSION, "type": "probe"}
    )

    assert response == {"schema_version": SCHEMA_VERSION, "type": "ack"}
    await session.aclose()
    assert session._handle.closed


def test_snapshot_confinement_grants_final_content_store_blobs(tmp_path):
    snapshot = tmp_path / "model-cache" / "models--google--embeddinggemma-2"
    snapshot = snapshot / "snapshots" / EMBEDDINGGEMMA2_MODEL_REVISION
    model_blobs = snapshot.parents[1] / "blobs"
    shared_blobs = tmp_path / "model-cache" / "blobs" / "sha256"
    snapshot.mkdir(parents=True)
    model_blobs.mkdir()
    shared_blobs.mkdir(parents=True)
    expected = {str(snapshot.resolve())}
    for index, relative in enumerate(REQUIRED_SNAPSHOT_FILES):
        final = shared_blobs / str(index)
        final.write_bytes(relative.encode())
        intermediate = model_blobs / str(index)
        intermediate.symlink_to(final)
        entry = snapshot / relative
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.symlink_to(intermediate)
        expected.add(str(final.resolve()))

    assert set(_snapshot_read_paths(snapshot)) == expected


@pytest.mark.asyncio
async def test_installed_factory_resolves_host_owned_runtime_paths(
    monkeypatch, tmp_path
):
    snapshot = tmp_path / EMBEDDINGGEMMA2_MODEL_REVISION
    snapshot.mkdir()
    runtime_python = tmp_path / "runtime" / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.touch()
    artifacts = ModuleType("frisket.engine._workers.embeddinggemma_artifacts")
    setattr(artifacts, "cached_embeddinggemma_path", lambda: snapshot)
    setattr(artifacts, "embeddinggemma_ready", lambda: True)
    install = ModuleType("frisket.runtime.embeddinggemma_install")
    setattr(install, "runtime_python", lambda: runtime_python)
    monkeypatch.setitem(
        sys.modules, "frisket.engine._workers.embeddinggemma_artifacts", artifacts
    )
    monkeypatch.setitem(sys.modules, "frisket.runtime.embeddinggemma_install", install)
    monkeypatch.setattr(
        PythonRuntime,
        "current",
        # subprocess-boundary: interpreter identity only; the factory never spawns it
        classmethod(lambda _cls: PythonRuntime(Path(sys.executable), tmp_path)),
    )

    session = EmbeddingGemma2Session.from_installed()

    assert session._snapshot == snapshot
    assert session._runtime == PythonRuntime(runtime_python, tmp_path)
    await session.aclose()


def test_oversize_batch_is_rejected_before_any_staging_write(monkeypatch, tmp_path):
    import frisket.engine._workers.embeddinggemma_session as session_module

    monkeypatch.setattr(session_module, "MAX_STAGED_BATCH_BYTES", 4)
    session = EmbeddingGemma2Session(
        tmp_path / EMBEDDINGGEMMA2_MODEL_REVISION, _runtime(tmp_path)
    )
    request_dir = Path(tempfile.mkdtemp(dir=session._staging, prefix="batch-"))
    try:
        with pytest.raises(EncodingError, match="512 MiB"):
            session._stage(request_dir, [ImageEncodingInput(b"12345")])
        assert list(request_dir.iterdir()) == []
    finally:
        shutil.rmtree(request_dir)
        session._staging_cleanup()


@pytest.mark.asyncio
async def test_empty_batch_is_invalid_without_closing_session(tmp_path):
    session = EmbeddingGemma2Session(
        tmp_path / EMBEDDINGGEMMA2_MODEL_REVISION, _runtime(tmp_path)
    )
    try:
        with pytest.raises(EncodingError) as caught:
            await session.encode(
                [],
                purpose=EncodingPurpose.DOCUMENT,
                space=EmbeddingGemma2Space(),
            )
        assert caught.value.code == "invalid_input"
        assert session._closed is False
    finally:
        await session.aclose()


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
