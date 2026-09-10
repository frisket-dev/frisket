"""Environment discovery for code-owned transcription worker registrations."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from frisket_models import app as app_module
from frisket_models.transcription import config as config_module
from frisket_models.app import create_app
from frisket_models.engines import Registry
from frisket_models.transcription.config import (
    MOSS_DEFINITION,
    PARAKEET_DEFINITION,
    WorkerDefinition,
    worker_registry_from_env,
)
from frisket_models.transcription.contract import (
    CONTRACT_VERSION,
    DiarizationMode,
    EngineProbe,
    SpeakerHint,
    TranscriptionEngineDescriptor,
    TranscriptionOptionSupport,
    WorkerCapabilities,
)
from frisket_models.transcription.gateway import (
    TranscriptionWorkerGateway,
    WorkerRegistry,
)
from frisket_models.transcription.moss import MOSS_DESCRIPTOR
from frisket_models.transcription.parakeet_tdt import PARAKEET_DESCRIPTOR

ENGINE = "fixture-intrinsic"
PREFIX = "FRISKET_TRANSCRIPTION_FIXTURE_WORKER"
TOKEN = "fixture-worker-secret"


def _definition() -> WorkerDefinition:
    return WorkerDefinition(
        engine=ENGINE,
        env_prefix=PREFIX,
        descriptor=TranscriptionEngineDescriptor(
            engine=ENGINE,
            model_ids=["example/fixture"],
            revision="0123456789abcdef",
            runtime_image_id=None,
            options=TranscriptionOptionSupport(
                diarization_mode=DiarizationMode.INTRINSIC,
                speaker_hint=SpeakerHint.NONE,
                language=True,
                model_size=False,
                vad=False,
                context=True,
            ),
        ),
    )


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        f"{PREFIX}_URL": "http://fixture-worker:8601",
        f"{PREFIX}_TOKEN": TOKEN,
    }
    values.update(overrides)
    return values


def test_empty_environment_is_an_empty_no_network_registry() -> None:
    registry = worker_registry_from_env(definitions=[_definition()], environ={})

    assert registry.endpoints() == ()
    assert registry.describe() == []


def test_production_moss_definition_is_opt_in_and_uses_reviewed_descriptor() -> None:
    assert MOSS_DEFINITION.descriptor is MOSS_DESCRIPTOR
    assert worker_registry_from_env(environ={}).endpoints() == ()

    registry = worker_registry_from_env(
        environ={
            "FRISKET_TRANSCRIPTION_MOSS_WORKER_URL": "http://moss-worker:9000",
            "FRISKET_TRANSCRIPTION_MOSS_WORKER_TOKEN": TOKEN,
        }
    )

    (endpoint,) = registry.endpoints()
    assert endpoint.expected_engine == "moss"
    assert endpoint.descriptor is MOSS_DESCRIPTOR


def test_production_parakeet_definition_is_cold_selectable() -> None:
    assert PARAKEET_DEFINITION.descriptor is PARAKEET_DESCRIPTOR

    registry = worker_registry_from_env(
        environ={
            "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER_URL": (
                "https://parakeet.example.test"
            ),
            "FRISKET_TRANSCRIPTION_PARAKEET_TDT_WORKER_TOKEN": TOKEN,
        }
    )

    (endpoint,) = registry.endpoints()
    assert endpoint.expected_engine == "parakeet-tdt"
    assert endpoint.catalog_from_config is True
    assert endpoint.descriptor is PARAKEET_DESCRIPTOR


def test_configured_moss_stays_selectable_without_waking_scale_to_zero_worker() -> None:
    registry = worker_registry_from_env(
        environ={
            "FRISKET_TRANSCRIPTION_MOSS_WORKER_URL": "https://moss.example.test",
            "FRISKET_TRANSCRIPTION_MOSS_WORKER_TOKEN": TOKEN,
        }
    )

    async def unexpected_probe(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"catalog woke cold worker: {request.url}")

    async def catalog() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(unexpected_probe)
        ) as client:
            gateway = TranscriptionWorkerGateway(registry, client=client)
            return (await gateway.public_capabilities())[0]

    capability = asyncio.run(catalog())
    assert capability["name"] == "moss"
    assert capability["available"] is True
    assert capability["loaded"] is False
    assert capability["error"] is None
    assert capability["models"] == list(MOSS_DESCRIPTOR.model_ids)


def test_environment_builds_endpoint_without_exposing_token_in_repr() -> None:
    registry = worker_registry_from_env(
        definitions=[_definition()], environ=_environment()
    )

    (endpoint,) = registry.endpoints()
    assert endpoint.expected_engine == ENGINE
    assert endpoint.base_url == "http://fixture-worker:8601"
    assert endpoint.token == TOKEN
    assert endpoint.timeout_seconds == 3600.0
    assert endpoint.probe_timeout_seconds == 5.0
    assert TOKEN not in repr(endpoint)


def test_file_backed_token_accepts_one_terminal_newline(tmp_path: Path) -> None:
    token_file = tmp_path / "worker-token"
    token_file.write_bytes((TOKEN + "\r\n").encode())
    environ = _environment()
    environ.pop(f"{PREFIX}_TOKEN")
    environ[f"{PREFIX}_TOKEN_FILE"] = str(token_file)

    (endpoint,) = worker_registry_from_env(
        definitions=[_definition()], environ=environ
    ).endpoints()

    assert endpoint.token == TOKEN


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        (
            {f"{PREFIX}_TOKEN": TOKEN},
            f"{PREFIX}_URL must be set",
        ),
        (
            {f"{PREFIX}_URL": "http://fixture-worker:8601"},
            "no worker token source",
        ),
        (
            {
                f"{PREFIX}_URL": "http://fixture-worker:8601",
                f"{PREFIX}_TOKEN": TOKEN,
                f"{PREFIX}_TOKEN_FILE": "/secret",
            },
            "set exactly one",
        ),
        (
            _environment(**{f"{PREFIX}_URL": ""}),
            f"{PREFIX}_URL must not be empty",
        ),
        (
            _environment(**{f"{PREFIX}_TOKEN": " padded "}),
            "invalid bearer token",
        ),
        (
            _environment(**{f"{PREFIX}_URL": "http://user:secret@worker:8601"}),
            "credential-free",
        ),
        (
            _environment(**{f"{PREFIX}_URL": "http://worker:8601/prefix"}),
            "root origin",
        ),
        (
            _environment(**{f"{PREFIX}_URL": "http://worker:8601?token=secret"}),
            "credential-free",
        ),
        (
            _environment(**{f"{PREFIX}_URL": "http://worker:8601?"}),
            "credential-free",
        ),
        (
            _environment(**{f"{PREFIX}_URL": "http://worker:8601#"}),
            "credential-free",
        ),
        (
            _environment(**{f"{PREFIX}_URL": "http://worker:0"}),
            "credential-free",
        ),
        (
            _environment(**{f"{PREFIX}_URL": "http://worker:65536"}),
            "credential-free",
        ),
        (
            _environment(**{f"{PREFIX}_TIMEOUT_SECONDS": "nan"}),
            "positive finite",
        ),
        (
            _environment(**{f"{PREFIX}_PROBE_TIMEOUT_SECONDS": "31"}),
            "must not exceed 30",
        ),
    ],
)
def test_partial_or_unsafe_configuration_fails_closed(
    environ: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        worker_registry_from_env(definitions=[_definition()], environ=environ)


def test_token_file_rejects_symlinks_and_oversized_content(tmp_path: Path) -> None:
    real_file = tmp_path / "real-token"
    real_file.write_text(TOKEN)
    symlink = tmp_path / "linked-token"
    symlink.symlink_to(real_file)
    oversized = tmp_path / "oversized-token"
    oversized.write_bytes(b"a" * 4097)

    for path in (symlink, oversized):
        environ = _environment()
        environ.pop(f"{PREFIX}_TOKEN")
        environ[f"{PREFIX}_TOKEN_FILE"] = str(path)
        with pytest.raises(ValueError, match="regular file"):
            worker_registry_from_env(definitions=[_definition()], environ=environ)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not getattr(os, "O_NONBLOCK", 0),
    reason="FIFO nonblocking flags require a POSIX host",
)
def test_token_file_rejects_fifo_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo = tmp_path / "worker-token-fifo"
    os.mkfifo(fifo)
    real_open = config_module.os.open

    def guarded_open(path, flags):
        assert flags & os.O_NONBLOCK
        return real_open(path, flags)

    monkeypatch.setattr(config_module.os, "open", guarded_open)
    environ = _environment()
    environ.pop(f"{PREFIX}_TOKEN")
    environ[f"{PREFIX}_TOKEN_FILE"] = str(fifo)

    with pytest.raises(ValueError, match="regular file"):
        worker_registry_from_env(definitions=[_definition()], environ=environ)


def test_invalid_url_parser_details_are_not_reflected() -> None:
    marker = "private-port-marker"
    environ = _environment(**{f"{PREFIX}_URL": f"http://worker:{marker}"})

    with pytest.raises(ValueError) as caught:
        worker_registry_from_env(definitions=[_definition()], environ=environ)

    assert str(caught.value) == (
        f"{PREFIX}_URL must be an absolute credential-free http(s) root origin"
    )
    assert marker not in str(caught.value)


def test_worker_origin_is_normalized_before_routes_are_appended() -> None:
    environ = _environment(**{f"{PREFIX}_URL": "HTTP://FIXTURE-WORKER:80/"})

    (endpoint,) = worker_registry_from_env(
        definitions=[_definition()], environ=environ
    ).endpoints()

    assert endpoint.base_url == "http://fixture-worker"
    assert endpoint.capabilities_url == "http://fixture-worker/v1/capabilities"


def test_explicit_gateway_injection_does_not_read_ambient_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_loader():
        raise AssertionError("ambient worker configuration was read")

    monkeypatch.setattr(app_module, "worker_registry_from_env", unexpected_loader)
    gateway = TranscriptionWorkerGateway(WorkerRegistry())

    app = create_app(
        token="models-token",
        registry=Registry([]),
        transcription_gateway=gateway,
    )

    assert app.state.transcription_gateway is gateway


def test_public_probe_redacts_remote_errors_and_requires_runtime_digest() -> None:
    definition = _definition()
    registry = worker_registry_from_env(
        definitions=[definition], environ=_environment()
    )

    async def leaking_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not connect using {TOKEN}", request=request)

    async def unavailable() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(leaking_handler)
        ) as client:
            gateway = TranscriptionWorkerGateway(registry, client=client)
            return (await gateway.public_capabilities())[0]

    capability = asyncio.run(unavailable())
    assert capability["available"] is False
    assert capability["error"] == "worker is unreachable"
    assert TOKEN not in str(capability)

    runtime_descriptor = definition.descriptor.model_copy(
        update={"runtime_image_id": "oci:sha256:" + ("b" * 64)}
    )

    async def reported_error_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=WorkerCapabilities(
                contract_version=CONTRACT_VERSION,
                descriptor=runtime_descriptor,
                probe=EngineProbe(
                    available=False,
                    loaded=False,
                    error=f"native server rejected {TOKEN}",
                ),
            ).model_dump_json(),
        )

    async def reported_error() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(reported_error_handler)
        ) as client:
            gateway = TranscriptionWorkerGateway(registry, client=client)
            return (await gateway.public_capabilities())[0]

    capability = asyncio.run(reported_error())
    assert capability["available"] is False
    assert capability["error"] == "worker reported itself unavailable"
    assert TOKEN not in str(capability)

    async def missing_digest_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=WorkerCapabilities(
                contract_version=CONTRACT_VERSION,
                descriptor=definition.descriptor,
                probe=EngineProbe(available=True, loaded=True, error=None),
            ).model_dump_json(),
        )

    async def missing_digest() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(missing_digest_handler)
        ) as client:
            gateway = TranscriptionWorkerGateway(registry, client=client)
            return (await gateway.public_capabilities())[0]

    capability = asyncio.run(missing_digest())
    assert capability["available"] is False
    assert capability["loaded"] is True
    assert capability["error"] == "worker provenance is unavailable"
