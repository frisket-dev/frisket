"""Private offline Parakeet inference worker.

This module is deliberately standard-library-only at import time.  ONNX,
NumPy, and PyAV are imported only after the sandbox owns the process and an
``init`` payload names verified immutable artifact directories.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, BinaryIO

from .framing import FrameCodec
from .parakeet_model import MODEL, MODEL_FILES, MODEL_REVISION, VAD_FILES, VAD_REVISION

SCHEMA_VERSION = "frisket.run_scoped_worker.v1"
ENGINE = "parakeet"

SAMPLE_RATE = 16_000
MAX_REQUEST_FRAME_BYTES = 1024 * 1024
MAX_RESPONSE_FRAME_BYTES = 64 * 1024 * 1024
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ORT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")

# The ONNX session shape is owned by this worker as constants rather than
# negotiated in the init frame: int8 weights (the fp32 external-data variant
# trips an onnxruntime model_path bug), CPU-only (CoreML auto-select killed
# workers on macOS). onnx-asr creates encoder, decoder, VAD, and several
# resampler sessions. A separate adaptive pool on every session
# multiplied both threads and address-space reservations enough to exceed the
# worker's 4 GiB sandbox. Newer ONNX Runtime versions let all sessions share
# one bounded pool instead; older versions retain the measured-safe 1x1
# non-spinning per-session fallback.
ONNX_QUANTIZATION = "int8"
ONNX_PROVIDERS = ["CPUExecutionProvider"]
ONNX_GLOBAL_INTRA_OP_CAP = 4
ONNX_INTER_OP_THREADS = 1
ONNX_FALLBACK_INTRA_OP_THREADS = 1


def _onnx_cpu_count() -> int:
    """Return a small, protocol-safe representation of detected CPUs."""

    try:
        cores = os.cpu_count()
    except Exception:
        cores = None
    if (
        isinstance(cores, bool)
        or not isinstance(cores, int)
        or cores < 1
        or cores > 1_000_000
    ):
        return 1
    return cores


def _onnx_global_intra_op_threads(cpu_count: int | None = None) -> int:
    """Leave one CPU for the host workload, with a measured-safe cap of four."""

    cores = _onnx_cpu_count() if cpu_count is None else cpu_count
    return min(max(1, cores - 1), ONNX_GLOBAL_INTRA_OP_CAP)


def _onnx_version(ort: Any) -> str:
    value = getattr(ort, "__version__", None)
    if isinstance(value, str) and _ORT_VERSION_RE.fullmatch(value):
        return value
    return "unknown"


def _configure_onnx_threads(ort: Any, session_options: Any) -> dict[str, Any]:
    """Configure a bounded pool and return its sanitized observable shape."""

    cpu_count = _onnx_cpu_count()
    intra = _onnx_global_intra_op_threads(cpu_count)
    configuration: dict[str, Any] = {
        "mode": "per_session_fallback",
        "intra": ONNX_FALLBACK_INTRA_OP_THREADS,
        "inter": ONNX_INTER_OP_THREADS,
        "cpu_count": cpu_count,
        "ort_version": _onnx_version(ort),
    }
    set_global_pool = getattr(ort, "set_global_thread_pool_sizes", None)
    if callable(set_global_pool) and hasattr(
        session_options, "use_per_session_threads"
    ):
        try:
            session_options.use_per_session_threads = False
            set_global_pool(intra, ONNX_INTER_OP_THREADS)
            configuration.update({"mode": "shared_global", "intra": intra})
            return configuration
        except Exception:
            # Some supported older runtimes expose an incomplete global-pool
            # API. Restore the bounded per-session configuration rather than
            # failing model startup or silently accepting ORT's adaptive pools.
            try:
                session_options.use_per_session_threads = True
            except Exception:
                pass
    session_options.intra_op_num_threads = ONNX_FALLBACK_INTRA_OP_THREADS
    session_options.inter_op_num_threads = ONNX_INTER_OP_THREADS
    return configuration


class WorkerProtocolError(RuntimeError):
    pass


_FRAME_CODEC = FrameCodec(
    max_request_bytes=MAX_REQUEST_FRAME_BYTES,
    max_response_bytes=MAX_RESPONSE_FRAME_BYTES,
    error_type=WorkerProtocolError,
)


class _Runtime:
    def __init__(
        self, *, model: Any, vad: Any, np: Any, onnx_threads: dict[str, Any]
    ) -> None:
        self.model = model
        self.vad = vad
        self.np = np
        self.onnx_threads = onnx_threads


def _protocol_stdout() -> BinaryIO:
    """Reserve the original stdout pipe and redirect ordinary stdout to stderr."""

    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return os.fdopen(protocol_fd, "wb", buffering=0)


def _base_frame(frame_type: str) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "type": frame_type}


def _require_exact_fields(
    value: dict[str, Any], allowed: set[str], *, frame: str
) -> None:
    if set(value) != allowed:
        raise WorkerProtocolError(f"{frame} frame has unknown or missing fields")


def _safe_error_message(_error: BaseException, *, category: str) -> str:
    # Third-party exceptions frequently contain absolute paths and low-level
    # runtime details.  Child text is untrusted protocol data, so return only a
    # bounded parent-owned category; the parent redacts/bounds again.
    return category[:500]


def _validate_files(directory: Path, filenames: tuple[str, ...]) -> None:
    if not directory.is_dir():
        raise FileNotFoundError("artifact snapshot directory is unavailable")
    for filename in filenames:
        if not (directory / filename).is_file():
            raise FileNotFoundError("required artifact file is unavailable")


def _validate_init(frame: dict[str, Any]) -> tuple[Path, Path | None, bool]:
    """Check only what protects correctness: schema/revision pins and artifact
    path existence. The init frame is composed by the trusted parent that
    spawned this child, so the child does not field-validate it (guardrail 16
    — the child defends against the network and a stale artifact store, not
    its parent); the ONNX session shape is this module's constants, not wire
    data."""
    if frame.get("schema_version") != SCHEMA_VERSION or frame.get("type") != "init":
        raise WorkerProtocolError("first frame must be a v1 init frame")
    artifacts = frame.get("artifacts")
    if not isinstance(artifacts, dict):
        raise WorkerProtocolError("init artifacts must be an object")
    if artifacts.get("model_revision") != MODEL_REVISION:
        raise WorkerProtocolError("model revision is unsupported")
    vad_enabled = frame.get("vad")
    if not isinstance(vad_enabled, bool):
        raise WorkerProtocolError("VAD option must be a boolean")
    model_path = Path(str(artifacts.get("model_path", ""))).absolute()
    _validate_files(model_path, MODEL_FILES)
    vad_path: Path | None = None
    if vad_enabled:
        if artifacts.get("vad_revision") != VAD_REVISION:
            raise WorkerProtocolError("VAD revision is unsupported")
        vad_path = Path(str(artifacts.get("vad_path", ""))).absolute()
        _validate_files(vad_path, VAD_FILES)
    return model_path, vad_path, vad_enabled


def _cpu_providers(ort: Any) -> list[str]:
    if "CPUExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("onnxruntime CPUExecutionProvider is unavailable")
    return list(ONNX_PROVIDERS)


def _load_runtime(init: dict[str, Any]) -> _Runtime:
    model_path, vad_path, vad_enabled = _validate_init(init)
    import numpy as np
    import onnx_asr
    import onnxruntime as ort

    providers = _cpu_providers(ort)
    session_options = ort.SessionOptions()
    onnx_threads = _configure_onnx_threads(ort, session_options)
    if onnx_threads["mode"] == "per_session_fallback":
        # This session key configures per-session pools only. Python ORT does
        # not expose the equivalent global-pool spin control, so do not imply
        # the entry governs the shared-pool path by setting it there.
        session_options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    model = onnx_asr.load_model(
        MODEL,
        path=model_path,
        quantization=ONNX_QUANTIZATION,
        providers=providers,
        sess_options=session_options,
    )
    vad = None
    if vad_enabled:
        assert vad_path is not None
        vad = onnx_asr.load_vad(
            "silero",
            path=vad_path,
            providers=providers,
            sess_options=session_options,
        )
    return _Runtime(model=model, vad=vad, np=np, onnx_threads=onnx_threads)


def _load_audio(path: str, np: Any) -> Any:
    with open(path, "rb") as stream:
        if stream.read(4) == b"RIFF":
            return path
    try:
        import av
    except ImportError as error:
        raise RuntimeError("non-WAV audio needs PyAV") from error
    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=SAMPLE_RATE
    )
    chunks = []
    with av.open(path) as container:
        stream = next(item for item in container.streams if item.type == "audio")
        for frame in container.decode(stream):
            chunks.extend(item.to_ndarray() for item in resampler.resample(frame))
        chunks.extend(item.to_ndarray() for item in resampler.resample(None))
    if not chunks:
        raise RuntimeError("no audio stream decoded")
    pcm = np.concatenate([chunk.reshape(-1) for chunk in chunks])
    return pcm.astype(np.float32) / 32768.0


def _word_segments(tokens: Any, timestamps: Any) -> list[dict[str, Any]]:
    words: list[str] = []
    starts: list[float] = []
    for token, timestamp in zip(tokens, timestamps, strict=False):
        if token[:1] in ("\u2581", " ") or not words:
            words.append(token.lstrip("\u2581 "))
            starts.append(timestamp)
        else:
            words[-1] += token
    segments: list[dict[str, Any]] = []
    last = timestamps[-1] if timestamps else 0.0
    for index, (word, start) in enumerate(zip(words, starts, strict=False)):
        if not word:
            continue
        end = starts[index + 1] if index + 1 < len(starts) else last
        segments.append({"start": round(start, 2), "end": round(end, 2), "text": word})
    return segments


def _transcribe(runtime: _Runtime, path: str) -> dict[str, Any]:
    waveform = _load_audio(path, runtime.np)
    if runtime.vad is not None:
        segments = [
            {
                "start": round(result.start, 2),
                "end": round(result.end, 2),
                "text": result.text.strip(),
            }
            # onnx-asr's own CLI uses batch_size=1 here. Its default of eight
            # made the encoder request an extra ~496 MB buffer for long media,
            # needlessly consuming the sandbox's bounded memory headroom.
            for result in runtime.model.with_vad(runtime.vad, batch_size=1).recognize(
                waveform, sample_rate=SAMPLE_RATE
            )
            if result.text.strip()
        ]
        text = " ".join(segment["text"] for segment in segments)
    else:
        result = runtime.model.with_timestamps().recognize(
            waveform, sample_rate=SAMPLE_RATE
        )
        text = result.text.strip()
        segments = _word_segments(result.tokens, result.timestamps)
    return {
        "text": text,
        "segments": segments,
        # v3 recognizes its supported languages automatically but does not
        # report a detected language on this API. Do not fabricate one.
        "language": None,
    }


def _validate_request(frame: dict[str, Any]) -> tuple[str, str]:
    _require_exact_fields(
        frame,
        {"schema_version", "type", "request_id", "operation", "input"},
        frame="request",
    )
    if frame.get("schema_version") != SCHEMA_VERSION or frame.get("type") != "request":
        raise WorkerProtocolError("expected a v1 request frame")
    request_id = frame.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkerProtocolError("request id is invalid")
    if frame.get("operation") != "transcribe":
        raise WorkerProtocolError("request operation is unsupported")
    input_value = frame.get("input")
    if not isinstance(input_value, dict) or not isinstance(
        input_value.get("path"), str
    ):
        raise WorkerProtocolError("request input path is invalid")
    _require_exact_fields(input_value, {"path"}, frame="request input")
    return request_id, os.path.abspath(input_value["path"])


def framed_main() -> None:
    """Run the v1 length-prefixed, one-request-at-a-time worker protocol."""

    protocol = _protocol_stdout()
    stdin = sys.stdin.buffer
    try:
        init = _FRAME_CODEC.read_frame(stdin)
        if init is None:
            raise WorkerProtocolError("stdin closed before init")
        try:
            runtime = _load_runtime(init)
        except Exception as error:
            code = (
                "local_artifact_unavailable"
                if isinstance(error, FileNotFoundError)
                else "parakeet_model_load_failed"
            )
            _FRAME_CODEC.write_frame(
                protocol,
                {
                    **_base_frame("fatal"),
                    "error": {
                        "code": code,
                        "message": _safe_error_message(
                            error,
                            category="the pinned Parakeet model could not be loaded",
                        ),
                    },
                },
            )
            return
        _FRAME_CODEC.write_frame(
            protocol,
            {
                **_base_frame("ready"),
                "engine": ENGINE,
                "model": MODEL,
                "onnx_threads": runtime.onnx_threads,
            },
        )
        while True:
            frame = _FRAME_CODEC.read_frame(stdin)
            if frame is None:
                raise WorkerProtocolError("stdin closed without a close frame")
            if (
                frame.get("schema_version") == SCHEMA_VERSION
                and frame.get("type") == "close"
            ):
                _require_exact_fields(frame, {"schema_version", "type"}, frame="close")
                return
            # No request-id reuse guard: the trusted parent numbers requests
            # monotonically (r1, r2, ...), and tracking every seen id grew
            # without bound over a long session.
            request_id, path = _validate_request(frame)
            try:
                data = _transcribe(runtime, path)
            except Exception as error:
                response = {
                    **_base_frame("result"),
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "code": "audio_transcription_failed",
                        "message": _safe_error_message(
                            error, category="audio could not be decoded or transcribed"
                        ),
                    },
                }
            else:
                response = {
                    **_base_frame("result"),
                    "request_id": request_id,
                    "ok": True,
                    "data": data,
                }
            _FRAME_CODEC.write_frame(protocol, response)
    except Exception as error:
        try:
            _FRAME_CODEC.write_frame(
                protocol,
                {
                    **_base_frame("fatal"),
                    "error": {
                        "code": "parakeet_protocol_failed",
                        "message": _safe_error_message(
                            error, category="the Parakeet worker protocol failed"
                        ),
                    },
                },
            )
        except Exception:
            pass
    finally:
        protocol.close()
