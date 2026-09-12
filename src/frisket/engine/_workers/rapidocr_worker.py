"""Private offline RapidOCR worker.

``main`` retains the original one-shot JSON-in/result-file-out contract used by
direct recipe calls and release acceptance tests.  ``framed_main`` is the
run-scoped variant: it loads RapidOCR once, accepts one request at a time, and
writes each potentially large OCR result into its request-owned staging
directory.  The framed response is only a small acknowledgement and metrics.

This module stays standard-library-only at import time.  RapidOCR, OpenCV, and
ONNX Runtime are imported only after the sandbox owns the child process.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import sys
import time
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator

from .framing import FrameCodec

SCHEMA_VERSION = "frisket.rapidocr_worker.v1"
ENGINE = "rapidocr"

MAX_REQUEST_FRAME_BYTES = 1024 * 1024
MAX_RESPONSE_FRAME_BYTES = 64 * 1024
MAX_CONFIG_THREADS = 64
_REQUEST_ID_RE = re.compile(r"^r[1-9][0-9]{0,15}$")
_PAGE_NAME_RE = re.compile(r"^page-[0-9]{6}\.img$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")


class WorkerProtocolError(RuntimeError):
    pass


_FRAME_CODEC = FrameCodec(
    max_request_bytes=MAX_REQUEST_FRAME_BYTES,
    max_response_bytes=MAX_RESPONSE_FRAME_BYTES,
    error_type=WorkerProtocolError,
)


class InvalidLanguageError(ValueError):
    pass


def _protocol_stdout() -> BinaryIO:
    """Reserve the protocol pipe before third-party libraries can print."""

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


def _checked_threads(value: Any, *, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_CONFIG_THREADS
    ):
        raise WorkerProtocolError(f"{name} must be an integer from 1 to 64")
    return value


def _safe_version(value: Any) -> str:
    if isinstance(value, str) and _VERSION_RE.fullmatch(value):
        return value
    return "unknown"


@contextmanager
def _shared_ort_session_options(
    ort: Any,
    ort_session_class: Any,
    *,
    intra_threads: int,
    inter_threads: int,
) -> Iterator[dict[str, Any]]:
    """Make RapidOCR's three sessions share one bounded ORT pool when possible.

    RapidOCR 3.8 constructs its own ``SessionOptions`` for detection,
    classification, and recognition.  It does not expose
    ``use_per_session_threads``, so the worker temporarily wraps the pinned
    adapter's option factory while those sessions are constructed.  An
    incomplete/older API falls back to the released 1x1 per-session shape only
    before ONNX Runtime's process-global setter is attempted.  Once that setter
    is called, any setup ambiguity fails startup closed because its mutation
    cannot be rolled back safely.
    """

    configuration: dict[str, Any] = {
        "mode": "per_session_fallback",
        "intra": 1,
        "inter": 1,
        "sessions_verified": 0,
    }
    original = getattr(ort_session_class, "_init_sess_opts", None)
    set_global_pool = getattr(ort, "set_global_thread_pool_sizes", None)
    session_options_class = getattr(ort, "SessionOptions", None)
    installed_wrapper = False

    if (
        callable(original)
        and callable(set_global_pool)
        and callable(session_options_class)
    ):
        try:
            probe_options = session_options_class()
            if not hasattr(probe_options, "use_per_session_threads"):
                raise AttributeError("global session-pool selection is unavailable")
        except Exception:
            probe_options = None
        if probe_options is not None:

            def shared_options(cfg: Any) -> Any:
                options = original(cfg)
                options.use_per_session_threads = False
                if options.use_per_session_threads is not False:
                    raise RuntimeError("RapidOCR session kept a private thread pool")
                configuration["sessions_verified"] += 1
                return options

            try:
                ort_session_class._init_sess_opts = staticmethod(shared_options)
                if getattr(ort_session_class, "_init_sess_opts") is not shared_options:
                    raise RuntimeError(
                        "RapidOCR session-option wrapper was not installed"
                    )
            except Exception:
                # No process-global state has changed yet, so restoring the
                # original factory leaves the explicit 1x1 fallback safe.
                ort_session_class._init_sess_opts = staticmethod(original)
                if getattr(ort_session_class, "_init_sess_opts") is not original:
                    raise RuntimeError(
                        "RapidOCR session-option factory could not be restored"
                    )
            else:
                installed_wrapper = True
                configuration = {
                    "mode": "shared_global",
                    "intra": intra_threads,
                    "inter": inter_threads,
                    "sessions_verified": 0,
                }
                try:
                    set_global_pool(intra_threads, inter_threads)
                except Exception as error:
                    # The setter may have mutated process-global state before
                    # raising.  Never construct private pools in that unknown
                    # state or report a clean fallback.
                    ort_session_class._init_sess_opts = staticmethod(original)
                    installed_wrapper = False
                    raise RuntimeError(
                        "ONNX Runtime global thread-pool setup failed"
                    ) from error
    try:
        yield configuration
    finally:
        if installed_wrapper:
            ort_session_class._init_sess_opts = staticmethod(original)


def _configure_opencv(cv2: Any, requested_threads: int) -> dict[str, Any]:
    cv2.setNumThreads(requested_threads)
    actual_threads = int(cv2.getNumThreads())
    if actual_threads < 1 or actual_threads > MAX_CONFIG_THREADS:
        raise RuntimeError("OpenCV did not accept its bounded thread count")
    ocl = getattr(cv2, "ocl", None)
    if ocl is None or not callable(getattr(ocl, "setUseOpenCL", None)):
        raise RuntimeError("OpenCV OpenCL control is unavailable")
    ocl.setUseOpenCL(False)
    use_opencl = getattr(ocl, "useOpenCL", None)
    if callable(use_opencl) and bool(use_opencl()):
        raise RuntimeError("OpenCV OpenCL could not be disabled")
    # Apple's GCD backend documents ``setNumThreads`` as a compatibility no-op
    # and reports its fixed backend width.  Keep requested and observed values
    # distinct so Linux can prove the exact bound without making macOS claim a
    # control its backend does not offer.
    return {
        "requested_threads": requested_threads,
        "observed_threads": actual_threads,
        "opencl": False,
    }


def _language_param(language: str | None) -> Any | None:
    if language is None:
        return None
    from rapidocr.utils.typings import LangRec

    try:
        return LangRec(language)
    except ValueError as error:
        valid = ", ".join(sorted(item.value for item in LangRec))
        raise InvalidLanguageError(
            f"unknown ocr language {language!r} for rapidocr (one of: {valid})"
        ) from error


def _load_runtime(
    *,
    language: str | None,
    model_root_dir: str | None,
    onnx_intra_threads: int,
    onnx_inter_threads: int,
    opencv_threads: int,
) -> tuple[Any, dict[str, Any]]:
    logging.disable(logging.WARNING)
    os.environ["OPENCV_FOR_THREADS_NUM"] = str(opencv_threads)
    import cv2
    import onnxruntime as ort
    from rapidocr import RapidOCR
    from rapidocr.inference_engine.onnxruntime import OrtInferSession

    opencv = _configure_opencv(cv2, opencv_threads)
    params: dict[str, Any] = {
        # These are the fail-safe values if global-pool selection is not
        # available.  In shared mode the wrapped SessionOptions explicitly
        # opt out of these per-session pools.
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    }
    if model_root_dir is not None:
        params["Global.model_root_dir"] = model_root_dir
    language_param = _language_param(language)
    if language_param is not None:
        params["Rec.lang_type"] = language_param

    with _shared_ort_session_options(
        ort,
        OrtInferSession,
        intra_threads=onnx_intra_threads,
        inter_threads=onnx_inter_threads,
    ) as onnx_threads:
        model_started = time.perf_counter()
        engine = RapidOCR(params=params)
        model_init_ms = round(max(0.0, time.perf_counter() - model_started) * 1000, 3)
        if (
            onnx_threads["mode"] == "shared_global"
            and onnx_threads["sessions_verified"] != 3
        ):
            raise RuntimeError(
                "RapidOCR did not create exactly three verified shared-pool sessions"
            )

    runtime = {
        "rapidocr_version": _safe_version(metadata.version("rapidocr")),
        "onnxruntime_version": _safe_version(getattr(ort, "__version__", None)),
        "onnx_threads": onnx_threads,
        "opencv_threads": opencv["requested_threads"],
        "opencv_threads_observed": opencv["observed_threads"],
        "opencv_opencl": opencv["opencl"],
        "model_init_ms": model_init_ms,
    }
    return engine, runtime


def _ocr_pages(
    engine: Any, paths: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from PIL import Image

    pages: list[dict[str, Any]] = []
    native_seconds = [0.0, 0.0, 0.0]
    recognized_blocks = 0
    input_pixels = 0
    max_page_pixels = 0
    max_page_width = 0
    max_page_height = 0
    for path in paths:
        width = height = 0
        try:
            with Image.open(path) as source_image:
                width, height = source_image.size
        except Exception:
            # Header telemetry must not redefine OCR's row-level error
            # semantics.  RapidOCR remains the authority on readability.
            width = height = 0
        result = engine(path)
        txts = list(getattr(result, "txts", None) or [])
        boxes = getattr(result, "boxes", None)
        scores = list(getattr(result, "scores", None) or [])
        recognized_blocks += len(txts)
        if width <= 0 or height <= 0:
            image = getattr(result, "img", None)
            shape = getattr(image, "shape", None)
            if isinstance(shape, tuple) and len(shape) >= 2:
                try:
                    height = int(shape[0])
                    width = int(shape[1])
                except (TypeError, ValueError):
                    height = width = 0
        if height > 0 and width > 0:
            pixels = height * width
            input_pixels += pixels
            max_page_pixels = max(max_page_pixels, pixels)
            max_page_width = max(max_page_width, width)
            max_page_height = max(max_page_height, height)
        elapse_list = getattr(result, "elapse_list", None)
        if isinstance(elapse_list, (list, tuple)):
            for index, value in enumerate(elapse_list[:3]):
                if (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(float(value))
                    and float(value) >= 0
                ):
                    native_seconds[index] += float(value)
        blocks: list[dict[str, Any]] = []
        for index, text in enumerate(txts):
            block: dict[str, Any] = {"text": text}
            if boxes is not None and index < len(boxes):
                block["bbox"] = [[int(x), int(y)] for x, y in boxes[index]]
            if index < len(scores):
                block["score"] = round(float(scores[index]), 4)
            blocks.append(block)
        pages.append(
            {
                "text": "\n".join(text.strip() for text in txts).strip(),
                "blocks": blocks,
            }
        )
    return pages, {
        "det_ms": round(native_seconds[0] * 1000, 3),
        "cls_ms": round(native_seconds[1] * 1000, 3),
        "rec_ms": round(native_seconds[2] * 1000, 3),
        "recognized_blocks": recognized_blocks,
        "input_pixels": input_pixels,
        "max_page_pixels": max_page_pixels,
        "max_page_width": max_page_width,
        "max_page_height": max_page_height,
    }


def _process_metrics() -> dict[str, int | None]:
    metrics: dict[str, int | None] = {
        "vm_hwm_bytes": None,
        "vm_peak_bytes": None,
        "threads": None,
    }
    if not sys.platform.startswith("linux"):
        return metrics
    try:
        status_text = Path("/proc/self/status").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return metrics
    for line in status_text.splitlines():
        key, separator, raw_value = line.partition(":")
        if not separator:
            continue
        parts = raw_value.split()
        try:
            if key == "VmHWM" and len(parts) == 2 and parts[1] == "kB":
                metrics["vm_hwm_bytes"] = int(parts[0]) * 1024
            elif key == "VmPeak" and len(parts) == 2 and parts[1] == "kB":
                metrics["vm_peak_bytes"] = int(parts[0]) * 1024
            elif key == "Threads" and len(parts) == 1:
                metrics["threads"] = int(parts[0])
        except ValueError:
            continue
    return metrics


def _validate_init(frame: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(frame, {"schema_version", "type", "config"}, frame="init")
    if frame.get("schema_version") != SCHEMA_VERSION or frame.get("type") != "init":
        raise WorkerProtocolError("first frame must be a RapidOCR init frame")
    config = frame.get("config")
    if not isinstance(config, dict):
        raise WorkerProtocolError("init config must be an object")
    _require_exact_fields(
        config,
        {
            "language",
            "model_root_dir",
            "onnx_intra_threads",
            "onnx_inter_threads",
            "opencv_threads",
        },
        frame="init config",
    )
    language = config.get("language")
    if language is not None and (
        not isinstance(language, str) or not 1 <= len(language) <= 64
    ):
        raise WorkerProtocolError("RapidOCR language is invalid")
    model_root_dir = config.get("model_root_dir")
    if model_root_dir is not None:
        if not isinstance(model_root_dir, str) or not os.path.isabs(model_root_dir):
            raise WorkerProtocolError("RapidOCR model root is invalid")
        model_root = Path(model_root_dir)
        if not model_root.is_dir():
            raise FileNotFoundError("RapidOCR model root is unavailable")
        model_root_dir = str(model_root.resolve())
    return {
        "language": language,
        "model_root_dir": model_root_dir,
        "onnx_intra_threads": _checked_threads(
            config.get("onnx_intra_threads"), name="ONNX intra-op threads"
        ),
        "onnx_inter_threads": _checked_threads(
            config.get("onnx_inter_threads"), name="ONNX inter-op threads"
        ),
        "opencv_threads": _checked_threads(
            config.get("opencv_threads"), name="OpenCV threads"
        ),
    }


def _request_relative_path(
    staging_root: Path,
    request_id: str,
    value: Any,
    *,
    output: bool,
) -> Path:
    if not isinstance(value, str) or len(value) > 256:
        raise WorkerProtocolError("request staging path is invalid")
    relative = PurePosixPath(value)
    expected_name = "result.json" if output else None
    if (
        relative.is_absolute()
        or len(relative.parts) != 3
        or relative.parts[:2] != ("requests", request_id)
        or (output and relative.parts[2] != expected_name)
        or (not output and not _PAGE_NAME_RE.fullmatch(relative.parts[2]))
    ):
        raise WorkerProtocolError("request staging path is outside its grant")
    candidate = staging_root.joinpath(*relative.parts)
    parent = candidate.parent.resolve(strict=True)
    try:
        parent.relative_to(staging_root)
    except ValueError as error:
        raise WorkerProtocolError("request staging path escaped its root") from error
    return candidate


def _input_file(staging_root: Path, request_id: str, value: Any) -> Path:
    path = _request_relative_path(staging_root, request_id, value, output=False)
    try:
        metadata_result = path.lstat()
    except OSError as error:
        raise WorkerProtocolError("request input is unavailable") from error
    if stat.S_ISLNK(metadata_result.st_mode) or not stat.S_ISREG(
        metadata_result.st_mode
    ):
        raise WorkerProtocolError("request input is not a regular staged file")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(staging_root)
    except ValueError as error:
        raise WorkerProtocolError("request input escaped its staging root") from error
    return resolved


def _validate_request(
    frame: dict[str, Any], staging_root: Path
) -> tuple[str, list[str], Path]:
    _require_exact_fields(
        frame,
        {"schema_version", "type", "request_id", "operation", "input", "output"},
        frame="request",
    )
    if frame.get("schema_version") != SCHEMA_VERSION or frame.get("type") != "request":
        raise WorkerProtocolError("expected a RapidOCR request frame")
    request_id = frame.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise WorkerProtocolError("request id is invalid")
    if frame.get("operation") != "ocr":
        raise WorkerProtocolError("request operation is unsupported")
    input_value = frame.get("input")
    output_value = frame.get("output")
    if not isinstance(input_value, dict) or not isinstance(output_value, dict):
        raise WorkerProtocolError("request input/output must be objects")
    _require_exact_fields(input_value, {"paths"}, frame="request input")
    _require_exact_fields(output_value, {"path"}, frame="request output")
    path_values = input_value.get("paths")
    if not isinstance(path_values, list) or not path_values:
        raise WorkerProtocolError("request paths must be a non-empty array")
    paths = [str(_input_file(staging_root, request_id, value)) for value in path_values]
    output_path = _request_relative_path(
        staging_root, request_id, output_value.get("path"), output=True
    )
    if output_path.exists() or output_path.is_symlink():
        raise WorkerProtocolError("request output already exists")
    return request_id, paths, output_path


def _write_result_file(path: Path, pages: list[dict[str, Any]]) -> None:
    # Exclusive creation ensures a stale/symlinked path is never followed.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(
            {"pages": pages},
            stream,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )


def framed_main() -> None:
    """Run the bounded one-request-at-a-time persistent worker protocol."""

    protocol = _protocol_stdout()
    stdin = sys.stdin.buffer
    staging_root = Path.cwd().resolve()
    try:
        init = _FRAME_CODEC.read_frame(stdin)
        if init is None:
            raise WorkerProtocolError("stdin closed before init")
        try:
            config = _validate_init(init)
            engine, runtime = _load_runtime(**config)
        except Exception as error:
            if isinstance(error, InvalidLanguageError):
                code = "invalid_language"
                message = str(error)[:500]
            elif isinstance(error, FileNotFoundError):
                code = "rapidocr_model_unavailable"
                message = "the configured RapidOCR model files are unavailable"
            else:
                code = "rapidocr_model_load_failed"
                message = "the RapidOCR model could not be loaded"
            _FRAME_CODEC.write_frame(
                protocol,
                {
                    **_base_frame("fatal"),
                    "error": {"code": code, "message": message},
                },
            )
            return
        _FRAME_CODEC.write_frame(
            protocol,
            {
                **_base_frame("ready"),
                "engine": ENGINE,
                "runtime": runtime,
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
            request_id, paths, output_path = _validate_request(frame, staging_root)
            started = time.perf_counter()
            try:
                pages, native_metrics = _ocr_pages(engine, paths)
                _write_result_file(output_path, pages)
            except Exception:
                response = {
                    **_base_frame("result"),
                    "request_id": request_id,
                    "ok": False,
                    "error": {
                        "code": "image_ocr_failed",
                        "message": "images could not be read or recognized",
                    },
                }
            else:
                response = {
                    **_base_frame("result"),
                    "request_id": request_id,
                    "ok": True,
                    "metrics": {
                        "duration_ms": round(
                            max(0.0, time.perf_counter() - started) * 1000, 3
                        ),
                        "page_count": len(paths),
                        **native_metrics,
                        **_process_metrics(),
                    },
                }
            _FRAME_CODEC.write_frame(protocol, response)
    except Exception:
        try:
            _FRAME_CODEC.write_frame(
                protocol,
                {
                    **_base_frame("fatal"),
                    "error": {
                        "code": "rapidocr_protocol_failed",
                        "message": "the RapidOCR worker protocol failed",
                    },
                },
            )
        except Exception:
            pass
    finally:
        protocol.close()


def main() -> None:
    """Retained one-shot worker contract used outside an execution scope."""

    payload = json.load(sys.stdin)
    logging.disable(logging.WARNING)
    try:
        engine, _runtime = _load_runtime(
            language=payload.get("language"),
            model_root_dir=payload.get("model_root_dir"),
            onnx_intra_threads=1,
            onnx_inter_threads=1,
            opencv_threads=1,
        )
    except ImportError:
        _fail(
            payload, "rapidocr not installed in this environment (pip install rapidocr)"
        )
        return
    except InvalidLanguageError as error:
        _fail(payload, str(error))
        return
    pages, _metrics = _ocr_pages(engine, list(payload["paths"]))
    _write(payload, {"pages": pages})


def _fail(payload: dict[str, Any], message: str) -> None:
    _write(payload, {"error": message})


def _write(payload: dict[str, Any], value: dict[str, Any]) -> None:
    with open(payload["out"], "w") as stream:
        json.dump(value, stream)


if __name__ == "__main__":
    main()
