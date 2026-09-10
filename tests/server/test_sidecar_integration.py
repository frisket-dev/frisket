"""Application wiring for the shared ``frisket-models`` sidecar client,
transcription tier, doctor capability probe, and Compose ``models`` profile.

The orphan ``ops/_sidecar.py`` is now THE client (rule-of-three: ocr + convert
+ transcribe). These tests pin:

- transcribe's sidecar tier round-trips POST /v1/transcribe and reports a
  HONEST cost 0.0 (self-hosted compute, not the unknown=None of metered APIs)
- the 429 sleep-and-retry behavior the ops hand-rolled is preserved in the
  shared client
- all THREE ops (ocr/convert/transcribe) reach the sidecar via ``sidecar_post``
- ``frisket doctor`` parses /capabilities into per-engine availability
- the repo-root docker-compose.yml parses with the 'models' profile

External-response cases use ``httpx.MockTransport``; the v1 continuous-chain
proof uses nested ``ASGITransport`` instances for the real sidecar and worker
apps.
"""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

import asyncio
import json
import math
import struct
import subprocess
import sys
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from pathlib import Path

import httpx
import pytest

from frisket.ops import _sidecar
from frisket.ops.base import OpContext
from frisket.contracts.transcription_sidecar import (
    TRANSCRIPTION_CONTEXT_MAX_CHARS,
    TRANSCRIPTION_CONTRACT_VERSION,
    TranscriptionEngineDescriptor,
    TranscriptionErrorEnvelope,
    TranscriptionOptions,
    TranscriptionOptionSupport,
    TranscriptionResponse,
)
from frisket.contracts.actions.schemas._engines import (
    EngineDeclaration,
    TranscriptionEngineCapabilities,
    project_transcription_engine_options,
)
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.ai.models.metadata import model_calls_cost_actual

from helpers import requires_ocr_runtime

# The fixed hosted model has one identity across catalog, route, and v1 wire.
SIDECAR_ENGINE = "whisper-turbo"
TURBO_MODEL_ID = "dropbox-dash/faster-whisper-large-v3-turbo"
V1_TEST_ENGINE = "synthetic-intrinsic-v1"
TRANSCRIPTION_V1_GOLDEN = (
    Path(__file__).resolve().parents[2]
    / "sidecar"
    / "tests"
    / "fixtures"
    / "transcription.golden.json"
)


def _wav(path, seconds=0.3, freq=220.0):
    sr = 16000
    n = int(sr * seconds)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / sr)))
        for i in range(n)
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)
    return path


def _ctx(handler) -> OpContext:
    return OpContext(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _transcribe_action(
    tmp_path, monkeypatch, handler, *, engine="whisper-turbo", **options
):
    async def post(_client, url, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await client.request("POST", url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    path = _wav(tmp_path / "a.wav")
    project = Project.create(tmp_path / "transcribe.frisket")
    try:
        sheet = project.add_sheet("Audio")
        column = project.add_column(sheet, "audio", type="audio")
        digest = project.add_blob(path.read_bytes(), filename="a.wav", mime="audio/wav")
        project.add_rows(
            sheet,
            [{"audio": {"blob": digest, "filename": "a.wav", "mime": "audio/wav"}}],
            {"audio": column},
        )
        request = {
            "action_id": "media.transcribe",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "audio", "engine": engine, **options},
            "output_names": {
                "text": "transcript",
                "segments": "transcript_segments",
            },
            "idempotency_key": "sidecar-transcription",
        }
        if transcribe_engines.transcribe_engine_capabilities(engine).detects_language:
            request["output_names"]["detected_language"] = "detected_language"
        result = run_action_spec(project, request, project_id="sidecar")
        if result.status == "needs_confirmation":
            request["confirmation"] = result.errors[0].details["promise_set_hash"]
            result = run_action_spec(project, request, project_id="sidecar")
        values = {
            output.name: next(
                iter(project.get_values(sheet, output.column_id).values())
            )
            for output in result.outputs
            if output.column_id is not None
        }
        calls = (
            RunResultStore(project).model_calls(result.run_id) if result.run_id else []
        )
        return result, values, calls
    finally:
        project.close()


def _routed_gateway_ctx(handler) -> OpContext:
    """A ROUTE-BOUND gateway dispatch context (the engine-roster cutover: the gateway whisper
    is reached through a route or the ephemeral deref — the dead gateway
    spellings no longer exist, and env is never read by dispatch)."""
    from frisket.engine.store.execution_routes import RouteRow
    from frisket.execution.provider import ConnectionConfig
    from frisket.execution.resolver import CandidateBinding, RouteRowFacts
    from frisket.execution.attempt import (
        ATTEMPT_EXTRA,
        AttemptCommitment,
        RoutedAdmission,
    )
    from frisket.execution.promise_compiler import OperatorBorneZeroCost
    from frisket.execution.targets import ExecutionTarget

    route = RouteRow(
        id="route_TESTROUTE0000000000000000",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_TEST",
        engine="whisper-turbo",
        options={},
        target_snapshot={
            "target_id": "models-gateway",
            "capability": "transcribe",
            "transport": "frisket.transcription.v1",
            "run_scoped": False,
        },
        route_fact_hash="hash",
        operator="self",
        egress_class="operator_lan",
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
        created_at="2026-07-24T00:00:00+00:00",
    )
    binding = CandidateBinding(
        facts=RouteRowFacts(
            target_id="models-gateway",
            engine="whisper-turbo",
            operator="self",
            egress_class="operator_lan",
            region=None,
            credential_source="local",
            cost_posture="operator_borne",
        ),
        connection=ConnectionConfig(base_url="http://models:8500", token="sekrit"),
        target=ExecutionTarget(
            id="models-gateway", operator="self", egress_class="operator_lan"
        ),
    )
    # The route and its binding reach an adapter as one value, the attempt.
    attempt = AttemptCommitment(
        attempt_id="attempt_TESTATTEMPT000000000000",
        run_id=1,
        seq=0,
        identity="identity",
        scope=(1,),
        admission=RoutedAdmission(
            head_route_id=route.id,
            head_promise_set_id=route.promise_set_id,
            route=route,
            promise_set=None,
            binding=binding,
            evaluation=None,
            admitted_by_consent_id=None,
        ),
        cost_basis=OperatorBorneZeroCost(),
        price_card_version=None,
    )
    return OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={ATTEMPT_EXTRA: attempt},
    )


def _install_synthetic_v1_engine(monkeypatch, engine: str = V1_TEST_ENGINE) -> str:
    from frisket.contracts.actions.schemas import media

    declaration = EngineDeclaration(
        id=engine,
        label="Synthetic intrinsic transcription",
        tier="sidecar",
        provider="frisket-sidecar",
        transcription=TranscriptionEngineCapabilities(
            transport="frisket.transcription.v1",
            language_mode="single",
            detects_language=True,
            diarization_mode="intrinsic",
            speaker_hint="none",
        ),
    )
    monkeypatch.setattr(
        media,
        "TRANSCRIBE_ENGINE_TABLE",
        (*media.TRANSCRIBE_ENGINE_TABLE, declaration),
    )
    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.common.project_transcription_engine_options",
        partial(
            project_transcription_engine_options, table=media.TRANSCRIBE_ENGINE_TABLE
        ),
    )
    return engine


@pytest.fixture
def synthetic_v1_engine(monkeypatch) -> str:
    """Install a test-only product row without registering a real GPU model."""

    return _install_synthetic_v1_engine(monkeypatch)


# ---------------------------------------------------------------------------
# transcribe sidecar tier


def test_transcribe_sidecar_roundtrip(tmp_path, monkeypatch):
    """Whisper Turbo uses the strict v1 route and controls."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.update(
            url=str(request.url),
            auth=request.headers.get("authorization"),
            sent_audio=b"RIFF" in body,
            sent_engine=b"whisper-turbo" in body,
            sent_lang=b"language" in body and b"en" in body,
        )
        return httpx.Response(
            200,
            json={
                "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
                "results": [
                    _transcription_v1_result(
                        engine=SIDECAR_ENGINE,
                        text=" hello from the sidecar ",
                        segments=[
                            {"start": 0.0, "end": 0.3, "text": " hello "},
                            {
                                "start": 0.3,
                                "end": 0.6,
                                "text": "from the sidecar",
                            },
                        ],
                        model_ids=[TURBO_MODEL_ID],
                        device="cpu",
                        dtype="int8",
                    )
                ],
            },
        )

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    result, out, calls = _transcribe_action(
        tmp_path, monkeypatch, handler, language=["en"]
    )
    assert result.status == "completed", result.errors
    assert seen["url"].endswith("/v1/transcribe")
    assert seen["auth"] == "Bearer sekrit"
    assert seen["sent_audio"] and seen["sent_engine"] and seen["sent_lang"]
    assert out["transcript"] == "hello from the sidecar"
    assert out["transcript_segments"] == [
        {"start": 0.0, "end": 0.3, "text": "hello", "segment_index": 0},
        {"start": 0.3, "end": 0.6, "text": "from the sidecar", "segment_index": 1},
    ]
    assert out["detected_language"] == "en"
    # HONEST 0.0: self-hosted compute has no per-request meter, so the
    # marginal price is genuinely zero — NOT the None a metered API reports
    # when its price is unknown (the 'no fabricated costs' rule: unknown
    # ≠ zero; here zero is a fact).
    assert calls
    assert model_calls_cost_actual(calls) == 0.0


def test_transcribe_gateway_wire_uses_the_named_turbo_identity(tmp_path, monkeypatch):
    """Whisper Turbo has one identity across the authored route and v1 wire."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["engine_on_wire"] = b"whisper-turbo" in request.read()
        return httpx.Response(
            200,
            json={
                "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
                "results": [
                    _transcription_v1_result(
                        engine=SIDECAR_ENGINE,
                        text="x",
                        segments=[],
                        model_ids=[TURBO_MODEL_ID],
                        device="cpu",
                        dtype="int8",
                        accepted_options={},
                    )
                ],
            },
        )

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    result, _, _ = _transcribe_action(tmp_path, monkeypatch, handler)
    assert result.status == "completed", result.errors
    assert seen["engine_on_wire"]


def test_transcribe_sidecar_needs_url(tmp_path, monkeypatch):
    """An unrouted gateway engine (moss) without FRISKET_MODELS_URL fails
    with the target's activation remedy via the ephemeral binding deref —
    never a silent fallback, never a crash."""
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)

    def forbidden(request):
        raise AssertionError("Missing route must refuse before HTTP dispatch")

    result, values, calls = _transcribe_action(
        tmp_path, monkeypatch, forbidden, engine="moss"
    )
    assert result.status == "failed", result.errors
    assert "FRISKET_MODELS_URL" in str(result.errors)
    assert not values and not calls


def _transcription_v1_result(**overrides):
    """Stub-fidelity pin (critique A3): the base payload is validated
    through the REAL wire model before any intentional per-test override is
    applied — a stub that drifts from the v1 contract fails here, not by
    silently testing against a shape the sidecar never sends."""
    result = {
        "engine": V1_TEST_ENGINE,
        "text": " hello from the GPU sidecar ",
        "segments": [
            {
                "start": 0.0,
                "end": 0.3,
                "text": " hello ",
                "speaker": "speaker-1",
                "speaker_confidence": "approximate",
                "words": [{"word": "hello", "start": 0.0, "end": 0.3}],
            }
        ],
        "language": "en",
        "duration": 0.3,
        "model_ids": ["Systran/faster-whisper-large-v3"],
        "revision": "pinned-revision",
        "device": "cuda:0",
        "dtype": "float16",
        "timings": {"load_seconds": 1.2, "inference_seconds": 0.4},
        "warnings": ["detected two speakers"],
        "accepted_options": {"language": "en"},
    }
    from frisket.contracts.transcription_sidecar import TranscriptionResult

    TranscriptionResult.model_validate(result)
    result.update(overrides)
    return result


def test_app_transcription_v1_models_match_shared_golden():
    """The app cannot import the sidecar package, so the language-neutral
    golden detects drift between the two strict copies of Boundary A."""
    # rule19: versioned wire fixture is test data shared across two packages
    golden = json.loads(TRANSCRIPTION_V1_GOLDEN.read_text())
    success = {
        "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
        "results": [golden["result"]],
    }

    assert (
        TranscriptionResponse.model_validate(success).model_dump(mode="json") == success
    )
    assert (
        TranscriptionErrorEnvelope.model_validate(golden["error"]).model_dump(
            mode="json"
        )
        == golden["error"]
    )

    invalid = json.loads(json.dumps(success))
    del invalid["results"][0]["segments"][0]["speaker"]
    with pytest.raises(ValueError, match="speaker_confidence requires"):
        TranscriptionResponse.model_validate(invalid)


def test_app_transcription_v1_context_wire_bound_matches_product_cap():
    """One contract, three surfaces (the contract): the 500-char `context` cap
    is defined by the wire contract, imported by the app schema, mirrored by
    the sidecar copy (parity-tested there) and the web maxLength. The wire
    itself rejects an over-cap prompt — the bound is not app-validation-only."""
    from frisket.contracts.actions.schemas.media import MAX_TRANSCRIBE_CONTEXT_CHARS

    assert MAX_TRANSCRIBE_CONTEXT_CHARS == TRANSCRIPTION_CONTEXT_MAX_CHARS
    at_cap = "x" * TRANSCRIPTION_CONTEXT_MAX_CHARS
    assert TranscriptionOptions.model_validate({"context": at_cap}).context == at_cap
    for invalid_context in ("", "x" * (TRANSCRIPTION_CONTEXT_MAX_CHARS + 1)):
        with pytest.raises(ValueError):
            TranscriptionOptions.model_validate({"context": invalid_context})


def test_app_transcription_v1_descriptor_models_match_shared_golden():
    """the capability contract mirrors the ability-descriptor types into the app contract copy
    through the same language-neutral golden, which pins both
    strict copies so drift fails CI on either side."""
    golden = json.loads(TRANSCRIPTION_V1_GOLDEN.read_text())

    for key in ("descriptor", "descriptor_optional_count"):
        payload = golden[key]
        descriptor = TranscriptionEngineDescriptor.model_validate(payload)
        assert descriptor.model_dump(mode="json") == payload
        assert (
            TranscriptionOptionSupport.model_validate(payload["options"]).model_dump(
                mode="json"
            )
            == payload["options"]
        )

    # Mirror-faithfulness: runtime_image_id keeps the sidecar's None default (a
    # leaf registers before the runtime injects its image identity).
    assert (
        TranscriptionEngineDescriptor.model_validate(
            golden["descriptor_optional_count"]
        ).runtime_image_id
        is None
    )

    # Closed model: unknown fields are drift, not passthrough.
    with pytest.raises(ValueError):
        TranscriptionEngineDescriptor.model_validate(
            {**golden["descriptor"], "future_field": True}
        )
    # Mirrored declaration validator: counts require the optional mode.
    with pytest.raises(ValueError, match="speaker_hint"):
        TranscriptionOptionSupport.model_validate(
            {**golden["descriptor"]["options"], "speaker_hint": "count"}
        )
    # Mirrored digest pattern.
    with pytest.raises(ValueError, match="pattern"):
        TranscriptionEngineDescriptor.model_validate(
            {**golden["descriptor"], "runtime_image_id": "sha256:not-a-digest"}
        )


def test_transcribe_sidecar_v1_sends_canonical_options_and_preserves_result(
    tmp_path, monkeypatch, synthetic_v1_engine
):
    """A declared v1 engine routes automatically and projects its options."""
    calls = []

    async def fake_sidecar_post(ctx, route, **kwargs):
        calls.append((ctx, route, kwargs))
        return {
            "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
            "results": [_transcription_v1_result()],
        }

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.sidecar.sidecar_post", fake_sidecar_post
    )
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    audio = _wav(tmp_path / "a.wav")
    ctx = OpContext(http=None)
    result = asyncio.run(
        transcribe_engines.TranscriptionV1Adapter().transcribe(
            synthetic_v1_engine,
            str(audio),
            {
                "language": ["en"],
                "vad": False,
                # Intrinsic diarization and speaker-count knobs are engine
                # facts, not inputs, and must not cross Boundary A.
                "diarize": True,
                "num_speakers": 2,
            },
            ctx,
        )
    )

    assert len(calls) == 1
    called_ctx, route, kwargs = calls[0]
    assert called_ctx is ctx
    assert route == "/v1/transcribe"
    assert kwargs["data"] == {
        "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
        "engine": V1_TEST_ENGINE,
        "options": '{"language":"en"}',
    }
    assert kwargs["op"] == "transcribe"
    assert kwargs["light_engine"] == "faster_whisper"
    timeout = kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 10.0
    assert timeout.read == 3600.0
    assert timeout.write == 300.0
    assert timeout.pool == 10.0
    assert kwargs["files"][0][0] == "files"
    assert kwargs["files"][0][1][0] == "a.wav"

    assert result == {
        "text": "hello from the GPU sidecar",
        "segments": [
            {
                "start": 0.0,
                "end": 0.3,
                "text": "hello",
                "speaker": "speaker-1",
                "speaker_confidence": "approximate",
                "words": [{"word": "hello", "start": 0.0, "end": 0.3}],
                "segment_index": 0,
            }
        ],
        "language": "en",
        "duration": 0.3,
        "model_ids": ["Systran/faster-whisper-large-v3"],
        "revision": "pinned-revision",
        "device": "cuda:0",
        "dtype": "float16",
        "timings": {"load_seconds": 1.2, "inference_seconds": 0.4},
        "warnings": ["detected two speakers"],
        "accepted_options": {"language": "en"},
        "cost": 0.0,
    }

    model_call = transcribe_engines.transcription_model_calls(
        V1_TEST_ENGINE,
        str(audio),
        {},
        result,
        transport="frisket.transcription.v1",
    )[0]
    assert model_call["model_ids"] == ["Systran/faster-whisper-large-v3"]
    assert (
        model_call["units"]
        | {
            "model_revision": "pinned-revision",
            "device": "cuda:0",
            "dtype": "float16",
            "timing_load_seconds": 1.2,
            "timing_inference_seconds": 0.4,
            "option_language": "en",
        }
        == model_call["units"]
    )
    assert model_call["warnings"] == ["detected two speakers"]


def test_transcribe_sidecar_v1_forwards_declared_context(tmp_path, monkeypatch):
    """the capability contract: a v1 engine declaring `context` forwards the trimmed hotword prompt
    verbatim in the wire options; the option filter no longer drops it."""
    from frisket.contracts.actions.schemas import media

    engine = "synthetic-context-v1"
    declaration = EngineDeclaration(
        id=engine,
        label="Synthetic context-capable transcription",
        tier="sidecar",
        provider="frisket-sidecar",
        transcription=TranscriptionEngineCapabilities(
            transport="frisket.transcription.v1",
            language_mode="auto_only",
            detects_language=False,
            diarization_mode="intrinsic",
            speaker_hint="none",
            context=True,
        ),
    )
    monkeypatch.setattr(
        media,
        "TRANSCRIBE_ENGINE_TABLE",
        (*media.TRANSCRIBE_ENGINE_TABLE, declaration),
    )
    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.common.project_transcription_engine_options",
        partial(
            project_transcription_engine_options, table=media.TRANSCRIBE_ENGINE_TABLE
        ),
    )

    calls = []

    async def fake_sidecar_post(ctx, route, **kwargs):
        calls.append((route, kwargs))
        return {
            "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
            "results": [
                _transcription_v1_result(
                    engine=engine,
                    language=None,
                    accepted_options={"context": "Frisket, Sortformer"},
                )
            ],
        }

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.sidecar.sidecar_post", fake_sidecar_post
    )
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    audio = _wav(tmp_path / "hotwords.wav")
    result = asyncio.run(
        transcribe_engines.TranscriptionV1Adapter().transcribe(
            engine,
            str(audio),
            {"context": "Frisket, Sortformer"},
            OpContext(http=None),
        )
    )

    assert len(calls) == 1
    route, kwargs = calls[0]
    assert route == "/v1/transcribe"
    assert kwargs["data"]["options"] == '{"context":"Frisket, Sortformer"}'
    assert result["accepted_options"] == {"context": "Frisket, Sortformer"}


def test_transcribe_sidecar_v1_continuous_asgi_chain(tmp_path, monkeypatch):
    """The Frisket recipe crosses both HTTP boundaries and the adapter seam."""

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "sidecar" / "src")
    )
    from frisket_models.app import create_app as create_sidecar_app
    from frisket_models.engines import Registry as SidecarRegistry
    from frisket_models.transcription.gateway import (
        TranscriptionWorkerGateway,
        WorkerEndpoint,
        WorkerRegistry,
    )
    from frisket_models.transcription.models_gpu_runtime import (
        CONTRACT_STUB_DESCRIPTOR,
        CONTRACT_STUB_ENGINE,
        CONTRACT_STUB_REGISTRATION,
    )
    from frisket_models.transcription.worker import create_worker_app

    public_token = "public-sidecar-secret"
    worker_token = "internal-worker-secret"
    runtime_image_id = "oci:sha256:" + ("d" * 64)
    engine = _install_synthetic_v1_engine(monkeypatch, CONTRACT_STUB_ENGINE)
    gateway_spool = tmp_path / "gateway-spool"
    worker_spool = tmp_path / "worker-spool"
    gateway_spool.mkdir()
    worker_spool.mkdir()

    worker_app = create_worker_app(
        CONTRACT_STUB_REGISTRATION,
        token=worker_token,
        concurrency=1,
        spool_dir=worker_spool,
        runtime_image_id=runtime_image_id,
    )
    endpoint = WorkerEndpoint(
        expected_engine=engine,
        base_url="http://worker.internal",
        token=worker_token,
        descriptor=CONTRACT_STUB_DESCRIPTOR,
        timeout_seconds=30,
    )

    def worker_client(_endpoint: WorkerEndpoint) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker_app),
            base_url="http://worker.internal",
        )

    sidecar_app = create_sidecar_app(
        token=public_token,
        registry=SidecarRegistry([]),
        concurrency=1,
        transcription_gateway=TranscriptionWorkerGateway(
            WorkerRegistry([endpoint]),
            client_factory=worker_client,
        ),
        transcription_spool_dir=gateway_spool,
    )
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", public_token)
    audio = _wav(tmp_path / "continuous.wav")

    async def invoke() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=sidecar_app),
            base_url="http://models.test",
        ) as client:
            return await transcribe_engines.TranscriptionV1Adapter().transcribe(
                engine,
                str(audio),
                {
                    "language": ["en"],
                    "diarize": True,
                    "num_speakers": 2,
                },
                OpContext(http=client),
            )

    result = asyncio.run(invoke())

    assert result["segments"] == [
        {
            "start": 0.0,
            "end": 0.25,
            "text": "contract",
            "speaker": "SPEAKER_00",
            "speaker_confidence": "diagnostic",
            "words": [{"word": "contract", "start": 0.0, "end": 0.25}],
            "segment_index": 0,
        }
    ]
    assert result["model_ids"] == ["frisket/contract-stub"]
    assert result["revision"] == "transcription-contract-v1"
    assert result["accepted_options"] == {"language": "en"}
    assert result["warnings"] == [
        "diagnostic contract stub; no model inference was run"
    ]
    assert list(gateway_spool.iterdir()) == []
    assert list(worker_spool.iterdir()) == []


@pytest.mark.parametrize(
    "body",
    [
        # Legacy/unversioned responses never pass as v1 by accident.
        {"results": [_transcription_v1_result()]},
        {
            "contract_version": "frisket.transcription.v2",
            "results": [_transcription_v1_result()],
        },
        {
            "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
            "results": [_transcription_v1_result(unexpected="drift")],
        },
    ],
)
def test_transcribe_sidecar_v1_rejects_malformed_or_wrong_version(
    body, tmp_path, monkeypatch, synthetic_v1_engine
):
    async def fake_sidecar_post(ctx, route, **kwargs):
        del ctx, route, kwargs
        return body

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.sidecar.sidecar_post", fake_sidecar_post
    )
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    audio = _wav(tmp_path / "a.wav")

    with pytest.raises(RuntimeError, match="malformed sidecar transcription v1"):
        asyncio.run(
            transcribe_engines.TranscriptionV1Adapter().transcribe(
                synthetic_v1_engine,
                str(audio),
                {},
                OpContext(http=None),
            )
        )


def test_transcribe_sidecar_v1_surfaces_structured_error(
    tmp_path, monkeypatch, synthetic_v1_engine
):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/transcribe"
        return httpx.Response(
            503,
            json={
                "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
                "error": {
                    "code": "engine_unavailable",
                    "message": "MOSS worker is not ready",
                    "retryable": True,
                    "details": {"engine": "moss"},
                },
            },
        )

    audio = _wav(tmp_path / "a.wav")

    with pytest.raises(
        RuntimeError,
        match=r"sidecar transcription error \(engine_unavailable\): MOSS worker",
    ):
        asyncio.run(
            transcribe_engines.TranscriptionV1Adapter().transcribe(
                synthetic_v1_engine,
                str(audio),
                {},
                _ctx(handler),
            )
        )


def test_non_200_success_shaped_body_cannot_become_a_success(
    tmp_path, monkeypatch, synthetic_v1_engine
):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
                "results": [_transcription_v1_result()],
            },
        )

    audio = _wav(tmp_path / "a.wav")
    with pytest.raises(RuntimeError, match=r"sidecar transcribe failed \(503\)"):
        asyncio.run(
            transcribe_engines.TranscriptionV1Adapter().transcribe(
                synthetic_v1_engine,
                str(audio),
                {},
                _ctx(handler),
            )
        )


# ---------------------------------------------------------------------------
# 429 sleep-and-retry (preserved from the hand-rolled ocr/convert clients)


def test_sidecar_post_retries_on_429(monkeypatch):
    """A 429 + Retry-After makes the client sleep-and-retry politely (the
    sidecar backpressure contract); a subsequent 200 succeeds. We patch the
    backoff sleep to keep the test fast and assert it was called."""
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    attempts = {"n": 0}
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(_sidecar.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="full")
        return httpx.Response(200, json={"ok": True})

    body = asyncio.run(
        _sidecar.sidecar_post(
            _ctx(handler), "/ocr", files=[], data={"engine": "dots.mocr"}, op="ocr"
        )
    )
    assert body == {"ok": True}
    assert attempts["n"] == 3  # two 429s, then a 200
    assert slept == [2.0, 2.0]  # Retry-After is authoritative


def test_sidecar_post_raises_after_persistent_429(monkeypatch):
    """Still 429 after MAX_ATTEMPTS → RuntimeError (never a silent give-up)."""
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(_sidecar.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"}, text="full")

    with pytest.raises(RuntimeError, match="sidecar ocr failed .429."):
        asyncio.run(
            _sidecar.sidecar_post(
                _ctx(handler), "/ocr", files=[], data={"engine": "dots.mocr"}, op="ocr"
            )
        )
    # Four attempts have only three gaps; never sleep after the terminal 429.
    assert slept == [2.0, 2.0, 2.0]


@pytest.mark.parametrize("retry_after", ["tomorrow", "nan", "inf", "999999"])
def test_sidecar_post_invalid_retry_after_uses_bounded_fallback(
    monkeypatch, retry_after
):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    slept = []
    attempts = 0

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(_sidecar.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"ok": True})

    body = asyncio.run(
        _sidecar.sidecar_post(
            _ctx(handler), "/ocr", files=[], data={"engine": "dots.mocr"}, op="ocr"
        )
    )

    assert body == {"ok": True}
    assert slept == [_sidecar.BACKOFF_SECONDS]


def test_sidecar_post_requires_token_when_url_configured(monkeypatch):
    """A configured sidecar URL without a bearer token is a local config error,
    not a generic 401 from the service."""
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "   ")
    called = {"http": False}

    def handler(request: httpx.Request) -> httpx.Response:
        called["http"] = True
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(
        RuntimeError,
        match="FRISKET_MODELS_TOKEN",
    ):
        asyncio.run(
            _sidecar.sidecar_post(
                _ctx(handler), "/ocr", files=[], data={"engine": "dots.mocr"}, op="ocr"
            )
        )
    assert called["http"] is False


def test_sidecar_headers_stays_pure_when_url_configured_without_token(monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "   ")

    assert _sidecar.sidecar_headers() == {}


# ---------------------------------------------------------------------------
# all THREE ops reach the sidecar via the shared client (rule-of-three)


def test_all_three_ops_call_sidecar_post(tmp_path, monkeypatch):
    """ocr/convert/transcribe each route their sidecar engine through THE
    shared ``sidecar_post`` (the extraction's whole point — one URL+bearer+
    429-retry, three consumers)."""
    from frisket.ops import ocr_engines as ocr
    from frisket.ops import ocr_engines_sidecar as ocr_sidecar
    from frisket.sdk.ops import transcribe_engines
    from tests.document_conversion_helpers import bound_document_converter
    from frisket.engine.executor import document_convert as convert
    from tests.ops.test_to_markdown_route_binding import (
        _attempt_extras,
        _gateway_binding,
        _route,
    )
    from frisket.execution.provider import ConnectionConfig

    calls = []

    async def fake_ocr_post(ctx, route, **kwargs):
        calls.append(("ocr", route, kwargs))
        return {"pages": [{"text": "ocr text", "blocks": []}]}

    async def fake_convert_post(ctx, route, **kwargs):
        calls.append(("convert", route, kwargs))
        return {"documents": [{"markdown": "# converted", "ocr_used": [False]}]}

    async def fake_transcribe_post(ctx, route, **kwargs):
        calls.append(("transcribe", route, kwargs))
        return {
            "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
            "results": [
                _transcription_v1_result(
                    engine="whisper-turbo",
                    text=" sidecar transcript ",
                    segments=[{"start": 0.0, "end": 0.3, "text": " sidecar "}],
                    model_ids=[TURBO_MODEL_ID],
                    device="cpu",
                    dtype="int8",
                    accepted_options={
                        "language": "en",
                        "vad": False,
                        "context": "Frisket",
                    },
                )
            ],
        }

    monkeypatch.setattr(ocr_sidecar, "sidecar_post", fake_ocr_post)
    monkeypatch.setattr(convert, "sidecar_post", fake_convert_post)
    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.sidecar.sidecar_post", fake_transcribe_post
    )
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")

    ctx = OpContext(http=None)
    page = tmp_path / "page.png"
    page.write_bytes(b"png")
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"%PDF-1.4")
    audio = _wav(tmp_path / "a.wav")

    binding = _gateway_binding(
        ConnectionConfig(base_url="http://models:8500", token="sekrit")
    )
    ocr_ctx = OpContext(
        extras=_attempt_extras(
            _route(
                engine="dots.mocr",
                target_snapshot={
                    "target_id": "models-gateway",
                    "capability": "ocr",
                    "transport": "sidecar.ocr",
                    "run_scoped": False,
                },
            ),
            replace(binding, facts=replace(binding.facts, engine="dots.mocr")),
        )
    )
    pages = asyncio.run(ocr.OcrEngines()._ocr_sidecar("dots.mocr", [page], ocr_ctx))
    convert_ctx = OpContext(
        extras=_attempt_extras(
            _route(),
            _gateway_binding(
                ConnectionConfig(base_url="http://models:8500", token="sekrit")
            ),
        )
    )
    markdown, ocr_used = asyncio.run(
        bound_document_converter(convert_ctx, engine="docling")._convert_sidecar(
            "docling", doc
        )
    )
    transcript = asyncio.run(
        transcribe_engines.TranscriptionV1Adapter().transcribe(
            "whisper-turbo",
            str(audio),
            {
                "language": "en",
                "vad": False,
                "context": "Frisket",
            },
            ctx,
        )
    )

    assert pages == [{"text": "ocr text", "blocks": []}]
    assert markdown == "# converted"
    assert ocr_used == [False]
    assert transcript["text"] == "sidecar transcript"
    assert transcript["segments"] == [
        {"start": 0.0, "end": 0.3, "text": "sidecar", "segment_index": 0}
    ]
    assert transcript["language"] == "en"
    assert transcript["cost"] == 0.0

    assert [(name, route) for name, route, _ in calls] == [
        ("ocr", "/ocr"),
        ("convert", "/to-markdown"),
        ("transcribe", "/v1/transcribe"),
    ]
    ocr_call, convert_call, transcribe_call = [kw for _, _, kw in calls]
    assert ocr_call["data"] == {"engine": "dots.mocr"}
    assert convert_call["data"] == {"engine": "docling"}
    assert transcribe_call["data"] == {
        "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
        "engine": "whisper-turbo",
        "options": '{"context":"Frisket","language":"en","vad":false}',
    }
    assert ocr_call["op"] == "ocr"
    assert convert_call["op"] == "convert"
    assert transcribe_call["op"] == "transcribe"
    assert ocr_call["files"][0][1][0] == "page.png"
    assert convert_call["files"][0][1][0] == "doc.pdf"
    assert transcribe_call["files"][0][1][0] == "a.wav"


# ---------------------------------------------------------------------------
# doctor capabilities parsing


def test_doctor_reports_sidecar_capabilities(monkeypatch, capsys):
    """`frisket doctor` probes /capabilities when FRISKET_MODELS_URL is set
    and reports per-engine up/down — exercised through the real doctor()."""
    import httpx as _httpx

    import frisket.cli as cli

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")

    calls: list[dict] = []

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        calls.append({"url": url, "headers": headers})
        return _httpx.Response(
            200,
            json={
                "service": "frisket-models",
                "version": "0.1.0",
                "engines": [
                    {"name": "dots.mocr", "route": "/ocr", "available": True},
                    {"name": "docling", "route": "/to-markdown", "available": False},
                    {
                        "name": "whisper-turbo",
                        "route": "/v1/transcribe",
                        "available": True,
                        "contract_versions": ["frisket.transcription.v1"],
                    },
                ],
            },
            request=_httpx.Request("GET", url),
        )

    monkeypatch.setattr(_httpx, "get", fake_get)
    assert cli.doctor_cmd() == 0  # sidecar info never fails the doctor
    out = capsys.readouterr().out
    assert "models sidecar" in out
    # Doctor makes other HTTP probes too (e.g. the yt-dlp version check) —
    # assert on the capabilities call specifically, not on call order.
    cap = next(c for c in calls if c["url"] == "http://models:8500/capabilities")
    assert cap["headers"]["Authorization"] == "Bearer sekrit"
    assert "dots.mocr=up" in out and "docling=down" in out
    assert "whisper-turbo=up" in out


def test_doctor_quiet_skip_without_url(monkeypatch, capsys):
    """No FRISKET_MODELS_URL → a quiet note, never a probe, never a
    failure."""
    import frisket.cli as cli

    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    assert cli.doctor_cmd() == 0
    out = capsys.readouterr().out
    assert "FRISKET_MODELS_URL unset" in out


# ---------------------------------------------------------------------------
# action catalog sidecar metadata


def _catalog_hints(client):
    response = client.get("/api/actions/v1/catalog")
    return response, {
        entry["kind"]: entry.get("ui_hints") or {}
        for entry in response.json()["actions"]
    }


def test_recipe_catalog_surfaces_sidecar_engine_availability(tmp_path, monkeypatch):
    """The v1 action catalog should include media engine availability from
    /capabilities for the UI picker."""
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    captured = {}

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        captured.update(
            url=url,
            headers=headers,
            timeout=timeout,
            follow_redirects=follow_redirects,
        )
        return httpx.Response(
            200,
            json={
                "engines": [
                    {"name": "dots.mocr", "route": "/ocr", "available": True},
                    {
                        "name": "docling",
                        "route": "/to-markdown",
                        "available": False,
                        "error": "docling extra missing",
                    },
                    {
                        "name": "whisper-turbo",
                        "route": "/v1/transcribe",
                        "available": True,
                        "models": [TURBO_MODEL_ID],
                        "contract_versions": ["frisket.transcription.v1"],
                    },
                ]
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = TestClient(create_app(tmp_path / "ws"))
    _, hints = _catalog_hints(client)

    assert captured["url"] == "http://models:8500/capabilities"
    assert captured["headers"]["Authorization"] == "Bearer sekrit"
    assert captured["follow_redirects"] is True
    # Keep an unreachable gateway on the old short connect budget, while the
    # read interval exceeds the gateway's five-second remote-worker probe.
    assert captured["timeout"].connect == 2.0
    assert captured["timeout"].read == 10.0
    assert captured["timeout"].write == 2.0
    assert captured["timeout"].pool == 2.0

    ocr_engines = {e["id"]: e for e in hints["media.ocr"]["engines"]}
    assert ocr_engines["rapidocr"]["tier"] == "local"
    assert ocr_engines["dots.mocr"]["tier"] == "sidecar"
    assert ocr_engines["dots.mocr"]["available"] is True

    convert_engines = {e["id"]: e for e in hints["media.to_markdown"]["engines"]}
    assert convert_engines["markitdown"]["tier"] == "local"
    assert convert_engines["docling"]["available"] is False
    assert convert_engines["docling"]["error"] == "docling extra missing"

    transcribe_engines = {e["id"]: e for e in hints["media.transcribe"]["engines"]}
    gateway = _gateway_target(hints)
    assert gateway["available"] is True
    assert gateway["models"] == [TURBO_MODEL_ID]
    assert transcribe_engines["openai/whisper-1"]["tier"] == "hosted"
    assert "remote" not in transcribe_engines  # deleted at the engine-roster cutover


def _gateway_target(hints, action_kind: str = "media.transcribe"):
    """Return the named Whisper Turbo model's sole gateway target."""
    engines = {e["id"]: e for e in hints[action_kind]["engines"]}
    return {t["target"]: t for t in engines["whisper-turbo"]["targets"]}[
        "models-gateway"
    ]


def test_recipe_catalog_missing_sidecar_token_disables_quality_tiers(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "   ")

    client = TestClient(create_app(tmp_path / "ws"))
    _, hints = _catalog_hints(client)

    for action_kind, sidecar_id in {
        "media.ocr": "dots.mocr",
        "media.to_markdown": "docling",
    }.items():
        engines = {e["id"]: e for e in hints[action_kind]["engines"]}
        assert engines[sidecar_id]["available"] is False
        assert "FRISKET_MODELS_TOKEN" in engines[sidecar_id]["error"]
    gateway = _gateway_target(hints)
    assert gateway["available"] is False
    assert "FRISKET_MODELS_TOKEN" in gateway["error"]


@requires_ocr_runtime
def test_recipe_catalog_unconfigured_sidecar_keeps_local_engines(tmp_path, monkeypatch):
    """The only test in this file that asserts real RapidOCR availability
    (`ocr["rapidocr"]["available"] is True`, via
    frisket.server.action_catalog_hints -> rapidocr_available()) -- every
    other test here exercises the sidecar/gateway wiring only, not the local
    RapidOCR runtime."""
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)

    client = TestClient(create_app(tmp_path / "ws"))
    _, hints = _catalog_hints(client)

    ocr = {e["id"]: e for e in hints["media.ocr"]["engines"]}
    assert ocr["rapidocr"]["available"] is True
    assert ocr["dots.mocr"]["available"] is False
    assert "FRISKET_MODELS_URL" in ocr["dots.mocr"]["error"]

    convert = {e["id"]: e for e in hints["media.to_markdown"]["engines"]}
    assert convert["markitdown"]["available"] is True
    assert convert["docling"]["available"] is False
    assert "FRISKET_MODELS_URL" in convert["docling"]["error"]

    # The collapsed ``faster_whisper`` stays available (local preferred
    # target) while its gateway target honestly reports the missing config.
    transcribe = {e["id"]: e for e in hints["media.transcribe"]["engines"]}
    assert transcribe["faster_whisper"]["available"] is True
    gateway = _gateway_target(hints)
    assert gateway["available"] is False
    assert "FRISKET_MODELS_URL" in gateway["error"]


def test_recipe_catalog_sidecar_probe_failure_is_nonfatal(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        raise httpx.TimeoutException("slow sidecar")

    monkeypatch.setattr(httpx, "get", fake_get)
    client = TestClient(create_app(tmp_path / "ws"))
    response, hints = _catalog_hints(client)

    assert response.status_code == 200
    transcribe = {e["id"]: e for e in hints["media.transcribe"]["engines"]}
    assert transcribe["faster_whisper"]["available"] is True
    gateway = _gateway_target(hints)
    assert gateway["available"] is False
    assert "TimeoutException" in gateway["error"]


def test_sidecar_capability_probe_only_classifies_read_timeouts_as_transient(
    monkeypatch,
):
    from frisket.ops._sidecar import probe_sidecar_capabilities

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("worker is cold")

    monkeypatch.setattr(httpx, "get", timeout)
    cold = probe_sidecar_capabilities(
        base="http://models:8500",
        token="sekrit",
        timeout=1.0,
    )
    assert cold["transient_failure"] is True

    for connection_failure in (
        httpx.ConnectTimeout("gateway connection timed out"),
        httpx.ConnectError("gateway is unreachable"),
    ):

        def unreachable(*args, _failure=connection_failure, **kwargs):
            raise _failure

        monkeypatch.setattr(httpx, "get", unreachable)
        failed = probe_sidecar_capabilities(
            base="http://models:8500",
            token="sekrit",
            timeout=1.0,
        )
        assert "transient_failure" not in failed


@pytest.mark.parametrize("edition", ["solo", "team"])
def test_recipe_catalog_reuses_sidecar_probe_across_global_and_project_routes(
    tmp_path, monkeypatch, edition
):
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    calls = []

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        calls.append(url)
        return httpx.Response(
            200,
            json={"engines": []},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = TestClient(create_app(tmp_path / "ws", edition=edition))
    project_id = client.post("/api/projects", json={"name": "Cached catalog"}).json()[
        "id"
    ]

    assert client.get("/api/actions/v1/catalog").status_code == 200
    assert (
        client.get(f"/api/projects/{project_id}/actions/v1/catalog").status_code == 200
    )
    assert calls == ["http://models:8500/capabilities"]


def test_cloud_catalog_uses_composition_without_probing_sidecar(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")

    def unexpected_get(*_args, **_kwargs):
        raise AssertionError("Cloud catalog must not wake the models gateway")

    monkeypatch.setattr(httpx, "get", unexpected_get)
    client = TestClient(create_app(tmp_path / "ws", edition="cloud"))
    project_id = client.post("/api/projects", json={"name": "Cold catalog"}).json()[
        "id"
    ]

    assert client.get("/api/actions/v1/catalog").status_code == 200
    assert (
        client.get(f"/api/projects/{project_id}/actions/v1/catalog").status_code == 200
    )


def test_recipe_catalog_sidecar_probe_cache_expires_deterministically(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from frisket.server.app import (
        SIDECAR_CAPABILITIES_CACHE_TTL_SECONDS,
        create_app,
    )

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    calls = 0
    now = [100.0]

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "engines": [
                    {
                        "name": "whisper-turbo",
                        "route": "/v1/transcribe",
                        "available": calls == 1,
                        "contract_versions": ["frisket.transcription.v1"],
                    }
                ]
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    app = create_app(tmp_path / "ws")
    app.state.sidecar_capabilities_cache._clock = lambda: now[0]
    client = TestClient(app)

    _, first_hints = _catalog_hints(client)
    _, cached_hints = _catalog_hints(client)
    assert calls == 1
    assert _gateway_target(first_hints)["available"] is True
    assert _gateway_target(cached_hints)["available"] is True

    now[0] += SIDECAR_CAPABILITIES_CACHE_TTL_SECONDS
    _, refreshed_hints = _catalog_hints(client)
    assert calls == 2
    assert _gateway_target(refreshed_hints)["available"] is False


def test_recipe_catalog_sidecar_probe_cache_key_tracks_url_and_token(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models-a:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "token-a")
    calls = []

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        calls.append((url, headers["Authorization"]))
        return httpx.Response(
            200,
            json={"engines": []},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = TestClient(create_app(tmp_path / "ws"))

    assert client.get("/api/actions/v1/catalog").status_code == 200
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "token-b")
    assert client.get("/api/actions/v1/catalog").status_code == 200
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models-b:8500")
    assert client.get("/api/actions/v1/catalog").status_code == 200

    assert calls == [
        ("http://models-a:8500/capabilities", "Bearer token-a"),
        ("http://models-a:8500/capabilities", "Bearer token-b"),
        ("http://models-b:8500/capabilities", "Bearer token-b"),
    ]


def test_recipe_catalog_caches_sidecar_probe_failures(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    calls = 0

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.TimeoutException("slow sidecar")
        return httpx.Response(
            200,
            json={"engines": []},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    client = TestClient(create_app(tmp_path / "ws"))

    first_response, first_hints = _catalog_hints(client)
    second_response, second_hints = _catalog_hints(client)

    assert first_response.status_code == second_response.status_code == 200
    assert calls == 1
    for hints in (first_hints, second_hints):
        gateway = _gateway_target(hints)
        assert gateway["available"] is False
        assert "TimeoutException" in gateway["error"]


def test_sidecar_capabilities_cache_coalesces_concurrent_misses():
    from frisket.server.app import _SidecarCapabilitiesCache

    workers = 6

    class InstrumentedLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._guard = threading.Lock()
            self._waiters = 0
            self.all_followers_waiting = threading.Event()

        def __enter__(self):
            with self._guard:
                acquired = self._lock.acquire(blocking=False)
                if not acquired:
                    self._waiters += 1
                    if self._waiters == workers - 1:
                        self.all_followers_waiting.set()
            if not acquired:
                self._lock.acquire()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self._lock.release()

    cache = _SidecarCapabilitiesCache(ttl_seconds=5.0, clock=lambda: 0.0)
    gate = InstrumentedLock()
    cache._lock = gate
    loads = 0
    expected = {
        "configured": True,
        "available": True,
        "engines": [],
        "error": None,
    }

    def load():
        nonlocal loads
        loads += 1
        assert gate.all_followers_waiting.wait(timeout=2.0)
        return expected

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(cache.get, ("http://models:8500", "sekrit"), load)
            for _ in range(workers)
        ]
        results = [future.result(timeout=2.0) for future in futures]

    assert loads == 1
    assert results == [expected] * workers


# ---------------------------------------------------------------------------
# compose 'models' profile parses


def test_compose_config_parses_with_dummy_env():
    """The repo-root docker-compose.yml parses (with the models profile
    enabled) given a dummy FRISKET_MODELS_TOKEN — the sidecar service's
    required-token interpolation resolves and the file is valid."""
    if not _which("docker"):
        pytest.skip("docker CLI not available")
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        ["docker", "compose", "--profile", "models", "config", "--quiet"],
        cwd=root,
        env={
            "PATH": _env_path(),
            "FRISKET_BUILD_SHA": "0" * 40,
            "FRISKET_MODELS_TOKEN": "dummy-secret",
            "FRISKET_MODELS_URL": "http://models:8500",
            "FRISKET_DATABASE_ADMIN_URL": "postgresql://dummy:dummy@db:5432/postgres",
            "FRISKET_RUN_QUEUE_DATABASE_URL": "postgresql://dummy:dummy@db:5432/frisket",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


def _env_path() -> str:
    import os

    return os.environ.get("PATH", "")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
