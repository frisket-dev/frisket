from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.engine._workers import rapidocr_worker as worker


class _SessionOptions:
    def __init__(self) -> None:
        self.use_per_session_threads = True


class _OrtSession:
    calls = 0

    @staticmethod
    def _init_sess_opts(config: Any) -> _SessionOptions:
        del config
        _OrtSession.calls += 1
        return _SessionOptions()


def _fake_ort(*, global_pool: bool = True) -> Any:
    calls: list[tuple[int, int]] = []
    value = SimpleNamespace(SessionOptions=_SessionOptions, calls=calls)
    if global_pool:

        def set_global_thread_pool_sizes(intra: int, inter: int) -> None:
            calls.append((intra, inter))

        value.set_global_thread_pool_sizes = set_global_thread_pool_sizes
    return value


def test_shared_pool_is_claimed_only_after_three_verified_session_options() -> None:
    ort = _fake_ort()
    _OrtSession.calls = 0
    original = _OrtSession._init_sess_opts
    with worker._shared_ort_session_options(
        ort, _OrtSession, intra_threads=2, inter_threads=1
    ) as configuration:
        options = [_OrtSession._init_sess_opts({}) for _ in range(3)]
        assert all(option.use_per_session_threads is False for option in options)
        assert configuration == {
            "mode": "shared_global",
            "intra": 2,
            "inter": 1,
            "sessions_verified": 3,
        }
    assert ort.calls == [(2, 1)]
    assert _OrtSession._init_sess_opts is original


def test_missing_global_api_falls_back_to_one_by_one() -> None:
    ort = _fake_ort(global_pool=False)
    with worker._shared_ort_session_options(
        ort, _OrtSession, intra_threads=4, inter_threads=1
    ) as configuration:
        assert configuration == {
            "mode": "per_session_fallback",
            "intra": 1,
            "inter": 1,
            "sessions_verified": 0,
        }


def test_missing_global_pool_property_falls_back_before_setter() -> None:
    calls: list[tuple[int, int]] = []
    ort = SimpleNamespace(
        SessionOptions=lambda: SimpleNamespace(),
        set_global_thread_pool_sizes=lambda intra, inter: calls.append((intra, inter)),
    )
    with worker._shared_ort_session_options(
        ort, _OrtSession, intra_threads=4, inter_threads=1
    ) as configuration:
        assert configuration["mode"] == "per_session_fallback"
    assert calls == []


def test_global_pool_setter_failure_fails_closed_and_restores_factory() -> None:
    def fail_setter(intra: int, inter: int) -> None:
        del intra, inter
        raise RuntimeError("partially mutated global state")

    ort = SimpleNamespace(
        SessionOptions=_SessionOptions,
        set_global_thread_pool_sizes=fail_setter,
    )
    original = _OrtSession._init_sess_opts
    with pytest.raises(RuntimeError, match="global thread-pool setup failed"):
        with worker._shared_ort_session_options(
            ort, _OrtSession, intra_threads=4, inter_threads=1
        ):
            pass
    assert _OrtSession._init_sess_opts is original


def test_wrapper_install_failure_uses_safe_fallback_without_global_setter() -> None:
    class RejectWrapper(type):
        def __setattr__(cls, name: str, value: Any) -> None:
            function = getattr(value, "__func__", value)
            if (
                name == "_init_sess_opts"
                and getattr(function, "__name__", "") == "shared_options"
            ):
                raise TypeError("class is immutable")
            super().__setattr__(name, value)

    class ImmutableOrtSession(metaclass=RejectWrapper):
        @staticmethod
        def _init_sess_opts(config: Any) -> _SessionOptions:
            del config
            return _SessionOptions()

    ort = _fake_ort()
    with worker._shared_ort_session_options(
        ort, ImmutableOrtSession, intra_threads=4, inter_threads=1
    ) as configuration:
        assert configuration["mode"] == "per_session_fallback"
    assert ort.calls == []


def test_ocr_pages_preserves_output_and_sums_native_metrics() -> None:
    class Engine:
        def __call__(self, path: str) -> Any:
            del path
            return SimpleNamespace(
                img=SimpleNamespace(shape=(1200, 2000, 3)),
                txts=(" First ", "Second"),
                boxes=(
                    ((0.1, 0.2), (1.1, 0.2), (1.1, 1.2), (0.1, 1.2)),
                    ((2, 2), (3, 2), (3, 3), (2, 3)),
                ),
                scores=(0.98765, 0.5),
                elapse_list=(0.01, 0.02, 0.03),
            )

    pages, metrics = worker._ocr_pages(Engine(), ["one", "two"])
    assert pages == [
        {
            "text": "First\nSecond",
            "blocks": [
                {
                    "text": " First ",
                    "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "score": 0.9877,
                },
                {
                    "text": "Second",
                    "bbox": [[2, 2], [3, 2], [3, 3], [2, 3]],
                    "score": 0.5,
                },
            ],
        },
        {
            "text": "First\nSecond",
            "blocks": [
                {
                    "text": " First ",
                    "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "score": 0.9877,
                },
                {
                    "text": "Second",
                    "bbox": [[2, 2], [3, 2], [3, 3], [2, 3]],
                    "score": 0.5,
                },
            ],
        },
    ]
    assert metrics == {
        "det_ms": 20.0,
        "cls_ms": 40.0,
        "rec_ms": 60.0,
        "recognized_blocks": 4,
        "input_pixels": 4_800_000,
        "max_page_pixels": 2_400_000,
        "max_page_width": 2000,
        "max_page_height": 1200,
    }


def test_blank_result_still_reports_header_pixel_dimensions(tmp_path: Path) -> None:
    from PIL import Image

    image_path = tmp_path / "blank.png"
    Image.new("RGB", (321, 123), "white").save(image_path)

    class Engine:
        def __call__(self, path: str) -> Any:
            assert path == str(image_path)
            return SimpleNamespace(
                img=None,
                txts=None,
                boxes=None,
                scores=None,
                elapse_list=(0.01, None, None),
            )

    pages, metrics = worker._ocr_pages(Engine(), [str(image_path)])
    assert pages == [{"text": "", "blocks": []}]
    assert metrics["input_pixels"] == 321 * 123
    assert metrics["max_page_pixels"] == 321 * 123
    assert metrics["max_page_width"] == 321
    assert metrics["max_page_height"] == 123


def test_request_paths_are_confined_to_request_staging_root(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    request = root / "requests" / "r1"
    request.mkdir(parents=True)
    (request / "page-000000.img").write_bytes(b"image")
    frame = {
        "schema_version": worker.SCHEMA_VERSION,
        "type": "request",
        "request_id": "r1",
        "operation": "ocr",
        "input": {"paths": ["requests/r1/page-000000.img"]},
        "output": {"path": "requests/r1/result.json"},
    }
    request_id, paths, output = worker._validate_request(frame, root)
    assert request_id == "r1"
    assert paths == [str((request / "page-000000.img").resolve())]
    assert output == request / "result.json"

    frame["input"]["paths"] = ["requests/r1/../../secret"]
    with pytest.raises(worker.WorkerProtocolError, match="outside its grant"):
        worker._validate_request(frame, root)


def test_symlinked_staged_input_is_rejected(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    request = root / "requests" / "r1"
    request.mkdir(parents=True)
    target = root / "target.img"
    target.write_bytes(b"image")
    (request / "page-000000.img").symlink_to(target)
    frame = {
        "schema_version": worker.SCHEMA_VERSION,
        "type": "request",
        "request_id": "r1",
        "operation": "ocr",
        "input": {"paths": ["requests/r1/page-000000.img"]},
        "output": {"path": "requests/r1/result.json"},
    }
    with pytest.raises(worker.WorkerProtocolError, match="regular staged file"):
        worker._validate_request(frame, root)


def test_opencv_threads_and_opencl_are_explicitly_bounded() -> None:
    class Ocl:
        enabled = True

        @classmethod
        def setUseOpenCL(cls, value: bool) -> None:
            cls.enabled = value

        @classmethod
        def useOpenCL(cls) -> bool:
            return cls.enabled

    class Cv2:
        threads = 0
        ocl = Ocl

        @classmethod
        def setNumThreads(cls, value: int) -> None:
            cls.threads = value

        @classmethod
        def getNumThreads(cls) -> int:
            return cls.threads

    assert worker._configure_opencv(Cv2, 2) == {
        "requested_threads": 2,
        "observed_threads": 2,
        "opencl": False,
    }


def test_process_metrics_never_contain_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    assert worker._process_metrics() == {
        "vm_hwm_bytes": None,
        "vm_peak_bytes": None,
        "threads": None,
    }
