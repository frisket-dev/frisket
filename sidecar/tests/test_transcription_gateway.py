"""Boundary-B gateway tests: no network and no model dependencies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from frisket_models.transcription.contract import (
    CONTRACT_VERSION,
    DiarizationMode,
    SpeakerHint,
    TranscribeOptionName,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
    TranscriptionEngineDescriptor,
    TranscriptionErrorEnvelope,
    TranscriptionError,
    TranscriptionOptionSupport,
    EngineProbe,
    WorkerCapabilities,
    WorkerTranscriptionResponse,
)
from frisket_models.transcription.gateway import (
    TranscriptionWorkerGateway,
    WorkerEndpoint,
    WorkerGatewayError,
    WorkerRegistry,
)

ENGINE = "fixture-intrinsic"


def _descriptor(engine: str = ENGINE) -> TranscriptionEngineDescriptor:
    return TranscriptionEngineDescriptor(
        engine=engine,
        model_ids=["example/transcription-fixture"],
        revision="0123456789abcdef",
        runtime_image_id="oci:sha256:" + ("a" * 64),
        options=TranscriptionOptionSupport(
            diarization_mode=DiarizationMode.INTRINSIC,
            speaker_hint=SpeakerHint.NONE,
            language=True,
            model_size=False,
            vad=False,
            context=True,
        ),
    )


def _result(engine: str = ENGINE) -> TranscribeResult:
    return TranscribeResult(
        engine=engine,
        text="hello from the worker",
        segments=[],
        language="en",
        duration=1.25,
        model_ids=["example/transcription-fixture"],
        revision="0123456789abcdef",
        device="cuda:0",
        dtype="bfloat16",
        timings={"inference_seconds": 0.4},
        warnings=[],
        accepted_options={
            TranscribeOptionName.LANGUAGE: "en",
            TranscribeOptionName.CONTEXT: "Frisket",
        },
    )


def _endpoint(*, timeout_seconds: float = 37.0) -> WorkerEndpoint:
    return WorkerEndpoint(
        expected_engine=ENGINE,
        base_url="http://moss-worker:8601/",
        token="worker-secret",
        descriptor=_descriptor(),
        timeout_seconds=timeout_seconds,
    )


def _options() -> TranscribeOptions:
    return TranscribeOptions(language="en", context="Frisket")


def _success_response(result: TranscribeResult | None = None) -> httpx.Response:
    envelope = WorkerTranscriptionResponse(
        contract_version=CONTRACT_VERSION,
        result=result or _result(),
    )
    return httpx.Response(
        200,
        content=envelope.model_dump_json(),
        headers={"Content-Type": "application/json"},
    )


def _error_response(
    status_code: int,
    *,
    code: str = "worker_failure",
    retry_after: str | None = None,
) -> httpx.Response:
    envelope = TranscriptionErrorEnvelope(
        contract_version=CONTRACT_VERSION,
        error=TranscriptionError(
            code=code,
            message="worker rejected the request with private-worker-marker",
            retryable=status_code in {429, 503, 504},
            details={
                "engine": ENGINE,
                "reason": "private-worker-marker",
            },
        ),
    )
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return httpx.Response(
        status_code,
        content=envelope.model_dump_json(),
        headers=headers,
    )


async def _call(
    *,
    audio_path: Path,
    handler,
    options: TranscribeOptions | None = None,
    endpoint: WorkerEndpoint | None = None,
    capabilities: WorkerCapabilities | None = None,
    preflight_requests: list[httpx.Request] | None = None,
) -> TranscribeResult:
    selected_endpoint = endpoint or _endpoint()
    runtime_capabilities = capabilities or WorkerCapabilities(
        contract_version=CONTRACT_VERSION,
        descriptor=selected_endpoint.descriptor,
        probe=EngineProbe(available=True, loaded=False, error=None),
    )

    async def routed_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if preflight_requests is not None:
                preflight_requests.append(request)
            return httpx.Response(
                200,
                content=runtime_capabilities.model_dump_json(),
            )
        return await handler(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(routed_handler)
    ) as client:
        gateway = TranscriptionWorkerGateway(
            WorkerRegistry([selected_endpoint]),
            client=client,
        )
        return await gateway.transcribe(
            engine=ENGINE,
            audio_path=audio_path,
            options=options or _options(),
        )


def test_registry_is_remote_and_descriptor_driven() -> None:
    endpoint = _endpoint()
    registry = WorkerRegistry([endpoint])

    assert registry.get(ENGINE) is endpoint
    assert registry.describe() == [endpoint.descriptor.model_dump(mode="json")]

    with pytest.raises(ValueError, match="does not match"):
        WorkerEndpoint(
            expected_engine="some-other-engine",
            base_url="http://worker:8601",
            token=None,
            descriptor=_descriptor(),
            timeout_seconds=1,
        )
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(endpoint)
    with pytest.raises(ValueError, match="finite"):
        WorkerEndpoint(
            expected_engine=ENGINE,
            base_url="http://worker:8601",
            token=None,
            descriptor=_descriptor(),
            timeout_seconds=float("inf"),
        )
    with pytest.raises(ValueError, match="non-empty"):
        WorkerEndpoint(
            expected_engine=ENGINE,
            base_url="http://worker:8601",
            token="   ",
            descriptor=_descriptor(),
            timeout_seconds=1,
        )


def test_public_capabilities_validate_worker_and_allow_runtime_digest() -> None:
    descriptor = _descriptor().model_copy(update={"runtime_image_id": None})
    endpoint = WorkerEndpoint(
        expected_engine=ENGINE,
        base_url="http://moss-worker:8601",
        token="worker-secret",
        descriptor=descriptor,
        timeout_seconds=3,
    )
    runtime_descriptor = descriptor.model_copy(
        update={"runtime_image_id": "oci:sha256:" + ("b" * 64)}
    )
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            content=WorkerCapabilities(
                contract_version=CONTRACT_VERSION,
                descriptor=runtime_descriptor,
                probe=EngineProbe(available=True, loaded=False, error=None),
            ).model_dump_json(),
        )

    async def scenario() -> list[dict]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = TranscriptionWorkerGateway(
                WorkerRegistry([endpoint]), client=client
            )
            return await gateway.public_capabilities()

    (capability,) = asyncio.run(scenario())
    assert seen == {
        "path": "/v1/capabilities",
        "auth": "Bearer worker-secret",
        "timeout": {
            "connect": 5.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        },
    }
    assert capability["available"] is True
    assert capability["loaded"] is False
    assert capability["revision"] == descriptor.revision
    assert capability["runtime_image_id"] == runtime_descriptor.runtime_image_id
    assert capability["contract_versions"] == [CONTRACT_VERSION]


def test_public_capabilities_degrade_incompatible_worker_to_unavailable() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"contract_version": "wrong"})

    async def scenario() -> list[dict]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = TranscriptionWorkerGateway(
                WorkerRegistry([_endpoint()]), client=client
            )
            return await gateway.public_capabilities()

    (capability,) = asyncio.run(scenario())
    assert capability["available"] is False
    assert capability["loaded"] is False
    assert capability["error"] == "worker capability probe failed"


def test_success_posts_canonical_boundary_b_multipart(tmp_path: Path) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"RIFF-retry-safe-audio")
    seen: dict[str, object] = {}
    preflights: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        seen.update(
            method=request.method,
            path=request.url.path,
            auth=request.headers.get("Authorization"),
            body=body,
            timeout=request.extensions.get("timeout"),
        )
        return _success_response()

    result = asyncio.run(
        _call(
            audio_path=audio_path,
            handler=handler,
            preflight_requests=preflights,
        )
    )

    assert result == _result()
    assert len(preflights) == 1
    preflight = preflights[0]
    assert preflight.method == "GET"
    assert preflight.url.path == "/v1/capabilities"
    assert preflight.headers.get("Authorization") == "Bearer worker-secret"
    assert preflight.extensions.get("timeout") == {
        "connect": 37.0,
        "read": 37.0,
        "write": 37.0,
        "pool": 37.0,
    }
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/transcribe"
    assert seen["auth"] == "Bearer worker-secret"
    body = seen["body"]
    assert isinstance(body, bytes)
    assert b'name="contract_version"' in body
    assert CONTRACT_VERSION.encode() in body
    assert b'name="engine"' in body and ENGINE.encode() in body
    assert b'name="options"' in body
    assert b'{"context":"Frisket","language":"en"}' in body
    assert b'name="file"; filename="clip.wav"' in body
    assert b'name="files"' not in body
    assert b"RIFF-retry-safe-audio" in body
    timeout = seen["timeout"]
    assert isinstance(timeout, dict)
    assert timeout == {
        "connect": 5.0,
        "read": 37.0,
        "write": 300.0,
        "pool": 5.0,
    }


def test_inference_preflight_uses_inference_timeout_but_public_probe_stays_short(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"audio")
    endpoint = _endpoint(timeout_seconds=37.0)
    capabilities = WorkerCapabilities(
        contract_version=CONTRACT_VERSION,
        descriptor=endpoint.descriptor,
        probe=EngineProbe(available=True, loaded=False, error=None),
    )
    get_timeouts: list[dict[str, float]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            get_timeouts.append(request.extensions["timeout"])
            return httpx.Response(200, content=capabilities.model_dump_json())
        return _success_response()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = TranscriptionWorkerGateway(
                WorkerRegistry([endpoint]),
                client=client,
            )
            await gateway.transcribe(
                engine=ENGINE,
                audio_path=audio_path,
                options=_options(),
            )
            await gateway.public_capabilities()

    asyncio.run(scenario())

    assert [timeout["read"] for timeout in get_timeouts] == [37.0, 5.0]


def test_modal_worker_result_redirects_are_followed_for_preflight_and_post(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"audio")
    endpoint = _endpoint()
    capabilities = WorkerCapabilities(
        contract_version=CONTRACT_VERSION,
        descriptor=endpoint.descriptor,
        probe=EngineProbe(available=True, loaded=False, error=None),
    )
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                303,
                headers={"Location": "/modal-result/capabilities"},
            )
        if request.url.path == "/modal-result/capabilities":
            return httpx.Response(200, content=capabilities.model_dump_json())
        if request.url.path == "/v1/transcribe":
            return httpx.Response(
                303,
                headers={"Location": "/modal-result/transcribe"},
            )
        if request.url.path == "/modal-result/transcribe":
            return _success_response()
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def scenario() -> TranscribeResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = TranscriptionWorkerGateway(
                WorkerRegistry([endpoint]),
                client=client,
            )
            return await gateway.transcribe(
                engine=ENGINE,
                audio_path=audio_path,
                options=_options(),
            )

    assert asyncio.run(scenario()) == _result()
    assert requests == [
        ("GET", "/v1/capabilities"),
        ("GET", "/modal-result/capabilities"),
        ("POST", "/v1/transcribe"),
        ("GET", "/modal-result/transcribe"),
    ]


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_code"),
    [
        ("descriptor-drift", 502, "worker_protocol_error"),
        ("unavailable", 503, "engine_unavailable"),
        ("missing-runtime-image-id", 502, "worker_protocol_error"),
    ],
)
def test_inference_preflight_fails_before_upload(
    tmp_path: Path,
    kind: str,
    expected_status: int,
    expected_code: str,
) -> None:
    descriptor = _descriptor()
    if kind == "missing-runtime-image-id":
        descriptor = descriptor.model_copy(update={"runtime_image_id": None})
    endpoint = WorkerEndpoint(
        expected_engine=ENGINE,
        base_url="http://moss-worker:8601",
        token="worker-secret",
        descriptor=descriptor,
        timeout_seconds=37,
    )
    runtime_descriptor = descriptor
    probe = EngineProbe(available=True, loaded=False, error=None)
    if kind == "descriptor-drift":
        runtime_descriptor = descriptor.model_copy(
            update={"runtime_image_id": "oci:sha256:" + ("b" * 64)}
        )
    elif kind == "unavailable":
        probe = EngineProbe(
            available=False,
            loaded=False,
            error="private-worker-preflight-marker",
        )
    capabilities = WorkerCapabilities(
        contract_version=CONTRACT_VERSION,
        descriptor=runtime_descriptor,
        probe=probe,
    )
    posted = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posted
        posted = True
        return _success_response()

    with pytest.raises(WorkerGatewayError) as caught:
        asyncio.run(
            _call(
                audio_path=tmp_path / "must-not-be-opened.wav",
                handler=handler,
                endpoint=endpoint,
                capabilities=capabilities,
            )
        )

    assert posted is False
    assert caught.value.status_code == expected_status
    assert caught.value.envelope.error.code == expected_code
    assert caught.value.envelope.error.details == {}
    assert "private-worker-preflight-marker" not in (
        caught.value.envelope.model_dump_json()
    )


@pytest.mark.parametrize("failure", ["connect", "timeout"])
def test_inference_preflight_transport_failure_is_sanitized_502(
    tmp_path: Path,
    failure: str,
) -> None:
    audio_path = tmp_path / "must-not-be-uploaded.wav"
    audio_path.write_bytes(b"private-audio-marker")
    posted = False
    private_marker = "private-preflight-transport-marker"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        if request.method == "GET":
            if failure == "connect":
                raise httpx.ConnectError(private_marker, request=request)
            raise httpx.ReadTimeout(private_marker, request=request)
        posted = True
        return _success_response()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = TranscriptionWorkerGateway(
                WorkerRegistry([_endpoint()]),
                client=client,
            )
            with pytest.raises(WorkerGatewayError) as caught:
                await gateway.transcribe(
                    engine=ENGINE,
                    audio_path=audio_path,
                    options=_options(),
                )

        error = caught.value
        assert error.status_code == 502
        assert error.envelope.error.code == "worker_unreachable"
        assert error.envelope.error.retryable is True
        assert error.envelope.error.details == {}
        assert private_marker not in error.envelope.model_dump_json()

    asyncio.run(scenario())
    assert posted is False


@pytest.mark.parametrize("status_code", [400, 413, 429, 500, 503, 504])
def test_structured_worker_statuses_are_normalized_without_private_details(
    tmp_path: Path,
    status_code: int,
) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"audio")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _error_response(
            status_code,
            code=f"worker_{status_code}",
            retry_after="7" if status_code == 429 else None,
        )

    with pytest.raises(WorkerGatewayError) as caught:
        asyncio.run(_call(audio_path=audio_path, handler=handler))

    error = caught.value
    assert error.status_code == status_code
    expected = {
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
    }[status_code]
    assert (
        error.envelope.error.code,
        error.envelope.error.message,
        error.envelope.error.retryable,
    ) == expected
    assert error.envelope.error.details == {}
    assert "private-worker-marker" not in error.envelope.model_dump_json()
    assert f"worker_{status_code}" not in error.envelope.model_dump_json()
    assert error.headers == ({"Retry-After": "7"} if status_code == 429 else {})


@pytest.mark.parametrize("retry_after", ["Wed, 21 Oct 2015 07:28:00 GMT", "999999"])
def test_untrusted_worker_retry_after_is_bounded(
    tmp_path: Path,
    retry_after: str,
) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"audio")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return _error_response(429, retry_after=retry_after)

    with pytest.raises(WorkerGatewayError) as caught:
        asyncio.run(_call(audio_path=audio_path, handler=handler))

    assert caught.value.headers == {}


@pytest.mark.parametrize(
    "kind",
    [
        "malformed",
        "wrong-version",
        "wrong-engine",
        "wrong-model",
        "wrong-revision",
        "wrong-options-receipt",
        "missing-intrinsic-speaker",
        "unexpected-status",
    ],
)
def test_incompatible_worker_responses_become_stable_502(
    tmp_path: Path,
    kind: str,
) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"audio")

    async def handler(_request: httpx.Request) -> httpx.Response:
        if kind == "malformed":
            return httpx.Response(200, content=b'{"contract_version":')
        if kind == "wrong-version":
            payload = json.loads(
                WorkerTranscriptionResponse(
                    contract_version=CONTRACT_VERSION,
                    result=_result(),
                ).model_dump_json()
            )
            payload["contract_version"] = "frisket.transcription.v2"
            return httpx.Response(200, json=payload)
        if kind == "wrong-engine":
            return _success_response(_result(engine="wrong-engine"))
        if kind == "wrong-model":
            return _success_response(
                _result().model_copy(update={"model_ids": ["private/wrong-model"]})
            )
        if kind == "wrong-revision":
            return _success_response(
                _result().model_copy(update={"revision": "private-wrong-revision"})
            )
        if kind == "wrong-options-receipt":
            return _success_response(
                _result().model_copy(
                    update={
                        "accepted_options": {
                            TranscribeOptionName.LANGUAGE: "fr",
                            TranscribeOptionName.CONTEXT: "Frisket",
                        }
                    }
                )
            )
        if kind == "missing-intrinsic-speaker":
            return _success_response(
                _result().model_copy(
                    update={
                        "segments": [
                            TranscribeSegment(
                                start=0.0,
                                end=1.0,
                                text="speaker label omitted",
                            )
                        ]
                    }
                )
            )
        return httpx.Response(418, json={"detail": "not the v1 envelope"})

    with pytest.raises(WorkerGatewayError) as caught:
        asyncio.run(_call(audio_path=audio_path, handler=handler))

    assert caught.value.status_code == 502
    assert caught.value.envelope.error.code == "worker_protocol_error"
    assert caught.value.envelope.error.retryable is True


def test_transport_timeout_becomes_stable_504(tmp_path: Path) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"audio")

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("worker took too long", request=request)

    with pytest.raises(WorkerGatewayError) as caught:
        asyncio.run(_call(audio_path=audio_path, handler=handler))

    assert caught.value.status_code == 504
    assert caught.value.envelope.error.code == "inference_timeout"
    assert caught.value.envelope.error.retryable is True


def test_a_second_call_reconstructs_the_file_body(tmp_path: Path) -> None:
    """The retry primitive reopens the path; it never reuses an exhausted body."""

    audio_path = tmp_path / "clip.wav"
    marker = b"audio-present-on-every-attempt"
    audio_path.write_bytes(marker)
    bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                content=WorkerCapabilities(
                    contract_version=CONTRACT_VERSION,
                    descriptor=_descriptor(),
                    probe=EngineProbe(
                        available=True,
                        loaded=False,
                        error=None,
                    ),
                ).model_dump_json(),
            )
        bodies.append(await request.aread())
        if len(bodies) == 1:
            return _error_response(503)
        return _success_response()

    async def scenario() -> TranscribeResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = TranscriptionWorkerGateway(
                WorkerRegistry([_endpoint()]),
                client=client,
            )
            with pytest.raises(WorkerGatewayError) as first:
                await gateway.transcribe(
                    engine=ENGINE,
                    audio_path=audio_path,
                    options=_options(),
                )
            assert first.value.status_code == 503
            return await gateway.transcribe(
                engine=ENGINE,
                audio_path=audio_path,
                options=_options(),
            )

    assert asyncio.run(scenario()) == _result()
    assert len(bodies) == 2
    assert all(marker in body for body in bodies)


def test_unknown_engine_fails_before_any_http_call(tmp_path: Path) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"audio")
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _success_response()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = TranscriptionWorkerGateway(WorkerRegistry(), client=client)
            with pytest.raises(WorkerGatewayError) as caught:
                await gateway.transcribe(
                    engine="missing",
                    audio_path=audio_path,
                    options=TranscribeOptions(),
                )
            assert caught.value.status_code == 400
            assert caught.value.envelope.error.code == "unsupported_engine"

    asyncio.run(scenario())
    assert called is False


def test_unsupported_option_fails_before_any_http_call(tmp_path: Path) -> None:
    audio_path = tmp_path / "clip.wav"
    audio_path.write_bytes(b"audio")
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _success_response()

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = TranscriptionWorkerGateway(
                WorkerRegistry([_endpoint()]),
                client=client,
            )
            with pytest.raises(WorkerGatewayError) as caught:
                await gateway.transcribe(
                    engine=ENGINE,
                    audio_path=audio_path,
                    # Intrinsic engines do not accept an optional diarize knob.
                    options=TranscribeOptions(diarize=True),
                )
            assert caught.value.status_code == 400
            assert caught.value.envelope.error.code == "unsupported_option"

    asyncio.run(scenario())
    assert called is False
