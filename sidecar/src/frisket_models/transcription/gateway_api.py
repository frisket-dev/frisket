"""Authenticated multipart route for the transcription gateway."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile

from .contract import (
    CONTRACT_VERSION,
    GatewayTranscriptionResponse,
    TranscribeOptions,
    TranscribeResult,
    TranscriptionErrorEnvelope,
    TranscriptionError,
)
from .gateway import WorkerGatewayError

DEFAULT_TRANSCRIPTION_MAX_UPLOAD_BYTES = 250 * 1024 * 1024
DEFAULT_TRANSCRIPTION_MAX_FILES = 16
_COPY_CHUNK_BYTES = 1024 * 1024
_FORM_FIELDS = frozenset({"contract_version", "engine", "options", "files"})


class GatewayAdmission(Protocol):
    """Import-independent sidecar admission interface."""

    def acquire(self) -> bool: ...

    def release(self) -> None: ...


class TranscriptionGateway(Protocol):
    """The route's local-or-remote transcription dispatch boundary."""

    async def transcribe(
        self,
        *,
        engine: str,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> TranscribeResult: ...

    async def public_capabilities(self) -> list[dict[str, Any]]: ...


@dataclass
class _RouteFailure(Exception):
    status_code: int
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _UploadBudget:
    max_bytes: int
    written: int = 0

    def consume(self, size: int) -> None:
        self.written += size
        if self.written > self.max_bytes:
            raise _RouteFailure(
                413,
                "input_too_large",
                "audio uploads exceed the gateway request limit",
                details={"max_request_upload_bytes": self.max_bytes},
            )


def _error_response(
    failure: _RouteFailure | WorkerGatewayError,
) -> JSONResponse:
    if isinstance(failure, WorkerGatewayError):
        envelope = failure.envelope
    else:
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
        content=envelope.model_dump(mode="json"),
        headers=failure.headers,
    )


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
        if not isinstance(decoded, dict):
            raise ValueError("options must be a JSON object")
        return TranscribeOptions.model_validate(decoded, strict=True)
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise _RouteFailure(
            400,
            "invalid_options",
            "options do not conform to the transcription v1 contract",
            details={"reason": str(exc)},
        ) from None


async def _parse_form(
    request: Request,
) -> tuple[str, str, TranscribeOptions, list[UploadFile], FormData]:
    if (
        not request.headers.get("content-type", "")
        .lower()
        .startswith("multipart/form-data")
    ):
        raise _RouteFailure(
            400, "invalid_request", "request must use multipart/form-data"
        )
    form: FormData | None = None
    try:
        form = await request.form(
            max_files=DEFAULT_TRANSCRIPTION_MAX_FILES,
            max_fields=16,
        )
    except Exception as exc:
        raise _RouteFailure(
            400,
            "invalid_request",
            "malformed multipart request",
            details={"reason": str(exc)},
        ) from None

    try:
        values: dict[str, list[Any]] = {}
        for name, value in form.multi_items():
            if name not in _FORM_FIELDS:
                raise _RouteFailure(
                    400,
                    "unknown_field",
                    f"unknown multipart field '{name}'",
                    details={"field": name},
                )
            values.setdefault(name, []).append(value)

        for name in ("contract_version", "engine", "options"):
            if len(values.get(name, [])) != 1:
                raise _RouteFailure(
                    400,
                    "invalid_request",
                    f"multipart field '{name}' must appear exactly once",
                    details={"field": name},
                )
        uploads = values.get("files", [])
        if not uploads or not all(isinstance(item, UploadFile) for item in uploads):
            raise _RouteFailure(
                400,
                "invalid_request",
                "multipart field 'files' must contain at least one file",
                details={"field": "files"},
            )

        contract_version, engine, raw_options = (
            values["contract_version"][0],
            values["engine"][0],
            values["options"][0],
        )
        if not all(
            isinstance(item, str) for item in (contract_version, engine, raw_options)
        ):
            raise _RouteFailure(
                400,
                "invalid_request",
                "contract_version, engine, and options must be text fields",
            )
        return (
            contract_version,
            engine,
            _decode_options(raw_options),
            uploads,
            form,
        )
    except BaseException:
        await form.close()
        raise


@contextlib.asynccontextmanager
async def _spooled_upload(
    upload: UploadFile,
    *,
    max_upload_bytes: int,
    spool_dir: Path | None,
    request_budget: _UploadBudget,
):
    suffix = Path(upload.filename or "").suffix
    if (
        not suffix
        or len(suffix) > 12
        or not suffix[1:].replace("-", "").replace("_", "").isalnum()
    ):
        suffix = ".audio"
    fd, raw_path = await run_in_threadpool(
        tempfile.mkstemp,
        prefix="frisket-transcription-gateway-",
        suffix=suffix,
        dir=str(spool_dir) if spool_dir is not None else None,
    )
    path = Path(raw_path)
    output = os.fdopen(fd, "wb")
    written = 0
    try:
        await upload.seek(0)
        while True:
            chunk = await upload.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            request_budget.consume(len(chunk))
            if written > max_upload_bytes:
                raise _RouteFailure(
                    413,
                    "input_too_large",
                    "audio upload exceeds the gateway input limit",
                    details={"max_upload_bytes": max_upload_bytes},
                )
            await run_in_threadpool(output.write, chunk)
        await run_in_threadpool(output.flush)
        output.close()
        yield path
    finally:
        if not output.closed:
            output.close()
        path.unlink(missing_ok=True)


def mount_gateway_transcribe(
    router: APIRouter,
    *,
    gateway: TranscriptionGateway,
    admission: GatewayAdmission,
    max_upload_bytes: int,
    spool_dir: Path | None,
    retry_after_seconds: str,
) -> None:
    """Mount the authenticated gateway route."""

    @router.post("/v1/transcribe")
    async def transcribe_v1(request: Request) -> JSONResponse:
        form: FormData | None = None
        acquired = False
        try:
            if not admission.acquire():
                raise _RouteFailure(
                    429,
                    "at_capacity",
                    "sidecar gateway is at capacity; retry shortly",
                    retryable=True,
                    headers={"Retry-After": retry_after_seconds},
                )
            acquired = True
            contract_version, engine, options, uploads, form = await _parse_form(
                request
            )
            if contract_version != CONTRACT_VERSION:
                raise _RouteFailure(
                    400,
                    "unsupported_contract_version",
                    f"unsupported transcription contract version '{contract_version}'",
                    details={"supported": CONTRACT_VERSION},
                )

            async with AsyncExitStack() as stack:
                request_budget = _UploadBudget(max_bytes=max_upload_bytes)
                paths = [
                    await stack.enter_async_context(
                        _spooled_upload(
                            upload,
                            max_upload_bytes=max_upload_bytes,
                            spool_dir=spool_dir,
                            request_budget=request_budget,
                        )
                    )
                    for upload in uploads
                ]
                results = [
                    await gateway.transcribe(
                        engine=engine,
                        audio_path=path,
                        options=options,
                    )
                    for path in paths
                ]
            envelope = GatewayTranscriptionResponse(
                contract_version=CONTRACT_VERSION,
                results=results,
            )
            return JSONResponse(content=envelope.model_dump(mode="json"))
        except (_RouteFailure, WorkerGatewayError) as failure:
            return _error_response(failure)
        except Exception:
            return _error_response(
                _RouteFailure(
                    500,
                    "gateway_failure",
                    "transcription gateway failed",
                )
            )
        finally:
            if acquired:
                admission.release()
            if form is not None:
                await form.close()
