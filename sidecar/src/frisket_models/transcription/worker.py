"""Reusable isolated-worker HTTP wrapper.

Owns strict multipart validation, non-queueing admission, bounded spooling,
lazy adapter construction, and cleanup. Engine packages supply only an
``AdapterRegistration``.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile

from .contract import (
    CONTRACT_VERSION,
    AdapterRegistration,
    EngineProbe,
    TranscribeOptions,
    TranscribeResult,
    TranscriptionErrorEnvelope,
    TranscriptionError,
    TranscriptionInputError,
    WorkerCapabilities,
    WorkerTranscriptionResponse,
)

DEFAULT_MAX_UPLOAD_BYTES = 250 * 1024 * 1024
DEFAULT_RETRY_AFTER_SECONDS = "2"
_COPY_CHUNK_BYTES = 1024 * 1024
_MULTIPART_FIELD_ORDER = ("contract_version", "engine", "options", "file")
_MULTIPART_FIELDS = frozenset(_MULTIPART_FIELD_ORDER)


@dataclass
class _WorkerFailure(Exception):
    status_code: int
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class _Admission:
    """Non-queueing concurrency gate; the gateway owns retries."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._in_flight = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            if self._in_flight >= self.limit:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._in_flight <= 0:  # Internal invariant, not client input.
                raise RuntimeError("worker admission released without a slot")
            self._in_flight -= 1

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight


class _AdapterState:
    """Lazy adapter with a sticky construction failure."""

    def __init__(self, registration: AdapterRegistration) -> None:
        self._registration = registration
        self._adapter: Any | None = None
        self._load_error: str | None = None
        self._lock = threading.Lock()

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def load(self) -> Any:
        adapter, _loaded_now = self.load_with_status()
        return adapter

    def load_with_status(self) -> tuple[Any, bool]:
        if self._adapter is not None:
            return self._adapter, False
        if self._load_error is not None:
            raise RuntimeError(self._load_error)

        with self._lock:
            if self._adapter is not None:
                return self._adapter, False
            if self._load_error is None:
                try:
                    adapter = self._registration.factory()
                    if not callable(getattr(adapter, "transcribe", None)):
                        raise TypeError(
                            "adapter factory must return an object with "
                            "transcribe(path, options)"
                        )
                    self._adapter = adapter
                except Exception as exc:
                    self._load_error = f"{type(exc).__name__}: {exc}"
            if self._load_error is not None:
                raise RuntimeError(self._load_error)

        return self._adapter, True


def _model_json(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _error_response(failure: _WorkerFailure) -> JSONResponse:
    envelope = TranscriptionErrorEnvelope(
        contract_version=CONTRACT_VERSION,
        error=TranscriptionError(
            code=failure.code,
            message=failure.message,
            retryable=failure.retryable,
            details=failure.details,
        ),
    )
    return JSONResponse(
        status_code=failure.status_code,
        content=_model_json(envelope),
        headers=failure.headers,
    )


def _authorization_failure(
    request: Request,
    token: str | None,
) -> _WorkerFailure | None:
    if token is None:
        return None
    header = request.headers.get("Authorization")
    if not header:
        return _WorkerFailure(
            401,
            "missing_bearer_token",
            "missing bearer token",
        )
    if header != f"Bearer {token}":
        return _WorkerFailure(
            403,
            "invalid_bearer_token",
            "invalid bearer token",
        )
    return None


def _decode_options(raw: str) -> TranscribeOptions:
    def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key: {key}")
            value[key] = item
        return value

    def _non_finite(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_object,
            parse_constant=_non_finite,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _WorkerFailure(
            400,
            "invalid_options",
            "options must be a valid JSON object",
            details={"reason": str(exc)},
        ) from None
    if not isinstance(decoded, dict):
        raise _WorkerFailure(
            400,
            "invalid_options",
            "options must be a JSON object",
        )
    try:
        return TranscribeOptions.model_validate(decoded, strict=True)
    except ValidationError as exc:
        raise _WorkerFailure(
            400,
            "invalid_options",
            "options do not conform to the transcription v1 contract",
            details={"reason": str(exc)},
        ) from None


async def _parse_multipart(
    request: Request,
) -> tuple[str, str, TranscribeOptions, UploadFile, FormData]:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise _WorkerFailure(
            400,
            "invalid_request",
            "request must use multipart/form-data",
        )
    form: FormData | None = None
    try:
        form = await request.form(max_files=1, max_fields=8)
    except Exception as exc:
        raise _WorkerFailure(
            400,
            "invalid_request",
            "malformed multipart request",
            details={"reason": str(exc)},
        ) from None

    try:
        values: dict[str, list[Any]] = {}
        for name, value in form.multi_items():
            if name not in _MULTIPART_FIELDS:
                raise _WorkerFailure(
                    400,
                    "unknown_field",
                    f"unknown multipart field '{name}'",
                    details={"field": name},
                )
            values.setdefault(name, []).append(value)

        for name in _MULTIPART_FIELD_ORDER:
            count = len(values.get(name, []))
            if count != 1:
                raise _WorkerFailure(
                    400,
                    "invalid_request",
                    f"multipart field '{name}' must appear exactly once",
                    details={"field": name, "count": count},
                )

        contract_version = values["contract_version"][0]
        engine = values["engine"][0]
        raw_options = values["options"][0]
        upload = values["file"][0]
        if not all(
            isinstance(value, str) for value in (contract_version, engine, raw_options)
        ):
            raise _WorkerFailure(
                400,
                "invalid_request",
                "contract_version, engine, and options must be text fields",
            )
        if not isinstance(upload, UploadFile):
            raise _WorkerFailure(
                400,
                "invalid_request",
                "file must be one multipart file",
            )
        return (
            contract_version,
            engine,
            _decode_options(raw_options),
            upload,
            form,
        )
    except BaseException:
        await form.close()
        raise


def _safe_upload_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if (
        not suffix
        or len(suffix) > 12
        or not suffix[1:].replace("-", "").replace("_", "").isalnum()
    ):
        return ".audio"
    return suffix


async def _spool_upload(
    upload: UploadFile,
    *,
    max_upload_bytes: int,
    spool_dir: Path | None,
) -> Path:
    directory = str(spool_dir) if spool_dir is not None else None
    fd, raw_path = await run_in_threadpool(
        tempfile.mkstemp,
        prefix="frisket-transcription-",
        suffix=_safe_upload_suffix(getattr(upload, "filename", None)),
        dir=directory,
    )
    path = Path(raw_path)
    output = os.fdopen(fd, "wb")
    written = 0
    try:
        while True:
            chunk = await upload.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > max_upload_bytes:
                raise _WorkerFailure(
                    413,
                    "input_too_large",
                    "audio upload exceeds the worker input limit",
                    details={"max_upload_bytes": max_upload_bytes},
                )
            await run_in_threadpool(output.write, chunk)
        await run_in_threadpool(output.flush)
        output.close()
        return path
    except BaseException:
        try:
            output.close()
        finally:
            path.unlink(missing_ok=True)
        raise


def _runtime_probe(
    registration: AdapterRegistration,
    state: _AdapterState,
    *,
    runtime_image_id: str | None,
) -> EngineProbe:
    try:
        declared = registration.probe()
        if not isinstance(declared, EngineProbe):
            declared = EngineProbe.model_validate(declared, strict=True)
    except Exception as exc:
        declared = EngineProbe(
            available=False,
            loaded=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    load_error = state.load_error
    if load_error is not None:
        return EngineProbe(available=False, loaded=False, error=load_error)
    if runtime_image_id is None:
        return EngineProbe(
            available=False,
            loaded=declared.loaded,
            error="worker runtime image id is not configured",
        )
    return EngineProbe(
        available=declared.available,
        # A constructed client does not prove the native server loaded weights.
        loaded=declared.loaded,
        error=declared.error,
    )


def create_worker_app(
    registration: AdapterRegistration,
    token: str | None = None,
    concurrency: int = 1,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    spool_dir: str | Path | None = None,
    runtime_image_id: str | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> FastAPI:
    """Build a worker; liveness stays open and reveals no capabilities."""

    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if max_upload_bytes < 1:
        raise ValueError("max_upload_bytes must be at least 1")

    resolved_spool_dir: Path | None = None
    if spool_dir is not None:
        resolved_spool_dir = Path(spool_dir).resolve()
        if not resolved_spool_dir.is_dir():
            raise ValueError("spool_dir must be an existing directory")

    descriptor = registration.descriptor
    resolved_runtime_image_id = runtime_image_id
    if resolved_runtime_image_id is None:
        resolved_runtime_image_id = os.environ.get(
            "FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID"
        )
    if resolved_runtime_image_id is None:
        resolved_runtime_image_id = descriptor.runtime_image_id
    if resolved_runtime_image_id is not None:
        descriptor = type(descriptor).model_validate(
            {
                **descriptor.model_dump(mode="python"),
                "runtime_image_id": resolved_runtime_image_id,
            },
            strict=True,
        )

    state = _AdapterState(registration)
    admission = _Admission(concurrency)
    app = FastAPI(title=f"frisket-transcription-worker-{descriptor.engine}")
    app.state.registration = registration
    app.state.adapter_state = state
    app.state.admission = admission

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/capabilities")
    async def capabilities(request: Request) -> JSONResponse:
        if failure := _authorization_failure(request, token):
            return _error_response(failure)
        probe = await run_in_threadpool(
            _runtime_probe,
            registration,
            state,
            runtime_image_id=descriptor.runtime_image_id,
        )
        response = WorkerCapabilities(
            contract_version=CONTRACT_VERSION,
            descriptor=descriptor,
            probe=probe,
        )
        return JSONResponse(content=_model_json(response))

    @app.post("/v1/transcribe")
    async def transcribe(request: Request) -> JSONResponse:
        if failure := _authorization_failure(request, token):
            return _error_response(failure)

        path: Path | None = None
        form: FormData | None = None
        acquired = False
        try:
            if not admission.acquire():
                raise _WorkerFailure(
                    429,
                    "at_capacity",
                    "worker is at capacity; retry shortly",
                    retryable=True,
                    headers={"Retry-After": DEFAULT_RETRY_AFTER_SECONDS},
                )
            acquired = True
            contract_version, engine, options, upload, form = await _parse_multipart(
                request
            )
            if contract_version != CONTRACT_VERSION:
                raise _WorkerFailure(
                    400,
                    "unsupported_contract_version",
                    f"unsupported transcription contract version '{contract_version}'",
                    details={"supported": CONTRACT_VERSION},
                )
            if engine != descriptor.engine:
                raise _WorkerFailure(
                    400,
                    "unsupported_engine",
                    f"worker serves engine '{descriptor.engine}', not '{engine}'",
                    details={"engine": engine},
                )
            try:
                accepted_options = descriptor.validate_options(options)
            except (TypeError, ValueError) as exc:
                raise _WorkerFailure(
                    400,
                    "unsupported_option",
                    f"engine '{engine}' does not support the requested options",
                    details={"reason": str(exc)},
                ) from None

            probe = await run_in_threadpool(
                _runtime_probe,
                registration,
                state,
                runtime_image_id=descriptor.runtime_image_id,
            )
            if not probe.available:
                raise _WorkerFailure(
                    503,
                    "engine_unavailable",
                    f"engine '{engine}' is unavailable",
                    retryable=True,
                    details={"reason": probe.error} if probe.error else {},
                )

            path = await _spool_upload(
                upload,
                max_upload_bytes=max_upload_bytes,
                spool_dir=resolved_spool_dir,
            )
            adapter_load_started = clock()
            try:
                adapter, loaded_now = await run_in_threadpool(state.load_with_status)
            except RuntimeError as exc:
                raise _WorkerFailure(
                    503,
                    "engine_unavailable",
                    f"engine '{engine}' failed to load",
                    retryable=True,
                    details={"reason": str(exc)},
                ) from None
            adapter_load_elapsed = clock() - adapter_load_started
            adapter_load_seconds = adapter_load_elapsed if loaded_now else 0.0

            inference_started = clock()
            try:
                raw_result = await run_in_threadpool(adapter.transcribe, path, options)
            except TranscriptionInputError as exc:
                raise _WorkerFailure(
                    exc.status_code,
                    exc.code,
                    exc.message,
                    details=exc.details,
                ) from None
            except (TimeoutError, httpx.TimeoutException) as exc:
                raise _WorkerFailure(
                    504,
                    "inference_timeout",
                    f"engine '{engine}' inference timed out",
                    retryable=True,
                    details={"reason": f"{type(exc).__name__}: {exc}"},
                ) from None
            except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                # Native-server disconnects are retryable availability failures.
                # Accepted risk: a persistent malformed response may also surface
                # as RemoteProtocolError and be retried indefinitely.
                raise _WorkerFailure(
                    503,
                    "engine_unavailable",
                    f"engine '{engine}' became unavailable during inference",
                    retryable=True,
                    details={"reason": f"{type(exc).__name__}: {exc}"},
                ) from None
            except Exception as exc:
                raise _WorkerFailure(
                    500,
                    "inference_failed",
                    f"engine '{engine}' inference failed",
                    details={"reason": f"{type(exc).__name__}: {exc}"},
                ) from None
            inference_seconds = clock() - inference_started

            try:
                result = TranscribeResult.model_validate(raw_result, strict=True)
            except ValidationError as exc:
                raise _WorkerFailure(
                    500,
                    "invalid_adapter_result",
                    f"engine '{engine}' returned an invalid result",
                    details={"reason": str(exc)},
                ) from None
            wrapper_timings = {
                "worker.adapter_load_seconds": (
                    adapter_load_seconds
                    if math.isfinite(adapter_load_seconds) and adapter_load_seconds >= 0
                    else 0.0
                ),
                "worker.inference_seconds": (
                    inference_seconds
                    if math.isfinite(inference_seconds) and inference_seconds >= 0
                    else 0.0
                ),
            }
            result = result.model_copy(
                update={"timings": {**result.timings, **wrapper_timings}}
            )
            if result.engine != engine:
                raise _WorkerFailure(
                    500,
                    "invalid_adapter_result",
                    f"engine '{engine}' returned a result for '{result.engine}'",
                )
            if (
                result.model_ids != descriptor.model_ids
                or result.revision != descriptor.revision
            ):
                raise _WorkerFailure(
                    500,
                    "invalid_adapter_result",
                    f"engine '{engine}' returned unregistered model provenance",
                    details={
                        "expected_model_ids": descriptor.model_ids,
                        "actual_model_ids": result.model_ids,
                        "expected_revision": descriptor.revision,
                        "actual_revision": result.revision,
                    },
                )
            try:
                descriptor.validate_result(result, options)
            except ValueError as exc:
                raise _WorkerFailure(
                    500,
                    "invalid_adapter_result",
                    f"engine '{engine}' returned a semantically invalid result",
                    details={"reason": str(exc)},
                ) from None

            canonical_accepted = {
                option.value if hasattr(option, "value") else str(option): value
                for option, value in accepted_options.items()
            }
            if result.accepted_options != accepted_options:
                raise _WorkerFailure(
                    500,
                    "invalid_adapter_result",
                    f"engine '{engine}' did not account for every accepted option",
                    details={
                        "requested": canonical_accepted,
                        "accepted": result.accepted_options,
                    },
                )

            response = WorkerTranscriptionResponse(
                contract_version=CONTRACT_VERSION,
                result=result,
            )
            return JSONResponse(content=_model_json(response))
        except _WorkerFailure as failure:
            return _error_response(failure)
        except Exception as exc:
            return _error_response(
                _WorkerFailure(
                    500,
                    "worker_failure",
                    "transcription worker failed",
                    details={"reason": f"{type(exc).__name__}: {exc}"},
                )
            )
        finally:
            # Avoid a cancellation checkpoint before deleting the spool file.
            try:
                if path is not None:
                    path.unlink(missing_ok=True)
            finally:
                if acquired:
                    admission.release()
            if form is not None:
                await form.close()

    return app
