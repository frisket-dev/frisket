from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from frisket.engine._workers import rapidocr_session as session_mod
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


def _runtime(**overrides: Any) -> dict[str, Any]:
    value = {
        "rapidocr_version": "3.8.1",
        "onnxruntime_version": "1.26.0",
        "onnx_threads": {
            "mode": "shared_global",
            "intra": 2,
            "inter": 1,
            "sessions_verified": 3,
        },
        "opencv_threads": 1,
        "opencv_threads_observed": 1,
        "opencv_opencl": False,
        "model_init_ms": 125.5,
    }
    value.update(overrides)
    return value


def _ready(**overrides: Any) -> bytes:
    values = {"engine": "rapidocr", "runtime": _runtime()}
    values.update(overrides)
    return _frame("ready", **values)


def _ack(request_id: str, *, page_count: int) -> bytes:
    return _frame(
        "result",
        request_id=request_id,
        ok=True,
        metrics={
            "duration_ms": 10.5,
            "page_count": page_count,
            "det_ms": 3.0,
            "cls_ms": 1.0,
            "rec_ms": 5.0,
            "recognized_blocks": page_count,
            "input_pixels": 100 * page_count,
            "max_page_pixels": 100,
            "max_page_width": 10,
            "max_page_height": 10,
            "vm_hwm_bytes": 1000,
            "vm_peak_bytes": 2000,
            "threads": 6,
        },
    )


class _FakeHandle:
    def __init__(
        self,
        responder: Callable[[dict[str, Any], "_FakeHandle"], bytes | BaseException]
        | None = None,
        *,
        pid: int | None = 4242,
    ) -> None:
        self.responder = responder
        self.pid = pid
        self.root: Path | None = None
        self.exchanges: list[dict[str, Any]] = []
        self.close_frames: list[dict[str, Any]] = []
        self.abort_calls = 0

    async def exchange_frame(self, payload, *, wall_seconds, response_limit):
        del wall_seconds, response_limit
        frame = json.loads(payload)
        self.exchanges.append(frame)
        if self.responder is not None:
            response = self.responder(frame, self)
            if isinstance(response, BaseException):
                raise response
            return response
        if frame["type"] == "init":
            return _ready()
        self.write_result(frame)
        return _ack(frame["request_id"], page_count=len(frame["input"]["paths"]))

    def write_result(
        self, frame: dict[str, Any], *, text: str = "hello", malformed: Any = None
    ) -> None:
        assert self.root is not None
        output = self.root / Path(frame["output"]["path"])
        value = (
            malformed
            if malformed is not None
            else {
                "pages": [
                    {
                        "text": text,
                        "blocks": [
                            {
                                "text": text,
                                "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                                "score": 0.99,
                            }
                        ],
                    }
                    for _ in frame["input"]["paths"]
                ]
            }
        )
        output.write_text(json.dumps(value), encoding="utf-8")

    async def close(self, payload, *, wall_seconds):
        del wall_seconds
        self.close_frames.append(json.loads(payload))
        return 0

    async def abort(self):
        self.abort_calls += 1


def _install_handle(
    monkeypatch: pytest.MonkeyPatch, handle: _FakeHandle
) -> dict[str, Any]:
    captured: dict[str, Any] = {"opens": 0}

    async def open_process(*args, **kwargs):
        del args
        captured["opens"] += 1
        captured["policy"] = kwargs["policy"]
        captured["scratch_dir"] = Path(kwargs["scratch_dir"])
        assert callable(kwargs["should_cancel"])
        handle.root = Path(kwargs["scratch_dir"])
        return handle

    monkeypatch.setattr(
        session_mod.sandbox_shim, "open_sandboxed_process", open_process
    )
    return captured


def _session(**overrides: Any) -> session_mod.RapidOCRProcessSession:
    values = {
        "worker_index": 1,
        "expected_rows": 2,
        "language": None,
        "model_root_dir": None,
        "onnx_intra_threads": 2,
        "onnx_inter_threads": 1,
        "opencv_threads": 1,
        "should_cancel": None,
    }
    values.update(overrides)
    return session_mod.RapidOCRProcessSession(**values)


@pytest.mark.parametrize(
    ("cpus", "workers", "intra"),
    ((1, 1, 1), (2, 2, 1), (3, 2, 1), (4, 2, 2), (8, 2, 4)),
)
def test_topology_uses_effective_cpu_for_multirow_default(
    monkeypatch: pytest.MonkeyPatch, cpus: int, workers: int, intra: int
) -> None:
    monkeypatch.setattr(session_mod, "_effective_cpu_count", lambda: cpus)
    monkeypatch.setattr(session_mod, "_effective_memory_bytes", lambda: 8 * 1024**3)
    monkeypatch.delenv("FRISKET_RAPIDOCR_WORKERS", raising=False)
    monkeypatch.delenv("FRISKET_RAPIDOCR_ONNX_THREADS", raising=False)
    monkeypatch.delenv("FRISKET_RAPIDOCR_OPENCV_THREADS", raising=False)

    topology = session_mod.rapidocr_topology()
    assert (topology.workers, topology.onnx_intra_threads) == (workers, intra)
    assert topology.onnx_inter_threads == topology.opencv_threads == 1
    assert topology.memory_mb == (4096 if workers == 1 else 3584)


def test_topology_uses_one_wide_worker_for_one_row_and_clamps_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_mod, "_effective_cpu_count", lambda: 4)
    monkeypatch.setattr(session_mod, "_effective_memory_bytes", lambda: 8 * 1024**3)
    for name in (
        "FRISKET_RAPIDOCR_WORKERS",
        "FRISKET_RAPIDOCR_ONNX_THREADS",
        "FRISKET_RAPIDOCR_OPENCV_THREADS",
    ):
        monkeypatch.delenv(name, raising=False)

    single = session_mod.rapidocr_topology(expected_rows=1)
    assert (single.workers, single.onnx_intra_threads) == (1, 4)
    multi = session_mod.rapidocr_topology(expected_rows=2)
    assert (multi.workers, multi.onnx_intra_threads) == (2, 2)

    monkeypatch.setenv("FRISKET_RAPIDOCR_WORKERS", "1")
    monkeypatch.setenv("FRISKET_RAPIDOCR_ONNX_THREADS", "2")
    overridden = session_mod.rapidocr_topology(expected_rows=4)
    assert (overridden.workers, overridden.onnx_intra_threads) == (1, 2)

    monkeypatch.setenv("FRISKET_RAPIDOCR_WORKERS", "99")
    with pytest.raises(ValueError, match="FRISKET_RAPIDOCR_WORKERS"):
        session_mod.rapidocr_topology()


@pytest.mark.parametrize(("host_gib", "child_mb"), ((4, 3072), (3, 2048), (2, 1024)))
def test_topology_clamps_two_workers_on_small_memory(
    monkeypatch: pytest.MonkeyPatch, host_gib: int, child_mb: int
) -> None:
    monkeypatch.setattr(session_mod, "_effective_cpu_count", lambda: 4)
    monkeypatch.setattr(
        session_mod, "_effective_memory_bytes", lambda: host_gib * 1024**3
    )
    monkeypatch.delenv("FRISKET_RAPIDOCR_WORKERS", raising=False)
    monkeypatch.delenv("FRISKET_RAPIDOCR_ONNX_THREADS", raising=False)
    monkeypatch.delenv("FRISKET_RAPIDOCR_OPENCV_THREADS", raising=False)
    topology = session_mod.rapidocr_topology()
    assert (topology.workers, topology.onnx_intra_threads) == (1, 4)
    assert topology.memory_mb == child_mb


def test_topology_uses_default_child_memory_when_host_memory_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_mod, "_effective_cpu_count", lambda: 4)
    monkeypatch.setattr(session_mod, "_effective_memory_bytes", lambda: None)
    monkeypatch.delenv("FRISKET_RAPIDOCR_WORKERS", raising=False)
    monkeypatch.delenv("FRISKET_RAPIDOCR_ONNX_THREADS", raising=False)
    monkeypatch.delenv("FRISKET_RAPIDOCR_OPENCV_THREADS", raising=False)
    topology = session_mod.rapidocr_topology()
    assert topology.workers == 2
    assert topology.memory_mb == session_mod.DEFAULT_MEMORY_MB


def test_session_is_lazy_reuses_child_and_copies_only_staged_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_path = tmp_path / "SECRET-SOURCE-NAME.png"
    secret_path.write_bytes(b"private image bytes")
    secret_text = "SECRET OCR TEXT"

    def respond(frame: dict[str, Any], handle: _FakeHandle) -> bytes:
        if frame["type"] == "init":
            return _ready()
        assert handle.root is not None
        assert str(secret_path) not in json.dumps(frame)
        for relative in frame["input"]["paths"]:
            assert (handle.root / relative).read_bytes() == secret_path.read_bytes()
        handle.write_result(frame, text=secret_text)
        return _ack(frame["request_id"], page_count=len(frame["input"]["paths"]))

    handle = _FakeHandle(respond)
    captured = _install_handle(monkeypatch, handle)
    caplog.set_level("INFO", logger=session_mod.logger.name)

    async def run() -> None:
        session = _session()
        assert session.started is False
        pages = await session.ocr([secret_path])
        assert pages[0]["text"] == secret_text
        assert not any(captured["scratch_dir"].joinpath("requests").iterdir())
        pages = await session.ocr([secret_path])
        assert pages[0]["blocks"][0]["score"] == 0.99
        assert session.started is True
        staging_root = captured["scratch_dir"]
        await session.close()
        assert not staging_root.exists()

    asyncio.run(run())
    assert captured["opens"] == 1
    assert captured["policy"].trusted_python_netwall is True
    assert [frame["type"] for frame in handle.exchanges] == [
        "init",
        "request",
        "request",
    ]
    assert handle.close_frames == [
        {"schema_version": session_mod.SCHEMA_VERSION, "type": "close"}
    ]
    rendered = "\n".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )
    assert str(secret_path) not in rendered
    assert secret_text not in rendered
    request_records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "rapidocr_request_completed"
    ]
    assert len(request_records) == 2
    assert request_records[0].native_det_ms == 3.0
    assert request_records[0].recognized_blocks == 1
    assert request_records[0].input_pixels == 100
    assert request_records[0].max_page_pixels == 100
    assert request_records[0].staging_ms >= 0
    assert request_records[0].total_duration_ms >= request_records[0].duration_ms
    assert request_records[0].worker_threads == 6


def test_valid_row_error_keeps_session_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "page.png"
    image.write_bytes(b"image")

    def respond(frame: dict[str, Any], handle: _FakeHandle) -> bytes:
        if frame["type"] == "init":
            return _ready()
        if frame["request_id"] == "r1":
            return _frame(
                "result",
                request_id="r1",
                ok=False,
                error={
                    "code": "image_ocr_failed",
                    "message": "images could not be read or recognized",
                },
            )
        handle.write_result(frame, text="recovered")
        return _ack(frame["request_id"], page_count=1)

    handle = _FakeHandle(respond)
    _install_handle(monkeypatch, handle)

    async def run() -> None:
        session = _session()
        with pytest.raises(session_mod.RapidOCRRowError):
            await session.ocr([image])
        assert (await session.ocr([image]))[0]["text"] == "recovered"
        await session.close()

    asyncio.run(run())
    assert handle.abort_calls == 0


def test_symlinked_result_is_rejected_and_worker_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "page.png"
    image.write_bytes(b"image")

    def respond(frame: dict[str, Any], handle: _FakeHandle) -> bytes:
        if frame["type"] == "init":
            return _ready()
        assert handle.root is not None
        output = handle.root / frame["output"]["path"]
        output.symlink_to(image)
        return _ack(frame["request_id"], page_count=1)

    handle = _FakeHandle(respond)
    _install_handle(monkeypatch, handle)

    async def run() -> None:
        session = _session()
        with pytest.raises(session_mod.RapidOCRSessionFailure, match="result file"):
            await session.ocr([image])
        assert session.closed is True

    asyncio.run(run())
    assert handle.abort_calls == 1


def test_cancellation_and_unverified_teardown_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "page.png"
    image.write_bytes(b"image")
    cancelled_handle = _FakeHandle(
        lambda frame, handle: (
            _ready()
            if frame["type"] == "init"
            else SandboxProcessCancelledError("cancelled")
        )
    )
    _install_handle(monkeypatch, cancelled_handle)

    async def cancelled() -> None:
        session = _session()
        with pytest.raises(session_mod.RapidOCRSessionCancelled):
            await session.ocr([image])
        assert session.teardown_failed is False

    asyncio.run(cancelled())
    assert cancelled_handle.abort_calls == 1

    teardown_handle = _FakeHandle(
        lambda frame, handle: (
            _ready()
            if frame["type"] == "init"
            else SandboxTeardownError("tree remained live")
        )
    )
    captured = _install_handle(monkeypatch, teardown_handle)

    async def teardown() -> None:
        session = _session()
        with pytest.raises(SandboxTeardownError):
            await session.ocr([image])
        assert session.teardown_failed is True
        assert captured["scratch_dir"].exists()

    asyncio.run(teardown())


def test_pool_is_lazy_bounded_and_closes_every_created_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    peak = 0
    instances: list[Any] = []
    release = asyncio.Event()

    class FakeSession:
        def __init__(self, **kwargs):
            self.worker_index = kwargs["worker_index"]
            self.started = False
            self.teardown_failed = False
            self.closed = False
            instances.append(self)

        async def ocr(self, paths):
            nonlocal active, peak
            del paths
            self.started = True
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1
            return [{"text": str(self.worker_index), "blocks": []}]

        async def close(self):
            self.closed = True

    monkeypatch.setattr(session_mod, "RapidOCRProcessSession", FakeSession)

    async def run() -> None:
        pool = session_mod.RapidOCRProcessPool(
            max_workers=2,
            expected_rows=3,
            language=None,
            model_root_dir=None,
            onnx_intra_threads=2,
            onnx_inter_threads=1,
            opencv_threads=1,
            should_cancel=None,
        )
        assert pool.started_workers == 0
        tasks = [asyncio.create_task(pool.ocr(["x"])) for _ in range(3)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(instances) == 2
        release.set()
        results = await asyncio.gather(*tasks)
        assert len(results) == 3
        assert pool.started_workers == 2
        await pool.close()
        assert pool.closed is True
        assert all(instance.closed for instance in instances)

    asyncio.run(run())
    assert peak == 2


def test_pool_close_aggregates_every_concurrent_session_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    class FailingSession:
        def __init__(self, **kwargs: Any) -> None:
            self.worker_index = kwargs["worker_index"]
            self.started = False
            self.teardown_failed = False
            self.closed = False

        async def ocr(self, paths: Any) -> list[dict[str, Any]]:
            del paths
            self.started = True
            await release.wait()
            return [{"text": str(self.worker_index), "blocks": []}]

        async def close(self) -> None:
            self.closed = True
            raise session_mod.RapidOCRSessionFailure(
                f"worker {self.worker_index} could not shut down"
            )

    monkeypatch.setattr(session_mod, "RapidOCRProcessSession", FailingSession)

    async def run() -> None:
        pool = session_mod.RapidOCRProcessPool(
            max_workers=2,
            expected_rows=2,
            language=None,
            model_root_dir=None,
            onnx_intra_threads=2,
            onnx_inter_threads=1,
            opencv_threads=1,
            should_cancel=None,
        )
        tasks = [asyncio.create_task(pool.ocr(["x"])) for _ in range(2)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        assert pool.started_workers == 2
        with pytest.raises(session_mod.RapidOCRSessionFailure) as excinfo:
            await pool.close()
        message = str(excinfo.value)
        assert "worker 1 could not shut down" in message
        assert "worker 2 could not shut down" in message
        assert len(excinfo.value.close_failures) == 2
        # A second close re-raises the same aggregated diagnostic.
        with pytest.raises(session_mod.RapidOCRSessionFailure) as again:
            await pool.close()
        assert again.value is excinfo.value

    asyncio.run(run())


def test_pool_close_keeps_teardown_precedence_and_still_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    class MixedFailureSession:
        def __init__(self, **kwargs: Any) -> None:
            self.worker_index = kwargs["worker_index"]
            self.started = False
            self.teardown_failed = False
            self.closed = False

        async def ocr(self, paths: Any) -> list[dict[str, Any]]:
            del paths
            self.started = True
            await release.wait()
            return [{"text": str(self.worker_index), "blocks": []}]

        async def close(self) -> None:
            self.closed = True
            if self.worker_index == 1:
                self.teardown_failed = True
                raise SandboxTeardownError(f"worker {self.worker_index} tree live")
            raise session_mod.RapidOCRSessionFailure(
                f"worker {self.worker_index} protocol failure"
            )

    monkeypatch.setattr(session_mod, "RapidOCRProcessSession", MixedFailureSession)

    async def run() -> None:
        pool = session_mod.RapidOCRProcessPool(
            max_workers=2,
            expected_rows=2,
            language=None,
            model_root_dir=None,
            onnx_intra_threads=2,
            onnx_inter_threads=1,
            opencv_threads=1,
            should_cancel=None,
        )
        tasks = [asyncio.create_task(pool.ocr(["x"])) for _ in range(2)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        with pytest.raises(SandboxTeardownError) as excinfo:
            await pool.close()
        message = str(excinfo.value)
        assert "worker 1 tree live" in message
        assert "worker 2 protocol failure" in message
        assert pool.teardown_failed is True

    asyncio.run(run())


def test_ready_and_ack_validation_reject_unverified_or_unbounded_metrics() -> None:
    valid = json.loads(_ready())
    runtime = session_mod._validate_ready(
        valid, requested_intra=2, requested_inter=1, requested_opencv=1
    )
    assert runtime["onnx_threads"]["sessions_verified"] == 3

    invalid = json.loads(_ready())
    invalid["runtime"]["onnx_threads"]["sessions_verified"] = 2
    with pytest.raises(session_mod.RapidOCRSessionFailure, match="runtime"):
        session_mod._validate_ready(
            invalid, requested_intra=2, requested_inter=1, requested_opencv=1
        )

    bad_ack = json.loads(_ack("r1", page_count=1))
    bad_ack["metrics"]["vm_peak_bytes"] = "lots"
    with pytest.raises(session_mod.RapidOCRSessionFailure, match="metrics"):
        session_mod._validate_ack(bad_ack, "r1", expected_pages=1)
