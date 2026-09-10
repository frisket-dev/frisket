"""Parakeet loads with an explicit ONNX provider.

ONNX Runtime's CoreMLExecutionProvider fails to even initialize on
nemo-parakeet-tdt-0.6b-v2
("model_path must not be empty", initializer.cc:45 -- the known CoreML-EP
bytes-loaded-model bug). A direct probe confirmed
``onnx_asr.load_model(PARAKEET_MODEL, providers=["CPUExecutionProvider"])``
loads clean. The run-scoped worker forces that provider for both model
and VAD initialization. These tests execute the real worker implementation
against fake modules, proving the override wins even when ONNX Runtime reports
CoreML as available -- CoreML is never attempted, not merely absent here.

Offline only: a fake ``onnx_asr`` + ``onnxruntime`` module is injected into
``sys.modules`` (no 600MB model download in CI). One optional test at the
bottom exercises the real load when the model is already HF-cached locally
(same skip convention as ``test_transcribe_engines.py``'s ``needs_parakeet``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from frisket.engine._workers.parakeet_artifacts import (
    PARAKEET_MODEL,
    PARAKEET_MODEL_FILES,
    PARAKEET_MODEL_REVISION,
    PARAKEET_VAD_FILES,
    PARAKEET_VAD_REVISION,
    ParakeetArtifacts,
)
from frisket.engine._workers.parakeet_session import parakeet_init_frame
from frisket.engine._workers import parakeet_worker
from frisket.engine._workers.parakeet_worker import _load_runtime, _transcribe


class _FakeVad:
    pass


class _FakeSessionOptions:
    def __init__(self) -> None:
        self.intra_op_num_threads: int | None = None
        self.inter_op_num_threads: int | None = None
        self.use_per_session_threads = True
        self.config_entries: dict[str, str] = {}

    def add_session_config_entry(self, key: str, value: str) -> None:
        self.config_entries[key] = value


class _FakeRecognizeResult:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class _FakeTimestampResult:
    text = "hi there"
    tokens = ("hi", "there")
    timestamps = (0.0, 0.3)


class _FakeModel:
    vad_batch_sizes: list[int] = []

    def with_vad(self, vad: Any, *, batch_size: int) -> "_FakeModel":
        del vad
        self.vad_batch_sizes.append(batch_size)
        return self

    def with_timestamps(self) -> "_FakeModel":
        return self

    def recognize(self, waveform: Any, sample_rate: int | None = None) -> Any:
        del waveform, sample_rate
        if _FakeOnnxAsr.mode == "vad":
            return [_FakeRecognizeResult(0.0, 0.6, "hi there")]
        return _FakeTimestampResult()


class _FakeOnnxAsr:
    """Records every load_model/load_vad call so tests can inspect the
    providers argument actually threaded through the private worker."""

    mode = "vad"
    load_model_calls: list[dict[str, Any]] = []
    load_vad_calls: list[dict[str, Any]] = []
    runtime_threads: dict[str, Any] = {}

    @staticmethod
    def load_model(
        model_id: str,
        path: str | Path,
        *,
        quantization: str,
        sess_options: _FakeSessionOptions,
        providers: list[str],
    ) -> Any:
        _FakeOnnxAsr.load_model_calls.append(
            {
                "model": model_id,
                "path": Path(path),
                "quantization": quantization,
                "sess_options": sess_options,
                "providers": providers,
            }
        )
        return _FakeModel()

    @staticmethod
    def load_vad(
        name: str,
        path: str | Path,
        *,
        sess_options: _FakeSessionOptions,
        providers: list[str],
    ) -> Any:
        _FakeOnnxAsr.load_vad_calls.append(
            {
                "name": name,
                "path": Path(path),
                "sess_options": sess_options,
                "providers": providers,
            }
        )
        return _FakeVad()


def _fake_onnxruntime(available: list[str], *, global_pool: str = "available") -> Any:
    module = type(sys)("onnxruntime")
    module.__version__ = "1.26.0"
    module.get_available_providers = lambda: list(available)  # type: ignore[attr-defined]
    module.SessionOptions = _FakeSessionOptions  # type: ignore[attr-defined]
    module.global_thread_pool_calls = []  # type: ignore[attr-defined]
    if global_pool != "missing":

        def set_global_thread_pool_sizes(intra_op: int, inter_op: int) -> None:
            module.global_thread_pool_calls.append((intra_op, inter_op))  # type: ignore[attr-defined]
            if global_pool == "broken":
                raise RuntimeError("global pool unavailable")

        module.set_global_thread_pool_sizes = set_global_thread_pool_sizes  # type: ignore[attr-defined]
    return module


def _run_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    available_providers: list[str],
    payload_extra: dict[str, Any] | None = None,
    mode: str = "vad",
    requests: int = 1,
    global_pool: str = "available",
) -> dict[str, Any]:
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 40)  # RIFF magic only: load_audio's
    # fast path reads the model natively by path and never touches the bytes
    # past the 4-byte sniff, so a real WAV body isn't needed here.

    _FakeOnnxAsr.mode = mode
    _FakeOnnxAsr.load_model_calls = []
    _FakeOnnxAsr.load_vad_calls = []
    _FakeModel.vad_batch_sizes = []
    monkeypatch.setitem(sys.modules, "onnx_asr", _FakeOnnxAsr)
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        _fake_onnxruntime(available_providers, global_pool=global_pool),
    )

    model_path = tmp_path / "model"
    model_path.mkdir()
    for filename in PARAKEET_MODEL_FILES:
        (model_path / filename).write_bytes(b"fixture")
    vad_path = tmp_path / "vad"
    vad_path.mkdir()
    for filename in PARAKEET_VAD_FILES:
        (vad_path / filename).write_bytes(b"fixture")

    vad = bool((payload_extra or {}).get("vad", True))
    init = parakeet_init_frame(
        ParakeetArtifacts(
            cache_dir=tmp_path / "cache",
            model_path=model_path,
            model_revision=PARAKEET_MODEL_REVISION,
            vad_path=vad_path if vad else None,
            vad_revision=PARAKEET_VAD_REVISION if vad else None,
        ),
        vad=vad,
    )
    runtime = _load_runtime(init)
    _FakeOnnxAsr.runtime_threads = dict(runtime.onnx_threads)
    result: dict[str, Any] = {}
    for _ in range(requests):
        result = _transcribe(runtime, str(wav))
    return result


# ---------------------------------------------------------------------------
# the fix: CPU is forced even when CoreML is available


def test_worker_forces_cpu_provider_even_when_coreml_is_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """This is the live-bug regression: on macOS, onnxruntime reports
    CoreMLExecutionProvider as available and would auto-select it if the
    worker omitted ``providers=``. Assert the explicit override wins."""
    monkeypatch.setattr(parakeet_worker.os, "cpu_count", lambda: 4)
    out = _run_worker(
        monkeypatch,
        tmp_path,
        available_providers=["CPUExecutionProvider", "CoreMLExecutionProvider"],
    )
    assert "error" not in out, out
    assert len(_FakeOnnxAsr.load_model_calls) == 1
    assert _FakeOnnxAsr.load_model_calls[0]["providers"] == ["CPUExecutionProvider"]
    assert _FakeOnnxAsr.load_model_calls[0]["path"].name == "model"
    assert len(_FakeOnnxAsr.load_vad_calls) == 1
    assert _FakeOnnxAsr.load_vad_calls[0]["providers"] == ["CPUExecutionProvider"]
    assert _FakeOnnxAsr.load_vad_calls[0]["path"].name == "vad"
    options = _FakeOnnxAsr.load_model_calls[0]["sess_options"]
    assert options is _FakeOnnxAsr.load_vad_calls[0]["sess_options"]
    assert options.use_per_session_threads is False
    assert options.intra_op_num_threads is None
    assert options.inter_op_num_threads is None
    assert options.config_entries == {}
    assert sys.modules["onnxruntime"].global_thread_pool_calls == [(3, 1)]  # type: ignore[attr-defined]
    assert _FakeOnnxAsr.runtime_threads == {
        "mode": "shared_global",
        "intra": 3,
        "inter": 1,
        "cpu_count": 4,
        "ort_version": "1.26.0",
    }
    assert _FakeModel.vad_batch_sizes == [1]


@pytest.mark.parametrize("global_pool", ["missing", "broken"])
def test_worker_falls_back_to_bounded_per_session_threads(
    global_pool: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Older or incomplete ORT global-pool APIs retain the safe 1x1 shape."""

    _run_worker(
        monkeypatch,
        tmp_path,
        available_providers=["CPUExecutionProvider"],
        global_pool=global_pool,
    )

    options = _FakeOnnxAsr.load_model_calls[0]["sess_options"]
    assert options is _FakeOnnxAsr.load_vad_calls[0]["sess_options"]
    assert options.use_per_session_threads is True
    assert options.intra_op_num_threads == 1
    assert options.inter_op_num_threads == 1
    assert options.config_entries == {"session.intra_op.allow_spinning": "0"}
    assert _FakeOnnxAsr.runtime_threads["mode"] == "per_session_fallback"
    assert _FakeOnnxAsr.runtime_threads["intra"] == 1
    assert _FakeOnnxAsr.runtime_threads["inter"] == 1
    assert _FakeOnnxAsr.runtime_threads["ort_version"] == "1.26.0"
    assert _FakeModel.vad_batch_sizes == [1]


def test_worker_loads_the_default_model_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _run_worker(monkeypatch, tmp_path, available_providers=["CPUExecutionProvider"])
    assert _FakeOnnxAsr.load_model_calls[0]["model"] == PARAKEET_MODEL
    assert _FakeOnnxAsr.load_model_calls[0]["quantization"] == "int8"


def test_two_requests_reuse_one_model_and_vad_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _run_worker(
        monkeypatch,
        tmp_path,
        available_providers=["CPUExecutionProvider"],
        requests=2,
    )
    assert len(_FakeOnnxAsr.load_model_calls) == 1
    assert len(_FakeOnnxAsr.load_vad_calls) == 1


def test_contract_has_no_caller_selectable_model_override() -> None:
    from frisket.actions.media import TranscribeParams

    with pytest.raises(ValueError, match="extra_forbidden"):
        TranscribeParams.model_validate(
            {
                "source": "media",
                "engine": "parakeet-tdt",
                "parakeet_model": "some/other-checkpoint",
            }
        )


def test_worker_reports_a_clear_error_when_cpu_provider_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No silent CoreML fallback: if CPUExecutionProvider genuinely isn't
    available, the worker fails loudly instead of loading anything."""
    with pytest.raises(RuntimeError, match="CPUExecutionProvider is unavailable"):
        _run_worker(
            monkeypatch, tmp_path, available_providers=["CoreMLExecutionProvider"]
        )
    assert _FakeOnnxAsr.load_model_calls == []


def test_worker_word_timestamps_path_also_forces_cpu_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """vad=False (word-level timestamps) shares the same load_model call --
    the provider fix isn't VAD-path-specific."""
    out = _run_worker(
        monkeypatch,
        tmp_path,
        available_providers=["CPUExecutionProvider", "CoreMLExecutionProvider"],
        payload_extra={"vad": False},
        mode="timestamps",
    )
    assert "error" not in out, out
    assert _FakeOnnxAsr.load_model_calls[0]["providers"] == ["CPUExecutionProvider"]
    # vad=False never touches load_vad
    assert _FakeOnnxAsr.load_vad_calls == []


# ---------------------------------------------------------------------------
# optional live probe -- exercises the real onnx_asr load only when the
# model is already cached locally (same convention as
# test_transcribe_engines.py's needs_parakeet; no download triggered here).


def _parakeet_cached() -> bool:
    hub = Path(
        os.environ.get(
            "HF_HUB_CACHE",
            Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
            / "hub",
        )
    )
    return any(hub.glob("models--*parakeet-tdt-0.6b-v2*"))


@pytest.mark.skipif(
    not _parakeet_cached(), reason="parakeet-tdt onnx model not in the HF cache"
)
def test_real_onnx_asr_load_model_accepts_explicit_cpu_provider() -> None:
    """Direct reproduction of the coordinator's live probe: real onnx_asr,
    real onnxruntime, explicit CPUExecutionProvider -- must load clean."""
    onnx_asr = pytest.importorskip("onnx_asr")
    hub = Path(
        os.environ.get(
            "HF_HUB_CACHE",
            Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
            / "hub",
        )
    )
    snapshots = list(
        hub.glob(
            "models--istupakov--parakeet-tdt-0.6b-v2-onnx/snapshots/"
            f"{PARAKEET_MODEL_REVISION}"
        )
    )
    assert snapshots
    model = onnx_asr.load_model(
        PARAKEET_MODEL,
        path=snapshots[0],
        quantization="int8",
        providers=["CPUExecutionProvider"],
    )
    assert model is not None
