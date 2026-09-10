from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from frisket.engine._workers import local_engine_lease as lease_mod
from frisket.engine._workers import parakeet_session as session_mod
from frisket.engine._workers.parakeet_artifacts import (
    ParakeetArtifactUnavailable,
    ParakeetArtifacts,
)
from frisket.engine.sandbox.shim import (
    SandboxProcessCancelledError,
    SandboxTeardownError,
)


def _frame(frame_type: str, **values: Any) -> bytes:
    return json.dumps(
        {
            "schema_version": session_mod.SCHEMA_VERSION,
            "type": frame_type,
            **values,
        },
        separators=(",", ":"),
    ).encode()


def _thread_config(**overrides: Any) -> dict[str, Any]:
    return {
        "mode": "shared_global",
        "intra": 4,
        "inter": 1,
        "cpu_count": 8,
        "ort_version": "1.26.0",
        **overrides,
    }


def _ready(**overrides: Any) -> bytes:
    values = {
        "engine": "parakeet",
        "model": "nemo-parakeet-tdt-0.6b-v2",
        "onnx_threads": _thread_config(),
    }
    values.update(overrides)
    return _frame("ready", **values)


def _success(request_id: str, text: str = "hello") -> bytes:
    return _frame(
        "result",
        request_id=request_id,
        ok=True,
        data={
            "text": text,
            "segments": [{"start": 0.0, "end": 0.5, "text": text}],
            "language": "en",
        },
    )


class _FakeHandle:
    def __init__(
        self,
        responder: Callable[[dict[str, Any], int], bytes | BaseException] | None = None,
        *,
        pid: int | None = None,
    ) -> None:
        self.responder = responder
        self.pid = pid
        self.exchanges: list[dict[str, Any]] = []
        self.close_frames: list[dict[str, Any]] = []
        self.abort_calls = 0

    async def exchange_frame(self, payload, *, wall_seconds, response_limit):
        frame = json.loads(payload)
        self.exchanges.append(frame)
        if self.responder is not None:
            response = self.responder(frame, len(self.exchanges))
            if isinstance(response, BaseException):
                raise response
            return response
        if frame["type"] == "init":
            return _ready()
        return _success(frame["request_id"])

    async def close(self, payload, *, wall_seconds):
        self.close_frames.append(json.loads(payload))
        return 0

    async def abort(self):
        self.abort_calls += 1


@pytest.fixture(autouse=True)
def _reset_lease() -> None:
    lease_mod._reset_lease_state_for_tests()
    yield
    lease_mod._reset_lease_state_for_tests()


@pytest.fixture
def artifacts(tmp_path: Path) -> ParakeetArtifacts:
    return ParakeetArtifacts(
        cache_dir=tmp_path / "hub",
        model_path=tmp_path / "model",
        vad_path=tmp_path / "vad",
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    artifacts: ParakeetArtifacts,
    handle: _FakeHandle,
) -> dict[str, int]:
    calls = {"resolve": 0, "open": 0}

    async def resolve(*, vad, should_cancel):
        calls["resolve"] += 1
        assert vad is True
        return artifacts

    async def open_process(*args, **kwargs):
        calls["open"] += 1
        assert callable(kwargs["should_cancel"])
        assert kwargs["extra_env"] == {
            "HF_HUB_CACHE": str(artifacts.cache_dir),
            "HF_HUB_OFFLINE": "1",
        }
        assert kwargs["policy"].trusted_python_netwall is True
        return handle

    monkeypatch.setattr(session_mod, "resolve_parakeet_artifacts", resolve)
    monkeypatch.setattr(
        session_mod.sandbox_shim, "open_sandboxed_process", open_process
    )
    return calls


def test_ready_frame_strictly_validates_observed_onnx_threads() -> None:
    shared = session_mod._validate_ready(json.loads(_ready()))
    assert shared == _thread_config()

    shared_four_cpu = _thread_config(intra=3, cpu_count=4)
    assert (
        session_mod._validate_ready(json.loads(_ready(onnx_threads=shared_four_cpu)))
        == shared_four_cpu
    )

    fallback = _thread_config(mode="per_session_fallback", intra=1, cpu_count=2)
    assert (
        session_mod._validate_ready(json.loads(_ready(onnx_threads=fallback)))
        == fallback
    )

    invalid = [
        _thread_config(intra=3),
        _thread_config(mode="per_session_fallback", intra=2),
        _thread_config(inter=2),
        _thread_config(cpu_count=True),
        _thread_config(ort_version="../../secret"),
        {**_thread_config(), "unexpected": 1},
    ]
    for threads in invalid:
        with pytest.raises(session_mod.ParakeetSessionFailure, match="thread"):
            session_mod._validate_ready(json.loads(_ready(onnx_threads=threads)))

    missing = json.loads(_ready())
    missing.pop("onnx_threads")
    with pytest.raises(session_mod.ParakeetSessionFailure, match="ready"):
        session_mod._validate_ready(missing)


def test_proc_status_peak_memory_parser_is_bounded_to_peak_fields() -> None:
    assert session_mod._parse_proc_status_memory(
        "Name:\tpython\n"
        "VmPeak:\t  4096 kB\n"
        "VmSize:\t  2048 kB\n"
        "VmHWM:\t   1024 kB\n"
        "VmRSS:\t    512 kB\n"
        "VmPeak:\tinvalid kB\n"
        "VmHWM:\t     -1 kB\n"
    ) == {"vm_peak_bytes": 4096 * 1024, "vm_hwm_bytes": 1024 * 1024}
    assert (
        session_mod._parse_proc_status_memory("VmPeak: 4 MB\nVmHWM: unknown kB\n") == {}
    )


def test_session_logs_safe_lifecycle_timings_threads_and_peak_memory(
    artifacts: ParakeetArtifacts,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript = "SECRET_TRANSCRIPT_MUST_NOT_BE_LOGGED"
    input_path = "SECRET_INPUT_PATH_MUST_NOT_BE_LOGGED.wav"

    def respond(frame: dict[str, Any], call: int) -> bytes:
        del call
        if frame["type"] == "init":
            return _ready()
        return _success(frame["request_id"], transcript)

    handle = _FakeHandle(respond, pid=4242)
    _install_fakes(monkeypatch, artifacts, handle)
    samples = iter(
        [
            {"vm_hwm_bytes": 100, "vm_peak_bytes": 200},
            {"vm_hwm_bytes": 150, "vm_peak_bytes": 250},
            {},
        ]
    )
    monkeypatch.setattr(
        session_mod,
        "_read_linux_peak_memory",
        lambda pid: next(samples) if pid == 4242 else {},
    )
    caplog.set_level("INFO", logger=session_mod.logger.name)

    async def run() -> None:
        session = session_mod.ParakeetProcessSession(
            expected_rows=1, vad=True, should_cancel=None
        )
        assert (await session.transcribe(input_path))["text"] == transcript
        await session.close()

    asyncio.run(run())
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", "").startswith("parakeet_")
    ]
    assert [record.event for record in records] == [
        "parakeet_artifact_resolution_completed",
        "parakeet_model_ready",
        "parakeet_request_completed",
        "parakeet_session_closed",
    ]
    assert all(record.duration_ms >= 0 for record in records)

    ready = records[1]
    assert ready.onnx_thread_mode == "shared_global"
    assert ready.onnx_intra_threads == 4
    assert ready.onnx_inter_threads == 1
    assert ready.onnx_cpu_count == 8
    assert ready.onnxruntime_version == "1.26.0"
    assert (ready.vm_hwm_bytes, ready.vm_peak_bytes) == (100, 200)

    request = records[2]
    assert (request.request_index, request.status) == (1, "ok")
    assert (request.vm_hwm_bytes, request.vm_peak_bytes) == (150, 250)
    closed = records[3]
    assert (closed.requests, closed.status, closed.returncode) == (1, "ok", 0)
    assert (closed.vm_hwm_bytes, closed.vm_peak_bytes) == (150, 250)

    rendered = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in records
    )
    assert input_path not in rendered
    assert transcript not in rendered


def test_session_is_lazy_reuses_one_child_and_closes_cleanly(
    artifacts: ParakeetArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _FakeHandle()
    calls = _install_fakes(monkeypatch, artifacts, handle)

    async def run() -> None:
        session = session_mod.ParakeetProcessSession(
            expected_rows=2, vad=True, should_cancel=None
        )
        await session.close()  # unused close is resource-free
        assert calls == {"resolve": 0, "open": 0}

        session = session_mod.ParakeetProcessSession(
            expected_rows=2, vad=True, should_cancel=None
        )
        assert (await session.transcribe("one.wav"))["text"] == "hello"
        assert (await session.transcribe("two.wav"))["text"] == "hello"
        await session.close()
        with pytest.raises(session_mod.ParakeetSessionFailure, match="closed"):
            await session.transcribe("three.wav")

    asyncio.run(run())
    assert calls == {"resolve": 1, "open": 1}
    assert [frame["type"] for frame in handle.exchanges] == [
        "init",
        "request",
        "request",
    ]
    assert [frame["request_id"] for frame in handle.exchanges[1:]] == ["r1", "r2"]
    assert handle.close_frames == [
        {"schema_version": session_mod.SCHEMA_VERSION, "type": "close"}
    ]
    assert handle.abort_calls == 0


def test_valid_row_error_does_not_poison_the_session(
    artifacts: ParakeetArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(frame: dict[str, Any], call: int):
        if frame["type"] == "init":
            return _ready()
        if frame["request_id"] == "r1":
            return _frame(
                "result",
                request_id="r1",
                ok=False,
                error={
                    "code": "audio_transcription_failed",
                    "message": "audio could not be decoded or transcribed",
                },
            )
        return _success(frame["request_id"], "recovered")

    handle = _FakeHandle(respond)
    calls = _install_fakes(monkeypatch, artifacts, handle)

    async def run() -> None:
        session = session_mod.ParakeetProcessSession(
            expected_rows=2, vad=True, should_cancel=None
        )
        with pytest.raises(session_mod.ParakeetRowError):
            await session.transcribe("bad.wav")
        assert (await session.transcribe("good.wav"))["text"] == "recovered"
        await session.close()

    asyncio.run(run())
    assert calls == {"resolve": 1, "open": 1}
    assert handle.abort_calls == 0


@pytest.mark.parametrize("failure", ["fatal", "wrong_id", "bad_segment"])
def test_protocol_failure_aborts_and_session_never_restarts(
    failure: str,
    artifacts: ParakeetArtifacts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(frame: dict[str, Any], call: int):
        if frame["type"] == "init":
            return _ready()
        if failure == "fatal":
            return _frame(
                "fatal",
                error={"code": "parakeet_protocol_failed", "message": "failed"},
            )
        if failure == "wrong_id":
            return _success("different-id")
        return _frame(
            "result",
            request_id=frame["request_id"],
            ok=True,
            data={
                "text": "bad",
                "segments": [{"start": "zero", "end": 1, "text": "bad"}],
                "language": "en",
            },
        )

    handle = _FakeHandle(respond)
    calls = _install_fakes(monkeypatch, artifacts, handle)

    async def run() -> None:
        session = session_mod.ParakeetProcessSession(
            expected_rows=2, vad=True, should_cancel=None
        )
        with pytest.raises(session_mod.ParakeetSessionFailure):
            await session.transcribe("one.wav")
        with pytest.raises(session_mod.ParakeetSessionFailure, match="closed"):
            await session.transcribe("two.wav")
        await session.close()

    asyncio.run(run())
    assert calls == {"resolve": 1, "open": 1}
    assert handle.abort_calls == 1


def test_missing_artifact_fatal_is_typed_and_aborts(
    artifacts: ParakeetArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(frame: dict[str, Any], call: int):
        return _frame(
            "fatal",
            error={
                "code": "local_artifact_unavailable",
                "message": "pinned artifact unavailable",
            },
        )

    handle = _FakeHandle(respond)
    _install_fakes(monkeypatch, artifacts, handle)

    async def run() -> None:
        session = session_mod.ParakeetProcessSession(
            expected_rows=1, vad=True, should_cancel=None
        )
        with pytest.raises(ParakeetArtifactUnavailable):
            await session.transcribe("one.wav")

    asyncio.run(run())
    assert handle.abort_calls == 1


def test_cancellation_and_teardown_remain_distinct(
    artifacts: ParakeetArtifacts, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancelled_handle = _FakeHandle(
        lambda frame, call: SandboxProcessCancelledError("cancelled")
    )
    _install_fakes(monkeypatch, artifacts, cancelled_handle)

    async def cancelled() -> None:
        session = session_mod.ParakeetProcessSession(
            expected_rows=1, vad=True, should_cancel=None
        )
        with pytest.raises(session_mod.ParakeetSessionCancelled):
            await session.transcribe("one.wav")

    asyncio.run(cancelled())
    assert cancelled_handle.abort_calls == 1

    lease_mod._reset_lease_state_for_tests()
    teardown_handle = _FakeHandle(
        lambda frame, call: SandboxTeardownError("tree remained live")
    )
    _install_fakes(monkeypatch, artifacts, teardown_handle)

    async def teardown() -> None:
        session = session_mod.ParakeetProcessSession(
            expected_rows=1, vad=True, should_cancel=None
        )
        with pytest.raises(SandboxTeardownError):
            await session.transcribe("one.wav")
        assert session.teardown_failed is True

    asyncio.run(teardown())


def test_process_local_lease_is_nonblocking_releasable_and_poisonable() -> None:
    def acquire() -> lease_mod.LocalEngineLease:
        return lease_mod.acquire_local_engine_lease("parakeet")

    first = acquire()
    with pytest.raises(lease_mod.LocalEngineLeaseBusy):
        acquire()
    first.release()
    second = acquire()
    second.poison()
    with pytest.raises(lease_mod.LocalEngineLeaseBusy, match="restart"):
        acquire()


def test_poisoning_one_engine_does_not_block_a_different_engine() -> None:
    parakeet = lease_mod.acquire_local_engine_lease("parakeet")
    parakeet.poison()
    with pytest.raises(lease_mod.LocalEngineLeaseBusy, match="restart"):
        lease_mod.acquire_local_engine_lease("parakeet")
    # A different engine is unaffected by parakeet's poisoned, unprovable
    # teardown: only parakeet itself is barred until process restart.
    ocr = lease_mod.acquire_local_engine_lease("ocr")
    ocr.release()


def test_policy_scales_cpu_safely_and_keeps_inference_offline() -> None:
    assert session_mod.cumulative_cpu_seconds(0) == 3_600
    assert session_mod.cumulative_cpu_seconds(3) == 10_800
    assert session_mod.cumulative_cpu_seconds(10**20) == session_mod.MAX_CPU_SECONDS
    policy = session_mod.parakeet_inference_policy(3)
    assert policy.cpu_seconds == 10_800
    assert policy.memory_mb == 4096
    assert policy.allow_network is False
    assert policy.trusted_python_netwall is True
    assert set(policy.allowed_extra_env) == {"HF_HUB_CACHE", "HF_HUB_OFFLINE"}
