"""transcribe-sandbox-abs-path-v1: local workers get absolute blob paths.

Faster-whisper runs in a scratch sandbox cwd, while Parakeet receives framed
requests through ``ParakeetProcessSession``. A relative blob path cannot
resolve in either child process even though it resolves fine from the server's
cwd, so both local engines must absolutize it at their worker-input seam.
"""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

import json
import os

import pytest

from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTRACT_VERSION
from frisket.runtime.launch import worker_argv
from frisket.sdk.ops.transcription import parakeet as transcribe_mod


class _CapturedCall:
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.argvs: list[list[str]] = []


@pytest.fixture()
def captured(monkeypatch, tmp_path):
    cap = _CapturedCall()

    async def fake_run_sandboxed(
        argv, *, policy, stdin_data, should_cancel=None, extra_env=None
    ):
        cap.argvs.append(list(argv))
        payload = json.loads(stdin_data.decode())
        cap.payloads.append(payload)

        class _R:
            ok = True
            returncode = 0
            stdout = json.dumps(
                {
                    "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
                    "results": [
                        {
                            "engine": "faster-whisper",
                            "text": "ok",
                            "segments": [],
                            "language": None,
                            "duration": 0.0,
                            "model_ids": ["faster-whisper/base"],
                            "revision": "runtime-resolved",
                            "device": "cpu",
                            "dtype": "int8",
                            "timings": {"inference_seconds": 0.0},
                            "warnings": [],
                            "accepted_options": payload.get("options", {}),
                        }
                    ],
                }
            ).encode()
            stderr = b""
            timed_out = False

        return _R()

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake_run_sandboxed
    )

    class FakeParakeetSession:
        def __init__(self, *, expected_rows, vad, should_cancel) -> None:
            del expected_rows, vad, should_cancel
            self.teardown_failed = False

        def mark_closed(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def transcribe(self, path: str) -> dict:
            # The framed Parakeet request is constructed by the session, so
            # observe its API input rather than the retired sandbox helper.
            cap.payloads.append({"path": path})
            return {"text": "ok", "segments": []}

    monkeypatch.setattr(transcribe_mod, "ParakeetProcessSession", FakeParakeetSession)
    blob = tmp_path / "blobs" / "ab" / "abcd1234"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"fake-audio")
    # a RELATIVE path, as the store hands out — the bug's trigger
    rel = os.path.relpath(blob, os.getcwd())
    return cap, rel, str(blob)


@pytest.mark.parametrize("engine", ["faster_whisper", "parakeet"])
def test_sandbox_payload_path_is_absolute(captured, engine):
    cap, rel_path, abs_path = captured
    adapter = (
        transcribe_engines.FasterWhisperAdapter()
        if engine == "faster_whisper"
        else transcribe_engines.ParakeetAdapter()
    )
    import asyncio

    asyncio.run(adapter.transcribe(rel_path, {"vad": False}))
    assert cap.payloads, "worker never invoked"
    payload = cap.payloads[-1]
    sent = payload.get("audio_path", payload.get("path"))
    assert os.path.isabs(sent), f"sandbox payload path not absolute: {sent}"
    assert os.path.realpath(sent) == os.path.realpath(abs_path)
    assert os.path.exists(sent)
    if engine == "faster_whisper":
        assert cap.argvs[-1] == worker_argv("faster-whisper")


def test_local_faster_whisper_forwards_supported_controls(captured):
    cap, rel_path, _abs_path = captured
    import asyncio

    asyncio.run(
        transcribe_engines.FasterWhisperAdapter().transcribe(
            rel_path,
            {
                "language": ["fr"],
                "model_size": "small",
                "vad": False,
                "context": "Frisket, CTranslate2",
            },
        )
    )

    assert cap.payloads[-1] == {
        "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
        "engine": "faster-whisper",
        "audio_path": os.path.abspath(rel_path),
        "options": {
            "language": "fr",
            "model_size": "small",
            "vad": False,
            "context": "Frisket, CTranslate2",
        },
    }


def test_local_faster_whisper_rejects_unversioned_worker_response(
    monkeypatch, tmp_path
):
    async def fake_run_sandboxed(
        argv, *, policy, stdin_data, should_cancel=None, extra_env=None
    ):
        class _R:
            ok = True
            returncode = 0
            stdout = json.dumps({"text": "legacy", "segments": []}).encode()
            stderr = b""
            timed_out = False

        return _R()

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake_run_sandboxed
    )
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    with pytest.raises(RuntimeError, match="malformed.*transcription v1 response"):
        import asyncio

        asyncio.run(
            transcribe_engines.FasterWhisperAdapter().transcribe(str(audio), {})
        )
