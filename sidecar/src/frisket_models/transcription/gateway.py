"""Strict client and static registry for isolated transcription workers."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from frisket_models.transcription.contract import (
    CONTRACT_VERSION,
    TRANSCRIBE_ENDPOINT,
    TranscribeOptions,
    TranscribeResult,
    EngineProbe,
    TranscriptionEngineDescriptor,
    TranscriptionErrorEnvelope,
    TranscriptionError,
    WorkerTranscriptionResponse,
    WorkerCapabilities,
)

_PUBLIC_WORKER_ERRORS: Mapping[int, tuple[str, str, bool]] = {
    400: (
        "worker_rejected_request",
        "transcription worker rejected the request",
        False,
    ),
    413: (
        "input_too_large",
        "audio input exceeds the transcription worker limit",
        False,
    ),
    429: (
        "at_capacity",
        "transcription worker is at capacity; retry shortly",
        True,
    ),
    500: ("worker_failure", "transcription worker failed", False),
    503: ("engine_unavailable", "transcription worker is unavailable", True),
    504: ("inference_timeout", "transcription worker timed out", True),
}
_FORWARDED_ERROR_STATUSES = frozenset(_PUBLIC_WORKER_ERRORS)
_MAX_WORKER_ERROR_BYTES = 64 * 1024
_MAX_RETRY_AFTER_SECONDS = 86_400
_WORKER_CONNECT_TIMEOUT_SECONDS = 5.0
_WORKER_WRITE_TIMEOUT_SECONDS = 300.0
_WORKER_POOL_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class WorkerEndpoint:
    """Static routing and expected identity for one worker."""

    expected_engine: str
    base_url: str
    token: str | None = field(repr=False)
    descriptor: TranscriptionEngineDescriptor
    timeout_seconds: float
    probe_timeout_seconds: float = 5.0
    catalog_from_config: bool = False

    def __post_init__(self) -> None:
        engine = self.expected_engine.strip()
        if not engine:
            raise ValueError("expected_engine must not be empty")
        if self.descriptor.engine != engine:
            raise ValueError(
                "worker descriptor engine does not match expected_engine: "
                f"{self.descriptor.engine!r} != {engine!r}"
            )

        base_url = self.base_url.rstrip("/")
        if "?" in base_url or "#" in base_url:
            raise ValueError("base_url must not contain a query or fragment")
        try:
            parsed = httpx.URL(base_url)
            port = parsed.port
        except httpx.InvalidURL:
            raise ValueError("base_url must be a valid http(s) root origin") from None
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not contain credentials")
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError("base_url port must be between 1 and 65535")
        if parsed.path not in {"", "/"}:
            raise ValueError("base_url must be a root origin without a path")
        base_url = str(parsed.copy_with(path="", query=None, fragment=None))
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not math.isfinite(self.timeout_seconds):
            raise ValueError("timeout_seconds must be finite")
        if self.probe_timeout_seconds <= 0:
            raise ValueError("probe_timeout_seconds must be positive")
        if not math.isfinite(self.probe_timeout_seconds):
            raise ValueError("probe_timeout_seconds must be finite")
        if self.token is not None and not self.token.strip():
            raise ValueError("token must be non-empty when provided")

        object.__setattr__(self, "expected_engine", engine)
        object.__setattr__(self, "base_url", base_url)

    @property
    def transcribe_url(self) -> str:
        return f"{self.base_url}{TRANSCRIBE_ENDPOINT}"

    @property
    def capabilities_url(self) -> str:
        return f"{self.base_url}/v1/capabilities"


class WorkerRegistry:
    """Static remote-worker registry; ``describe`` performs no I/O."""

    def __init__(self, endpoints: Iterable[WorkerEndpoint] = ()) -> None:
        self._endpoints: dict[str, WorkerEndpoint] = {}
        for endpoint in endpoints:
            self.register(endpoint)

    def register(self, endpoint: WorkerEndpoint) -> None:
        engine = endpoint.expected_engine
        if engine in self._endpoints:
            raise ValueError(f"duplicate transcription worker for engine {engine!r}")
        self._endpoints[engine] = endpoint

    def get(self, engine: str) -> WorkerEndpoint:
        try:
            return self._endpoints[engine]
        except KeyError:
            raise KeyError(f"unknown transcription worker engine {engine!r}") from None

    def describe(self) -> list[dict[str, Any]]:
        return [
            endpoint.descriptor.model_dump(mode="json")
            for endpoint in self._endpoints.values()
        ]

    def endpoints(self) -> tuple[WorkerEndpoint, ...]:
        return tuple(self._endpoints.values())


class WorkerGatewayError(Exception):
    """Serializable HTTP-shaped worker failure."""

    def __init__(
        self,
        *,
        status_code: int,
        envelope: TranscriptionErrorEnvelope,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(envelope.error.message)
        self.status_code = status_code
        self.envelope = envelope
        self.headers = dict(headers or {})


WorkerClientFactory = Callable[[WorkerEndpoint], httpx.AsyncClient]


class TranscriptionWorkerGateway:
    """Call isolated workers through the strict wire schema."""

    def __init__(
        self,
        registry: WorkerRegistry,
        *,
        client: httpx.AsyncClient | None = None,
        client_factory: WorkerClientFactory | None = None,
    ) -> None:
        if client is not None and client_factory is not None:
            raise ValueError("pass client or client_factory, not both")
        self.registry = registry
        self._client = client
        self._client_factory = client_factory

    async def __call__(
        self,
        *,
        engine: str,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> TranscribeResult:
        return await self.transcribe(
            engine=engine,
            audio_path=audio_path,
            options=options,
        )

    async def transcribe(
        self,
        *,
        engine: str,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> TranscribeResult:
        """Open the audio inside each attempt for retry-safe streaming."""

        try:
            endpoint = self.registry.get(engine)
        except KeyError:
            raise _gateway_error(
                status_code=400,
                code="unsupported_engine",
                message=f"unknown transcription engine {engine!r}",
                retryable=False,
            ) from None

        try:
            endpoint.descriptor.validate_options(options)
        except ValueError as exc:
            raise _gateway_error(
                status_code=400,
                code="unsupported_option",
                message=str(exc),
                retryable=False,
            ) from None

        owned_client = self._client is None
        client = self._client
        if client is None:
            if self._client_factory is not None:
                client = self._client_factory(endpoint)
            else:
                client = httpx.AsyncClient(follow_redirects=False)

        try:
            await self._preflight_inference(client=client, endpoint=endpoint)
            try:
                response = await self._post_once(
                    client=client,
                    endpoint=endpoint,
                    audio_path=audio_path,
                    options=options,
                )
            except httpx.TimeoutException:
                raise _gateway_error(
                    status_code=504,
                    code="inference_timeout",
                    message=f"transcription worker {engine!r} timed out",
                    retryable=True,
                ) from None
            except httpx.RequestError:
                raise _gateway_error(
                    status_code=502,
                    code="worker_unreachable",
                    message=f"transcription worker {engine!r} is unreachable",
                    retryable=True,
                ) from None
        finally:
            if owned_client:
                await client.aclose()

        if response.status_code == 200:
            return _parse_success(
                response,
                descriptor=endpoint.descriptor,
                options=options,
            )
        if response.status_code in _FORWARDED_ERROR_STATUSES:
            raise _parse_worker_error(response)
        raise _gateway_error(
            status_code=502,
            code="worker_protocol_error",
            message="transcription worker returned an unexpected HTTP status",
            retryable=True,
        )

    async def public_capabilities(self) -> list[dict[str, Any]]:
        """Return configured cold workers and probe ordinary worker state."""

        endpoints = self.registry.endpoints()
        if not endpoints:
            return []
        return list(
            await asyncio.gather(
                *(self._public_capability(endpoint) for endpoint in endpoints)
            )
        )

    async def _public_capability(self, endpoint: WorkerEndpoint) -> dict[str, Any]:
        if endpoint.catalog_from_config:
            return _public_capability_payload(
                endpoint.descriptor,
                EngineProbe(
                    available=True,
                    loaded=False,
                    error=None,
                ),
            )
        try:
            capabilities = await self._fetch_capabilities(
                endpoint,
                timeout_seconds=endpoint.probe_timeout_seconds,
            )
            descriptor = capabilities.descriptor
            probe = capabilities.probe
            if not _descriptor_matches(endpoint.descriptor, descriptor):
                raise _WorkerDescriptorMismatch
            if probe.available and descriptor.runtime_image_id is None:
                probe = EngineProbe(
                    available=False,
                    loaded=probe.loaded,
                    error="worker provenance is unavailable",
                )
            elif probe.error is not None:
                probe = EngineProbe(
                    available=False,
                    loaded=probe.loaded,
                    error="worker reported itself unavailable",
                )
        except Exception as exc:
            descriptor = endpoint.descriptor
            probe = EngineProbe(
                available=False,
                loaded=False,
                error=_public_probe_error(exc),
            )
        return _public_capability_payload(descriptor, probe)

    async def _preflight_inference(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: WorkerEndpoint,
    ) -> None:
        """Verify live provenance before opening or uploading audio."""

        try:
            capabilities = await self._fetch_capabilities(
                endpoint,
                client=client,
                timeout_seconds=endpoint.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.RequestError):
            raise _gateway_error(
                status_code=502,
                code="worker_unreachable",
                message="transcription worker is unreachable",
                retryable=True,
            ) from None
        except ValueError:
            raise _bad_worker_response() from None
        if not _descriptor_matches(endpoint.descriptor, capabilities.descriptor):
            raise _bad_worker_response()
        if capabilities.descriptor.runtime_image_id is None:
            raise _bad_worker_response()
        if not capabilities.probe.available or capabilities.probe.error is not None:
            raise _public_worker_status_error(503)

    async def _fetch_capabilities(
        self,
        endpoint: WorkerEndpoint,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float,
    ) -> WorkerCapabilities:
        owned_client = False
        if client is None:
            client = self._client
        if client is None:
            owned_client = True
            if self._client_factory is not None:
                client = self._client_factory(endpoint)
            else:
                client = httpx.AsyncClient(follow_redirects=False)
        headers = (
            {"Authorization": f"Bearer {endpoint.token}"} if endpoint.token else None
        )
        try:
            response = await client.get(
                endpoint.capabilities_url,
                headers=headers,
                timeout=timeout_seconds,
                follow_redirects=True,
            )
        finally:
            if owned_client:
                await client.aclose()
        if response.status_code != 200:
            raise ValueError(
                f"worker capabilities returned HTTP {response.status_code}"
            )
        try:
            capabilities = WorkerCapabilities.model_validate_json(
                response.content,
                strict=True,
            )
        except (ValidationError, ValueError):
            raise ValueError("worker capabilities response is malformed") from None
        if capabilities.contract_version != CONTRACT_VERSION:
            raise ValueError("worker capabilities version is incompatible")
        return capabilities

    async def _post_once(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: WorkerEndpoint,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> httpx.Response:
        data = {
            "contract_version": CONTRACT_VERSION,
            "engine": endpoint.expected_engine,
            "options": _canonical_options_json(options),
        }
        headers = (
            {"Authorization": f"Bearer {endpoint.token}"} if endpoint.token else None
        )
        with audio_path.open("rb") as audio:
            return await client.post(
                endpoint.transcribe_url,
                data=data,
                files={
                    "file": (
                        audio_path.name,
                        audio,
                        "application/octet-stream",
                    )
                },
                headers=headers,
                timeout=httpx.Timeout(
                    connect=_WORKER_CONNECT_TIMEOUT_SECONDS,
                    read=endpoint.timeout_seconds,
                    write=_WORKER_WRITE_TIMEOUT_SECONDS,
                    pool=_WORKER_POOL_TIMEOUT_SECONDS,
                ),
                follow_redirects=True,
            )


def _canonical_options_json(options: TranscribeOptions) -> str:
    return json.dumps(
        options.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )


def _public_capability_payload(
    descriptor: TranscriptionEngineDescriptor,
    probe: EngineProbe,
) -> dict[str, Any]:
    return {
        "name": descriptor.engine,
        "route": TRANSCRIBE_ENDPOINT,
        "available": probe.available and probe.error is None,
        "loaded": probe.loaded,
        "models": list(descriptor.model_ids),
        "error": probe.error,
        "contract_versions": [CONTRACT_VERSION],
        "revision": descriptor.revision,
        "runtime_image_id": descriptor.runtime_image_id,
        "options": descriptor.options.model_dump(mode="json"),
    }


def _descriptor_matches(
    expected: TranscriptionEngineDescriptor,
    actual: TranscriptionEngineDescriptor,
) -> bool:
    """Allow runtime to fill only an unknown runtime image identity."""

    expected_payload = expected.model_dump(mode="json")
    actual_payload = actual.model_dump(mode="json")
    if expected_payload["runtime_image_id"] is None:
        expected_payload["runtime_image_id"] = actual_payload["runtime_image_id"]
    return expected_payload == actual_payload


class _WorkerDescriptorMismatch(Exception):
    """Excluded from public diagnostics."""


def _public_probe_error(exc: Exception) -> str:
    """Return bounded diagnostics without remote or secret content."""

    if isinstance(exc, httpx.TimeoutException):
        return "worker capability probe timed out"
    if isinstance(exc, httpx.RequestError):
        return "worker is unreachable"
    if isinstance(exc, _WorkerDescriptorMismatch):
        return "worker descriptor is incompatible"
    return "worker capability probe failed"


def _parse_success(
    response: httpx.Response,
    *,
    descriptor: TranscriptionEngineDescriptor,
    options: TranscribeOptions,
) -> TranscribeResult:
    try:
        envelope = WorkerTranscriptionResponse.model_validate_json(
            response.content,
            strict=True,
        )
    except (ValidationError, ValueError):
        raise _bad_worker_response() from None
    if envelope.contract_version != CONTRACT_VERSION:
        raise _bad_worker_response()
    try:
        accepted_options = descriptor.validate_options(options)
        descriptor.validate_result(envelope.result, options)
    except ValueError:
        raise _bad_worker_response()
    if envelope.result.accepted_options != accepted_options:
        raise _bad_worker_response()
    return envelope.result


def _parse_worker_error(response: httpx.Response) -> WorkerGatewayError:
    if len(response.content) > _MAX_WORKER_ERROR_BYTES:
        return _bad_worker_response()
    try:
        envelope = TranscriptionErrorEnvelope.model_validate_json(
            response.content,
            strict=True,
        )
    except (ValidationError, ValueError):
        return _bad_worker_response()
    if envelope.contract_version != CONTRACT_VERSION:
        return _bad_worker_response()

    headers: dict[str, str] = {}
    if response.status_code == 429:
        retry_after = _safe_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            headers["Retry-After"] = retry_after
    failure = _public_worker_status_error(response.status_code)
    failure.headers.update(headers)
    return failure


def _safe_retry_after(value: str | None) -> str | None:
    """Accept only bounded delta-seconds from an untrusted worker."""

    if value is None or not value.isascii() or not value.isdigit():
        return None
    if len(value) > 6:
        return None
    seconds = int(value)
    if seconds > _MAX_RETRY_AFTER_SECONDS:
        return None
    return str(seconds)


def _public_worker_status_error(status_code: int) -> WorkerGatewayError:
    code, message, retryable = _PUBLIC_WORKER_ERRORS[status_code]
    return _gateway_error(
        status_code=status_code,
        code=code,
        message=message,
        retryable=retryable,
    )


def _bad_worker_response() -> WorkerGatewayError:
    return _gateway_error(
        status_code=502,
        code="worker_protocol_error",
        message="transcription worker returned a malformed or incompatible response",
        retryable=True,
    )


def _gateway_error(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
) -> WorkerGatewayError:
    return WorkerGatewayError(
        status_code=status_code,
        envelope=TranscriptionErrorEnvelope(
            contract_version=CONTRACT_VERSION,
            error=TranscriptionError(
                code=code,
                message=message,
                retryable=retryable,
                details={},
            ),
        ),
    )


__all__ = [
    "TranscriptionWorkerGateway",
    "WorkerEndpoint",
    "WorkerGatewayError",
    "WorkerRegistry",
]
