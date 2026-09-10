"""Golden and negative conformance tests for transcription contract v1."""

from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from frisket_models.transcription import (
    CONTRACT_VERSION,
    AdapterRegistration,
    DiarizationMode,
    EngineProbe,
    GatewayTranscriptionResponse,
    SpeakerHint,
    TranscribeOptions,
    TranscribeResult,
    TranscriptionEngineDescriptor,
    TranscriptionErrorEnvelope,
    TranscriptionOptionSupport,
    WorkerCapabilities,
    WorkerTranscriptionResponse,
)

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "transcription.golden.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    # rule19: versioned wire fixture is test data shared across two packages
    return json.loads(GOLDEN_PATH.read_text())


def test_golden_descriptor_options_result_and_error_round_trip_exact(golden):
    descriptor = TranscriptionEngineDescriptor.model_validate(golden["descriptor"])
    options = TranscribeOptions.model_validate(golden["options"])
    result = TranscribeResult.model_validate(golden["result"])
    error = TranscriptionErrorEnvelope.model_validate(golden["error"])

    assert descriptor.model_dump(mode="json") == golden["descriptor"]
    assert options.model_dump(mode="json", exclude_none=True) == golden["options"]
    assert result.model_dump(mode="json") == golden["result"]
    assert error.model_dump(mode="json") == golden["error"]
    assert descriptor.validate_options(options) == result.accepted_options


def test_golden_optional_count_descriptor_round_trips_exact(golden):
    """The app package mirrors the descriptor types (deliberate copies in
    frisket.contracts.transcription_sidecar); this second descriptor pins
    the optional+count branch and the runtime_image_id=None default on both sides
    of the shared-golden parity seam."""
    payload = golden["descriptor_optional_count"]
    descriptor = TranscriptionEngineDescriptor.model_validate(payload)
    assert descriptor.model_dump(mode="json") == payload
    assert descriptor.runtime_image_id is None
    assert descriptor.options.diarization_mode is DiarizationMode.OPTIONAL
    assert descriptor.options.speaker_hint is SpeakerHint.COUNT


def test_success_and_capability_envelopes_are_closed_and_versioned(golden):
    descriptor = TranscriptionEngineDescriptor.model_validate(golden["descriptor"])
    result = TranscribeResult.model_validate(golden["result"])
    probe = EngineProbe(available=True, loaded=False, error=None)

    gateway = GatewayTranscriptionResponse(
        contract_version=CONTRACT_VERSION, results=[result]
    )
    worker = WorkerTranscriptionResponse(
        contract_version=CONTRACT_VERSION, result=result
    )
    capabilities = WorkerCapabilities(
        contract_version=CONTRACT_VERSION,
        descriptor=descriptor,
        probe=probe,
    )

    assert gateway.model_dump(mode="json") == {
        "contract_version": CONTRACT_VERSION,
        "results": [golden["result"]],
    }
    assert worker.model_dump(mode="json") == {
        "contract_version": CONTRACT_VERSION,
        "result": golden["result"],
    }
    assert capabilities.model_dump(mode="json") == {
        "contract_version": CONTRACT_VERSION,
        "descriptor": golden["descriptor"],
        "probe": {"available": True, "loaded": False, "error": None},
    }

    with pytest.raises(ValidationError):
        WorkerTranscriptionResponse.model_validate(
            {
                "contract_version": "frisket.transcription.v2",
                "result": golden["result"],
            }
        )
    with pytest.raises(ValidationError):
        GatewayTranscriptionResponse.model_validate(
            {
                "contract_version": CONTRACT_VERSION,
                "results": [golden["result"]],
                "future_field": True,
            }
        )
    with pytest.raises(ValidationError):
        GatewayTranscriptionResponse(results=[])
    with pytest.raises(ValidationError):
        WorkerTranscriptionResponse.model_validate({"result": golden["result"]})


@pytest.mark.parametrize(
    "runtime_image_id",
    [
        "oci:sha256:" + ("a" * 64),
        "modal:im-Abc123",
    ],
)
def test_runtime_image_id_accepts_exact_supported_namespaces(runtime_image_id):
    descriptor = TranscriptionEngineDescriptor.model_validate(
        {
            **json.loads(GOLDEN_PATH.read_text())["descriptor"],
            "runtime_image_id": runtime_image_id,
        }
    )
    assert descriptor.runtime_image_id == runtime_image_id


@pytest.mark.parametrize(
    "runtime_image_id",
    [
        "sha256:" + ("a" * 64),
        "oci:sha256:not-a-digest",
        "modal:Abc123",
        "modal:im-",
    ],
)
def test_probe_and_runtime_image_id_reject_invalid_identity(runtime_image_id):
    with pytest.raises(ValidationError, match="available engine probe"):
        EngineProbe(available=True, loaded=False, error="server is down")
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        TranscriptionEngineDescriptor(
            engine="fixture",
            model_ids=["example/model"],
            revision="pinned",
            runtime_image_id=runtime_image_id,
            options=TranscriptionOptionSupport(
                diarization_mode=DiarizationMode.NONE,
                speaker_hint=SpeakerHint.NONE,
                language=False,
                model_size=False,
                vad=False,
                context=False,
            ),
        )


def test_legacy_image_digest_field_is_not_accepted(golden):
    payload = dict(golden["descriptor"])
    payload["image_digest"] = payload.pop("runtime_image_id")

    with pytest.raises(ValidationError, match="extra_forbidden"):
        TranscriptionEngineDescriptor.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"num_speakers": 2, "min_speakers": 1},
        {"num_speakers": 2, "max_speakers": 3},
        {"min_speakers": 4, "max_speakers": 2},
        {"num_speakers": 0},
        {"num_speakers": 2},
        {"diarize": False, "min_speakers": 1, "max_speakers": 3},
        {"vad": 1},
        {"future_knob": True},
        # `context` wire bound (mirrors the app copy's
        # TRANSCRIPTION_CONTEXT_MAX_CHARS = 500): empty and over-cap both
        # fail at the wire, not just in the app's product validation.
        {"context": ""},
        {"context": "x" * 501},
    ],
)
def test_options_reject_invalid_shapes_and_coercion(payload):
    with pytest.raises(ValidationError):
        TranscribeOptions.model_validate(payload)


def test_context_wire_bound_accepts_exactly_the_cap():
    # 500 is the shared bound across the wire contract, the app-side product
    # cap (MAX_TRANSCRIBE_CONTEXT_CHARS), and the web input's maxLength.
    at_cap = "x" * 500
    assert TranscribeOptions.model_validate({"context": at_cap}).context == at_cap


def test_supplied_false_is_not_treated_as_an_omitted_option():
    options = TranscribeOptions(diarize=False, vad=False)
    assert [item.value for item in options.supplied_option_names()] == [
        "diarize",
        "vad",
    ]
    assert options.model_dump(mode="json", exclude_none=True) == {
        "diarize": False,
        "vad": False,
    }


@pytest.mark.parametrize("mode", [DiarizationMode.NONE, DiarizationMode.INTRINSIC])
@pytest.mark.parametrize(
    "payload",
    [
        {"diarize": False},
        {"diarize": True},
        {"diarize": True, "num_speakers": 2},
        {"diarize": True, "min_speakers": 1, "max_speakers": 3},
    ],
)
def test_none_and_intrinsic_modes_reject_all_diarization_controls(mode, payload):
    support = TranscriptionOptionSupport(
        diarization_mode=mode,
        speaker_hint=SpeakerHint.NONE,
        language=False,
        model_size=False,
        vad=False,
        context=False,
    )
    with pytest.raises(ValueError, match="unsupported transcription option"):
        support.validate_options(TranscribeOptions.model_validate(payload))


def test_optional_mode_accepts_diarize_and_only_declared_count_hints():
    no_count = TranscriptionOptionSupport(
        diarization_mode=DiarizationMode.OPTIONAL,
        speaker_hint=SpeakerHint.NONE,
        language=False,
        model_size=False,
        vad=False,
        context=False,
    )
    assert no_count.validate_options(TranscribeOptions(diarize=True)) == {
        "diarize": True
    }
    with pytest.raises(ValueError, match="num_speakers"):
        no_count.validate_options(TranscribeOptions(diarize=True, num_speakers=2))

    with_count = no_count.model_copy(update={"speaker_hint": SpeakerHint.COUNT})
    assert with_count.validate_options(
        TranscribeOptions(diarize=True, min_speakers=2, max_speakers=4)
    ) == {"diarize": True, "min_speakers": 2, "max_speakers": 4}


def test_descriptor_rejects_independently_unsupported_fields(golden):
    descriptor = TranscriptionEngineDescriptor.model_validate(golden["descriptor"])

    with pytest.raises(ValueError, match="vad"):
        descriptor.validate_options(TranscribeOptions(vad=True))
    with pytest.raises(ValueError, match="diarize"):
        descriptor.validate_options(TranscribeOptions(diarize=False))


def test_descriptor_rejects_speaker_data_when_diarization_is_not_declared(golden):
    result = TranscribeResult.model_validate(golden["result"])
    descriptor = TranscriptionEngineDescriptor.model_validate(
        {
            **golden["descriptor"],
            "options": {
                "diarization_mode": "none",
                "speaker_hint": "none",
                "language": True,
                "model_size": False,
                "vad": False,
                "context": True,
            },
        }
    )
    with pytest.raises(ValueError, match="undeclared speaker data"):
        descriptor.validate_result(result, TranscribeOptions(language="en"))


def test_invalid_speaker_hint_declaration_is_rejected():
    with pytest.raises(ValidationError, match="speaker_hint"):
        TranscriptionOptionSupport(
            diarization_mode=DiarizationMode.INTRINSIC,
            speaker_hint=SpeakerHint.COUNT,
            language=False,
            model_size=False,
            vad=False,
            context=False,
        )


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        (("segments", 0, "start"), -0.1),
        (("segments", 0, "end"), math.inf),
        (("duration",), math.nan),
        (("timings", "total_seconds"), -1.0),
    ],
)
def test_result_rejects_invalid_finite_nonnegative_values(golden, target, replacement):
    payload = json.loads(json.dumps(golden["result"]))
    cursor = payload
    for key in target[:-1]:
        cursor = cursor[key]
    cursor[target[-1]] = replacement
    with pytest.raises(ValidationError):
        TranscribeResult.model_validate(payload)


def test_speaker_confidence_preserves_the_existing_marker_shape(golden):
    assert (
        TranscribeResult.model_validate(golden["result"]).segments[0].speaker_confidence
        == "approximate"
    )
    payload = json.loads(json.dumps(golden["result"]))
    payload["segments"][0]["speaker_confidence"] = 0.97
    with pytest.raises(ValidationError):
        TranscribeResult.model_validate(payload)


def test_result_allows_overlap_but_rejects_reverse_start_order(golden):
    result = TranscribeResult.model_validate(golden["result"])
    assert result.segments[1].start < result.segments[0].end

    payload = json.loads(json.dumps(golden["result"]))
    payload["segments"][1]["start"] = 0.9
    payload["segments"][0]["start"] = 1.0
    with pytest.raises(ValidationError, match="nondecreasing"):
        TranscribeResult.model_validate(payload)


def test_result_rejects_invalid_word_or_segment_intervals(golden):
    segment_payload = json.loads(json.dumps(golden["result"]))
    segment_payload["segments"][0]["start"] = 1.5
    segment_payload["segments"][0]["end"] = 1.4
    with pytest.raises(ValidationError, match="segment start"):
        TranscribeResult.model_validate(segment_payload)

    word_payload = json.loads(json.dumps(golden["result"]))
    word_payload["segments"][0]["words"][0]["start"] = 0.5
    word_payload["segments"][0]["words"][0]["end"] = 0.4
    with pytest.raises(ValidationError, match="word start"):
        TranscribeResult.model_validate(word_payload)


@pytest.mark.parametrize(
    "accepted_options",
    [
        {"unknown": True},
        {"language": 7},
        {"diarize": "true"},
        {"language": None},
        {"num_speakers": 2, "max_speakers": 3},
        {"num_speakers": 2},
    ],
)
def test_result_accepted_options_reuses_the_strict_request_schema(
    golden, accepted_options
):
    payload = json.loads(json.dumps(golden["result"]))
    payload["accepted_options"] = accepted_options
    with pytest.raises((ValidationError, ValueError)):
        TranscribeResult.model_validate(payload)


def test_error_envelope_is_structured_and_closed(golden):
    payload = json.loads(json.dumps(golden["error"]))
    payload["error"]["status"] = 400
    with pytest.raises(ValidationError):
        TranscriptionErrorEnvelope.model_validate(payload)


def test_registration_is_frozen_and_does_not_call_factory_or_probe(golden):
    descriptor = TranscriptionEngineDescriptor.model_validate(golden["descriptor"])
    calls: list[str] = []

    def factory():
        calls.append("factory")
        raise AssertionError("registration must remain lazy")

    def probe():
        calls.append("probe")
        return EngineProbe(available=True, loaded=False)

    registration = AdapterRegistration(
        descriptor=descriptor,
        factory=factory,
        probe=probe,
    )
    assert calls == []
    with pytest.raises(FrozenInstanceError):
        registration.factory = lambda: None
