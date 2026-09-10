"""Import-light ASGI factories for a models-only GPU host."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from frisket_models.app import create_app
from frisket_models.engines import Registry
from frisket_models.transcription.config import (
    PRODUCTION_WORKER_DEFINITIONS,
    WorkerDefinition,
    worker_registry_from_env,
)
from frisket_models.transcription.contract import (
    AdapterRegistration,
    EngineProbe,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
    TranscribeWord,
    TranscriptionEngineDescriptor,
    TranscriptionOptionSupport,
)
from frisket_models.transcription.gateway import TranscriptionWorkerGateway
from frisket_models.transcription.whisper_turbo import WHISPER_TURBO_DEFINITION
from frisket_models.transcription.worker import create_worker_app

CONTRACT_STUB_ENGINE = "contract-stub"
CONTRACT_STUB_ENV_PREFIX = "FRISKET_TRANSCRIPTION_CONTRACT_STUB_WORKER"
CONTRACT_STUB_MODEL_ID = "frisket/contract-stub"
CONTRACT_STUB_REVISION = "transcription-contract-v1"

CONTRACT_STUB_DESCRIPTOR = TranscriptionEngineDescriptor(
    engine=CONTRACT_STUB_ENGINE,
    model_ids=[CONTRACT_STUB_MODEL_ID],
    revision=CONTRACT_STUB_REVISION,
    runtime_image_id=None,
    options=TranscriptionOptionSupport(
        diarization_mode="intrinsic",
        speaker_hint="none",
        language=True,
        model_size=False,
        vad=False,
        context=True,
    ),
)

CONTRACT_STUB_DEFINITION = WorkerDefinition(
    engine=CONTRACT_STUB_ENGINE,
    env_prefix=CONTRACT_STUB_ENV_PREFIX,
    descriptor=CONTRACT_STUB_DESCRIPTOR,
)


class _ContractStubAdapter:
    """Model-free deployment smoke-test adapter."""

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> TranscribeResult:
        # Verify the spool handoff without reflecting caller data.
        if not audio_path.is_file():
            raise RuntimeError("contract stub did not receive a spooled audio file")

        word = TranscribeWord(word="contract", start=0.0, end=0.25)
        segment = TranscribeSegment(
            start=0.0,
            end=0.25,
            text="contract",
            speaker="SPEAKER_00",
            speaker_confidence="diagnostic",
            words=[word],
        )
        return TranscribeResult(
            engine=CONTRACT_STUB_ENGINE,
            text=segment.text,
            segments=[segment],
            language=options.language,
            duration=segment.end,
            model_ids=[CONTRACT_STUB_MODEL_ID],
            revision=CONTRACT_STUB_REVISION,
            device="diagnostic",
            dtype="none",
            timings={"diagnostic_seconds": 0.0},
            warnings=["diagnostic contract stub; no model inference was run"],
            accepted_options=options.supplied_options(),
        )


CONTRACT_STUB_REGISTRATION = AdapterRegistration(
    descriptor=CONTRACT_STUB_DESCRIPTOR,
    factory=_ContractStubAdapter,
    probe=lambda: EngineProbe(available=True, loaded=False, error=None),
)


def create_models_gateway_app() -> FastAPI:
    """Create a public gateway with no in-process model engines."""

    definitions = (
        *PRODUCTION_WORKER_DEFINITIONS,
        CONTRACT_STUB_DEFINITION,
        WHISPER_TURBO_DEFINITION,
    )
    worker_registry = worker_registry_from_env(definitions=definitions)
    public_token = os.environ.get("FRISKET_MODELS_TOKEN")
    if public_token is not None and any(
        endpoint.token == public_token for endpoint in worker_registry.endpoints()
    ):
        raise RuntimeError("public gateway and internal worker tokens must be distinct")
    spool_dir = os.environ.get("FRISKET_TRANSCRIPTION_SPOOL_DIR")
    return create_app(
        registry=Registry([]),
        concurrency=1,
        transcription_gateway=TranscriptionWorkerGateway(worker_registry),
        transcription_spool_dir=spool_dir or None,
    )


def create_contract_stub_worker_app() -> FastAPI:
    """Create the fail-closed, concurrency-one diagnostic worker."""

    token = os.environ.get("FRISKET_TRANSCRIPTION_WORKER_TOKEN")
    if not token:
        raise RuntimeError(
            "FRISKET_TRANSCRIPTION_WORKER_TOKEN is required for the diagnostic "
            "transcription worker"
        )
    concurrency = int(os.environ.get("FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY", "1"))
    if concurrency != 1:
        raise ValueError("GPU worker inference concurrency must be exactly 1")
    max_upload_bytes = int(
        os.environ.get("FRISKET_TRANSCRIPTION_WORKER_MAX_UPLOAD_BYTES", "262144000")
    )
    if max_upload_bytes < 1:
        raise ValueError("GPU worker upload limit must be positive")
    return create_worker_app(
        CONTRACT_STUB_REGISTRATION,
        token=token,
        concurrency=concurrency,
        max_upload_bytes=max_upload_bytes,
    )


__all__ = [
    "CONTRACT_STUB_DEFINITION",
    "CONTRACT_STUB_DESCRIPTOR",
    "CONTRACT_STUB_ENGINE",
    "CONTRACT_STUB_REGISTRATION",
    "create_contract_stub_worker_app",
    "create_models_gateway_app",
]
