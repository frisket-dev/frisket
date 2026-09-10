"""Strict app-owned transcription v1 contract models.

The sidecar is a separately packaged service, so the product cannot import its
Python models.  These closed models deliberately duplicate Boundary A and make
contract drift a loud validation failure instead of allowing a partial legacy
response to pass through the recipe.

This is a live consumer contract. ``TranscriptionV1Adapter`` uses it for the gateway
multipart metadata and response, while local Faster Whisper uses the same
options and response envelope across its sandboxed subprocess boundary. The
local request adds only ``audio_path`` because the file is already
materialized inside the operator trust boundary.

``sidecar/tests/test_contract_mirror_parity.py`` directly compares this
module's declared schemas against the sidecar source of truth
(``sidecar/src/frisket_models/transcription/contract.py``) — field sets,
requiredness, defaults, constraints, and model config — and pins the exact
wire literals. Focused contract and client tests own executable behavior.

The ``options.context`` field is "non-empty, at most
``TRANSCRIPTION_CONTEXT_MAX_CHARS`` (500) characters" so the
  wire bound matches the app-side product cap
  (``MAX_TRANSCRIBE_CONTEXT_CHARS`` in
  ``frisket/contracts/actions/schemas/media.py``, which now imports the
  bound from here) and the web input. Mirrored in the sidecar source of
  truth (``sidecar/src/frisket_models/transcription/contract.py``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)


TRANSCRIPTION_CONTRACT_VERSION = "frisket.transcription.v1"
TRANSCRIPTION_ENDPOINT = "/v1/transcribe"

# One contract, three surfaces: this wire bound is THE source for the app-side
# product cap (``MAX_TRANSCRIBE_CONTEXT_CHARS`` in
# ``frisket/contracts/actions/schemas/media.py`` imports it) and is mirrored
# by the web form's maxLength and by the sidecar copy's literal 500
# (``sidecar/src/frisket_models/transcription/contract.py`` — mirrored
# copies, same value, parity-tested).
TRANSCRIPTION_CONTEXT_MAX_CHARS = 500

NonnegativeFiniteFloat = Annotated[
    float,
    Field(ge=0, allow_inf_nan=False),
]
NonemptyString = Annotated[str, Field(min_length=1)]
BoundedContextString = Annotated[
    str,
    Field(min_length=1, max_length=TRANSCRIPTION_CONTEXT_MAX_CHARS),
]
AcceptedOptionScalar = str | bool | int

# Mirrors of the sidecar's descriptor vocabulary.  The source of truth uses
# StrEnums opted into string parsing (``Field(strict=False)``); the app copy
# uses string Literals, which accept and emit the identical wire strings.
TranscriptionDiarizationMode = Literal["none", "optional", "intrinsic"]
TranscriptionSpeakerHint = Literal["none", "count"]
TranscriptionRuntimeImageId = Annotated[
    str,
    Field(pattern=r"^(?:oci:sha256:[0-9a-f]{64}|modal:im-[A-Za-z0-9]+)$"),
]


class TranscriptionWireModel(BaseModel):
    """Closed, strict JSON object at transcription Boundary A."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TranscriptionOptions(TranscriptionWireModel):
    """Options currently understood by the v1 gateway.

    Which fields an engine accepts remains descriptor-driven.  The app emits
    only controls its selected product engine declares; this model prevents an
    accidental model-specific key from leaking onto the shared wire.
    """

    language: NonemptyString | None = None
    model_size: NonemptyString | None = None
    diarize: bool | None = None
    num_speakers: int | None = Field(default=None, ge=1)
    min_speakers: int | None = Field(default=None, ge=1)
    max_speakers: int | None = Field(default=None, ge=1)
    vad: bool | None = None
    context: BoundedContextString | None = None

    @model_validator(mode="after")
    def _speaker_count_shape(self) -> "TranscriptionOptions":
        if self.num_speakers is not None and (
            self.min_speakers is not None or self.max_speakers is not None
        ):
            raise ValueError(
                "num_speakers cannot be combined with min_speakers or max_speakers"
            )
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError("min_speakers cannot exceed max_speakers")
        if (
            self.num_speakers is not None
            or self.min_speakers is not None
            or self.max_speakers is not None
        ) and self.diarize is not True:
            raise ValueError("speaker-count hints require diarize=true")
        return self


class LocalFasterWhisperRequest(TranscriptionWireModel):
    """Private-subprocess materialization of the v1 request metadata.

    The gateway carries audio as multipart bytes; the sandboxed local worker
    receives the already-materialized absolute path instead.  Everything else
    is the same closed v1 request vocabulary, including the contract literal,
    hyphenated wire engine name, and :class:`TranscriptionOptions`.
    """

    contract_version: Literal[TRANSCRIPTION_CONTRACT_VERSION]
    engine: Literal["faster-whisper"]
    audio_path: NonemptyString
    options: TranscriptionOptions


class TranscriptionOptionSupport(TranscriptionWireModel):
    """Per-engine option-support declarations; no engine inherits Whisper controls.

    Deliberate mirror of ``frisket_models.transcription.contract
    .TranscriptionOptionSupport`` (the sidecar package's source of truth) —
    the app cannot import ``frisket_models``, so drift is caught by
    ``sidecar/tests/test_contract_mirror_parity.py`` (schema-level,
    field-by-field — golden payloads alone provably miss added optional
    fields); the shared golden fixture in
    ``sidecar/tests/fixtures/transcription.golden.json`` provides example
    round-trip parity only.  The sidecar's runtime helper (``validate_options``) is gateway/
    worker behavior and is not mirrored, matching how the app copy of the
    options model omits the sidecar's ``supplied_options`` helpers.
    """

    diarization_mode: TranscriptionDiarizationMode
    speaker_hint: TranscriptionSpeakerHint
    language: bool
    model_size: bool
    vad: bool
    context: bool
    clean: bool = False

    @model_validator(mode="after")
    def _diarization_declaration(self) -> "TranscriptionOptionSupport":
        if self.diarization_mode != "optional" and self.speaker_hint != "none":
            raise ValueError(
                "speaker_hint must be 'none' unless diarization_mode is 'optional'"
            )
        return self


class TranscriptionEngineDescriptor(TranscriptionWireModel):
    """Immutable engine identity plus its independently declared controls.

    Deliberate mirror of ``frisket_models.transcription.contract
    .TranscriptionEngineDescriptor``; same parity discipline as
    ``TranscriptionOptionSupport`` above (schema-level mirror parity test;
    golden fixture for round-trip examples only).  Mirror-faithfulness note:
    ``runtime_image_id`` keeps the sidecar's ``None`` default — a model leaf can
    register before the worker runtime injects its image identity, while live
    capabilities must populate it.  The sidecar's runtime helpers
    (``validate_options``/``validate_result``) are not mirrored.
    """

    engine: NonemptyString
    model_ids: list[NonemptyString] = Field(min_length=1)
    revision: NonemptyString
    runtime_image_id: TranscriptionRuntimeImageId | None = None
    options: TranscriptionOptionSupport


class TranscriptionWord(TranscriptionWireModel):
    word: NonemptyString
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _ordered(self) -> "TranscriptionWord":
        if self.end < self.start:
            raise ValueError("word end must be greater than or equal to start")
        return self


class TranscriptionSegment(TranscriptionWireModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    text: str
    speaker: NonemptyString | None = None
    speaker_confidence: NonemptyString | None = None
    words: list[TranscriptionWord] | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "TranscriptionSegment":
        if self.end < self.start:
            raise ValueError("segment end must be greater than or equal to start")
        if self.speaker_confidence is not None and self.speaker is None:
            raise ValueError("speaker_confidence requires a speaker label")
        return self


class TranscriptionResult(TranscriptionWireModel):
    engine: NonemptyString
    text: str
    segments: list[TranscriptionSegment]
    language: NonemptyString | None = None
    duration: NonnegativeFiniteFloat | None = None
    model_ids: list[NonemptyString] = Field(min_length=1)
    revision: NonemptyString
    device: NonemptyString
    dtype: NonemptyString
    timings: dict[NonemptyString, NonnegativeFiniteFloat]
    warnings: list[NonemptyString]
    accepted_options: dict[str, AcceptedOptionScalar]

    @field_validator("accepted_options", mode="before")
    @classmethod
    def _accepted_options_match_request(
        cls, value: object
    ) -> dict[str, AcceptedOptionScalar]:
        if not isinstance(value, dict):
            raise ValueError("accepted_options must be an object")
        if any(item is None for item in value.values()):
            raise ValueError("accepted_options cannot contain null values")
        options = TranscriptionOptions.model_validate(value)
        return options.model_dump(mode="python", exclude_none=True)

    @model_validator(mode="after")
    def _segments_are_start_sorted(self) -> "TranscriptionResult":
        starts = [segment.start for segment in self.segments]
        if starts != sorted(starts):
            raise ValueError("segments must be sorted by nondecreasing start")
        return self


class TranscriptionResponse(TranscriptionWireModel):
    contract_version: Literal[TRANSCRIPTION_CONTRACT_VERSION]
    results: list[TranscriptionResult] = Field(min_length=1)


class TranscriptionError(TranscriptionWireModel):
    code: NonemptyString
    message: NonemptyString
    retryable: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class TranscriptionErrorEnvelope(TranscriptionWireModel):
    contract_version: Literal[TRANSCRIPTION_CONTRACT_VERSION]
    error: TranscriptionError


def parse_transcription_response(body: Any) -> TranscriptionResponse:
    """Validate a success envelope, surfacing a structured v1 error cleanly."""

    try:
        error_envelope = TranscriptionErrorEnvelope.model_validate(body)
    except ValidationError:
        error_envelope = None
    if error_envelope is not None:
        error = error_envelope.error
        raise RuntimeError(
            f"sidecar transcription error ({error.code}): {error.message}"
        )
    try:
        return TranscriptionResponse.model_validate(body)
    except ValidationError as error:
        raise RuntimeError(
            f"malformed sidecar transcription v1 response: {error}"
        ) from None
