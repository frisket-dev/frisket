"""Import-light transcription wire and adapter contract.

These strict types span app-to-gateway, gateway-to-worker, and worker-to-adapter
boundaries. Keep this module free of FastAPI and model runtimes. Tests pin it
against the app-side mirror, including the 500-character context bound.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypeAlias, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

CONTRACT_VERSION = "frisket.transcription.v1"
TRANSCRIBE_ENDPOINT = "/v1/transcribe"

ContractVersion: TypeAlias = Literal["frisket.transcription.v1"]
NonEmptyString: TypeAlias = Annotated[str, Field(min_length=1)]
NonNegativeFiniteFloat: TypeAlias = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
PositiveInteger: TypeAlias = Annotated[int, Field(gt=0)]
# Mirrored by the app contract and parity-tested.
BoundedContextString: TypeAlias = Annotated[str, Field(min_length=1, max_length=500)]
RuntimeImageId: TypeAlias = Annotated[
    str,
    Field(pattern=r"^(?:oci:sha256:[0-9a-f]{64}|modal:im-[A-Za-z0-9]+)$"),
]


class _StrictContractModel(BaseModel):
    """Strict, closed wire model."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DiarizationMode(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    INTRINSIC = "intrinsic"


class SpeakerHint(StrEnum):
    NONE = "none"
    COUNT = "count"


class TranscribeOptionName(StrEnum):
    LANGUAGE = "language"
    MODEL_SIZE = "model_size"
    DIARIZE = "diarize"
    NUM_SPEAKERS = "num_speakers"
    MIN_SPEAKERS = "min_speakers"
    MAX_SPEAKERS = "max_speakers"
    VAD = "vad"
    CONTEXT = "context"


# Accept wire enum strings while keeping scalar options strict.
DiarizationModeField: TypeAlias = Annotated[DiarizationMode, Field(strict=False)]
SpeakerHintField: TypeAlias = Annotated[SpeakerHint, Field(strict=False)]
OptionNameField: TypeAlias = Annotated[TranscribeOptionName, Field(strict=False)]
AcceptedOptionValue: TypeAlias = str | bool | int
AcceptedOptions: TypeAlias = dict[OptionNameField, AcceptedOptionValue]


class TranscribeOptions(_StrictContractModel):
    """Request options; ``None`` alone means not supplied."""

    language: NonEmptyString | None = None
    model_size: NonEmptyString | None = None
    diarize: bool | None = None
    num_speakers: PositiveInteger | None = None
    min_speakers: PositiveInteger | None = None
    max_speakers: PositiveInteger | None = None
    vad: bool | None = None
    context: BoundedContextString | None = None

    @model_validator(mode="after")
    def validate_speaker_count_shape(self) -> TranscribeOptions:
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

    def supplied_option_names(self) -> tuple[TranscribeOptionName, ...]:
        """Return supplied fields in canonical order."""

        return tuple(
            option
            for option in TranscribeOptionName
            if getattr(self, option.value) is not None
        )

    def supplied_options(self) -> AcceptedOptions:
        """Return receipt-safe supplied values."""

        values = self.model_dump(mode="python", exclude_none=True)
        return {TranscribeOptionName(name): value for name, value in values.items()}


class TranscriptionOptionSupport(_StrictContractModel):
    """Per-engine controls; none are inherited."""

    diarization_mode: DiarizationModeField
    speaker_hint: SpeakerHintField
    language: bool
    model_size: bool
    vad: bool
    context: bool
    clean: bool = False

    @model_validator(mode="after")
    def validate_diarization_declaration(self) -> TranscriptionOptionSupport:
        if (
            self.diarization_mode is not DiarizationMode.OPTIONAL
            and self.speaker_hint is not SpeakerHint.NONE
        ):
            raise ValueError(
                "speaker_hint must be 'none' unless diarization_mode is 'optional'"
            )
        return self

    def validate_options(self, options: TranscribeOptions) -> AcceptedOptions:
        """Return supported caller values verbatim; reject all others."""

        rejected: list[TranscribeOptionName] = []
        supplied = set(options.supplied_option_names())

        if TranscribeOptionName.LANGUAGE in supplied and not self.language:
            rejected.append(TranscribeOptionName.LANGUAGE)
        if TranscribeOptionName.MODEL_SIZE in supplied and not self.model_size:
            rejected.append(TranscribeOptionName.MODEL_SIZE)
        if TranscribeOptionName.VAD in supplied and not self.vad:
            rejected.append(TranscribeOptionName.VAD)
        if TranscribeOptionName.CONTEXT in supplied and not self.context:
            rejected.append(TranscribeOptionName.CONTEXT)

        diarization_fields = {
            TranscribeOptionName.DIARIZE,
            TranscribeOptionName.NUM_SPEAKERS,
            TranscribeOptionName.MIN_SPEAKERS,
            TranscribeOptionName.MAX_SPEAKERS,
        }
        if self.diarization_mode is not DiarizationMode.OPTIONAL:
            rejected.extend(
                option
                for option in TranscribeOptionName
                if option in supplied and option in diarization_fields
            )
        elif self.speaker_hint is SpeakerHint.NONE:
            count_fields = diarization_fields - {TranscribeOptionName.DIARIZE}
            rejected.extend(
                option
                for option in TranscribeOptionName
                if option in supplied and option in count_fields
            )

        if rejected:
            names = ", ".join(option.value for option in dict.fromkeys(rejected))
            raise ValueError(f"unsupported transcription option(s): {names}")
        return options.supplied_options()


class TranscriptionEngineDescriptor(_StrictContractModel):
    """Immutable engine identity and controls."""

    engine: NonEmptyString
    model_ids: Annotated[list[NonEmptyString], Field(min_length=1)]
    revision: NonEmptyString
    # Registration may precede runtime image-identity injection.
    runtime_image_id: RuntimeImageId | None = None
    options: TranscriptionOptionSupport

    def validate_options(self, options: TranscribeOptions) -> AcceptedOptions:
        try:
            return self.options.validate_options(options)
        except ValueError as exc:
            raise ValueError(f"engine {self.engine!r}: {exc}") from exc

    def validate_result(
        self,
        result: TranscribeResult,
        options: TranscribeOptions,
    ) -> TranscribeResult:
        """Validate engine-specific output semantics."""

        if result.engine != self.engine:
            raise ValueError(
                f"engine {self.engine!r} returned a result for {result.engine!r}"
            )
        if result.model_ids != self.model_ids or result.revision != self.revision:
            raise ValueError(f"engine {self.engine!r} returned unregistered provenance")

        diarization_required = (
            self.options.diarization_mode is DiarizationMode.INTRINSIC
            or (
                self.options.diarization_mode is DiarizationMode.OPTIONAL
                and options.diarize is True
            )
        )
        if diarization_required:
            missing = [
                index
                for index, segment in enumerate(result.segments)
                if not segment.speaker
            ]
            if missing:
                raise ValueError(
                    f"engine {self.engine!r} omitted speaker labels for segment(s): "
                    + ", ".join(str(index) for index in missing)
                )
        else:
            unexpected = [
                index
                for index, segment in enumerate(result.segments)
                if segment.speaker is not None or segment.speaker_confidence is not None
            ]
            if unexpected:
                raise ValueError(
                    f"engine {self.engine!r} returned undeclared speaker data for "
                    "segment(s): " + ", ".join(str(index) for index in unexpected)
                )
        return result


class TranscribeWord(_StrictContractModel):
    word: NonEmptyString
    start: NonNegativeFiniteFloat
    end: NonNegativeFiniteFloat

    @model_validator(mode="after")
    def validate_interval(self) -> TranscribeWord:
        if self.start > self.end:
            raise ValueError("word start cannot exceed end")
        return self


class TranscribeSegment(_StrictContractModel):
    start: NonNegativeFiniteFloat
    end: NonNegativeFiniteFloat
    text: str
    speaker: NonEmptyString | None = None
    # Confidence is qualitative; never fabricate a numeric value.
    speaker_confidence: NonEmptyString | None = None
    words: list[TranscribeWord] | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> TranscribeSegment:
        if self.start > self.end:
            raise ValueError("segment start cannot exceed end")
        if self.speaker_confidence is not None and self.speaker is None:
            raise ValueError("speaker_confidence requires a speaker label")
        return self


class TranscribeResult(_StrictContractModel):
    engine: NonEmptyString
    text: str
    segments: list[TranscribeSegment]
    language: NonEmptyString | None = None
    duration: NonNegativeFiniteFloat | None = None
    model_ids: Annotated[list[NonEmptyString], Field(min_length=1)]
    revision: NonEmptyString
    device: NonEmptyString
    dtype: NonEmptyString
    timings: dict[NonEmptyString, NonNegativeFiniteFloat]
    warnings: list[NonEmptyString]
    # Verbatim accepted request values, checked against the descriptor.
    accepted_options: AcceptedOptions

    @field_validator("accepted_options", mode="before")
    @classmethod
    def validate_accepted_option_values(cls, value: object) -> AcceptedOptions:
        if not isinstance(value, dict):
            raise ValueError("accepted_options must be an object")
        if any(item is None for item in value.values()):
            raise ValueError("accepted_options cannot contain null values")
        # Reuse request validation, including exclusive speaker-count shapes.
        options = TranscribeOptions.model_validate(value)
        return options.supplied_options()

    @model_validator(mode="after")
    def validate_segment_order(self) -> TranscribeResult:
        for previous, current in zip(self.segments, self.segments[1:]):
            if current.start < previous.start:
                raise ValueError("segments must be ordered by nondecreasing start time")
        return self


class GatewayTranscriptionResponse(_StrictContractModel):
    """Ordered results for gateway uploads."""

    contract_version: ContractVersion
    results: Annotated[list[TranscribeResult], Field(min_length=1)]


class WorkerTranscriptionResponse(_StrictContractModel):
    """Single worker result."""

    contract_version: ContractVersion
    result: TranscribeResult


class EngineProbe(_StrictContractModel):
    """State probe that must never cold-load a model."""

    available: bool
    loaded: bool
    error: str | None = None

    @model_validator(mode="after")
    def validate_availability_error(self) -> EngineProbe:
        if self.available and self.error is not None:
            raise ValueError("an available engine probe cannot also report an error")
        return self


class WorkerCapabilities(_StrictContractModel):
    contract_version: ContractVersion
    descriptor: TranscriptionEngineDescriptor
    probe: EngineProbe


class TranscriptionError(_StrictContractModel):
    code: NonEmptyString
    message: NonEmptyString
    retryable: bool
    details: dict[str, JsonValue] = Field(default_factory=dict)


class TranscriptionErrorEnvelope(_StrictContractModel):
    contract_version: ContractVersion
    error: TranscriptionError


class TranscriptionInputError(ValueError):
    """Caller-controlled audio/input error, never a runtime failure."""

    def __init__(
        self,
        message: str,
        *,
        too_large: bool = False,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        message = message.strip()
        if not message:
            raise ValueError("transcription input error message must not be empty")
        super().__init__(message)
        self.message = message
        self.status_code = 413 if too_large else 400
        self.code = "input_too_large" if too_large else "invalid_audio"
        self.details = dict(details or {})


@runtime_checkable
class TranscriptionAdapter(Protocol):
    """Model-specific leaf adapter."""

    def transcribe(
        self, audio_path: Path, options: TranscribeOptions
    ) -> TranscribeResult: ...


AdapterFactory: TypeAlias = Callable[[], TranscriptionAdapter]
EngineProbeCallable: TypeAlias = Callable[[], EngineProbe]


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    """Registration that must not import or load a model."""

    descriptor: TranscriptionEngineDescriptor
    factory: AdapterFactory
    probe: EngineProbeCallable

    def __post_init__(self) -> None:
        if not callable(self.factory):
            raise TypeError("factory must be callable")
        if not callable(self.probe):
            raise TypeError("probe must be callable")
