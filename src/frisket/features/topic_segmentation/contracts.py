"""Small, engine-neutral contracts for dialogue topic segmentation.

Engines return source-addressable locators, not timestamps guessed from text.
The temporal locking layer is deliberately separate: it owns the policy that
turns these locators into source-bound ``timeline_ranges`` values.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from pydantic import JsonValue


DETAIL_LEVELS = ("fewer", "balanced", "more")
TOPIC_ANALYSIS_SIDECAR_COLUMN = "__topic_analysis"
TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION = "frisket.topic_segmentation.analysis.v1"
DetailLevel: TypeAlias = Literal["fewer", "balanced", "more"]
SourceKind: TypeAlias = Literal["timestamped_transcript", "untimed_transcript"]


class SegmentationSettingsError(ValueError):
    """An engine's settings do not match its closed settings contract."""


class SegmentationPreflightError(ValueError):
    """A transcript cannot be analyzed by the selected engine."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SegmentationCancelled(RuntimeError):
    """The caller cancelled segmentation before it completed."""


class SegmentationEngineUnavailable(RuntimeError):
    """A selected local engine or one of its private dependencies is absent."""


class SegmentationExecutionError(RuntimeError):
    """An available engine failed to produce a valid segmentation result."""


@dataclass(frozen=True, slots=True)
class DialogueUnit:
    id: str
    ordinal: int
    text: str
    speaker: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("dialogue unit id must be a non-empty string")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ValueError("dialogue unit ordinal must be an integer")
        if self.ordinal < 0:
            raise ValueError("dialogue unit ordinal must be non-negative")
        if not isinstance(self.text, str):
            raise ValueError("dialogue unit text must be a string")
        if self.speaker is not None and not isinstance(self.speaker, str):
            raise ValueError("dialogue unit speaker must be a string or null")
        for name, value in (("start_ms", self.start_ms), ("end_ms", self.end_ms)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"dialogue unit {name} must be a non-negative integer")
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError(
                "dialogue unit timing must provide both start_ms and end_ms"
            )
        if (
            self.start_ms is not None
            and self.end_ms is not None
            and self.start_ms >= self.end_ms
        ):
            raise ValueError("dialogue unit timing must satisfy start_ms < end_ms")


@dataclass(frozen=True, slots=True)
class SegmentationSnapshot:
    snapshot_hash: str
    source_kind: SourceKind
    language: str | None
    units: tuple[DialogueUnit, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_hash, str) or not self.snapshot_hash.strip():
            raise ValueError("segmentation snapshot_hash must be a non-empty string")
        if self.source_kind not in {
            "timestamped_transcript",
            "untimed_transcript",
        }:
            raise ValueError(f"unknown segmentation source_kind: {self.source_kind!r}")
        if self.language is not None and (
            not isinstance(self.language, str) or not self.language.strip()
        ):
            raise ValueError("segmentation language must be a non-empty string or null")
        if not isinstance(self.units, tuple):
            raise ValueError("segmentation units must be an immutable tuple")

        seen_ids: set[str] = set()
        previous_ordinal = -1
        previous_start = -1
        for unit in self.units:
            if not isinstance(unit, DialogueUnit):
                raise ValueError("segmentation units must contain DialogueUnit values")
            if unit.id in seen_ids:
                raise ValueError(f"duplicate dialogue unit id: {unit.id!r}")
            seen_ids.add(unit.id)
            if unit.ordinal <= previous_ordinal:
                raise ValueError("dialogue unit ordinals must be strictly increasing")
            previous_ordinal = unit.ordinal

            timed = unit.start_ms is not None and unit.end_ms is not None
            if self.source_kind == "timestamped_transcript" and not timed:
                raise ValueError("timestamped transcript units must all carry timing")
            if self.source_kind == "untimed_transcript" and timed:
                raise ValueError("untimed transcript units must not carry timing")
            if timed:
                assert unit.start_ms is not None
                if unit.start_ms < previous_start:
                    raise ValueError("dialogue unit start_ms values must be ordered")
                previous_start = unit.start_ms


@dataclass(frozen=True, slots=True)
class EngineDefinition:
    id: str
    version: str
    label: str
    description: str
    available: bool = True
    error: str | None = None
    recommended: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("id", self.id),
            ("version", self.version),
            ("label", self.label),
            ("description", self.description),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"engine {name} must be a non-empty string")
        if self.available and self.error is not None:
            raise ValueError("an available engine cannot carry an availability error")
        if not self.available and (self.error is None or not self.error.strip()):
            raise ValueError("an unavailable engine must explain why")


@dataclass(frozen=True, slots=True)
class PreflightResult:
    ok: bool
    error_code: str | None = None
    message: str | None = None
    details: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ok and (self.error_code is not None or self.message is not None):
            raise ValueError("successful preflight cannot carry an error")
        if not self.ok and (
            not isinstance(self.error_code, str)
            or not self.error_code.strip()
            or not isinstance(self.message, str)
            or not self.message.strip()
        ):
            raise ValueError("failed preflight requires error_code and message")

    @classmethod
    def passed(cls, **details: JsonValue) -> PreflightResult:
        return cls(ok=True, details=details)

    @classmethod
    def failed(
        cls,
        error_code: str,
        message: str,
        **details: JsonValue,
    ) -> PreflightResult:
        return cls(
            ok=False,
            error_code=error_code,
            message=message,
            details=details,
        )

    def require(self) -> None:
        if not self.ok:
            raise SegmentationPreflightError(
                self.error_code or "preflight_failed",
                self.message or "The selected engine cannot analyze this transcript.",
            )


@dataclass(frozen=True, slots=True)
class SegmentationContext:
    """Invocation-only context; algorithm dependencies stay engine-private."""

    cancelled: Callable[[], bool] | None = None

    def raise_if_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise SegmentationCancelled("topic segmentation was cancelled")


@dataclass(frozen=True, slots=True)
class BetweenUnits:
    left_unit_id: str
    right_unit_id: str
    kind: Literal["between_units"] = field(default="between_units", init=False)

    def __post_init__(self) -> None:
        if not self.left_unit_id.strip() or not self.right_unit_id.strip():
            raise ValueError("between_units locator requires both unit ids")
        if self.left_unit_id == self.right_unit_id:
            raise ValueError("between_units locator requires two different units")


@dataclass(frozen=True, slots=True)
class WithinUnit:
    unit_id: str
    kind: Literal["within_unit"] = field(default="within_unit", init=False)

    def __post_init__(self) -> None:
        if not self.unit_id.strip():
            raise ValueError("within_unit locator requires a unit id")


@dataclass(frozen=True, slots=True)
class ExactTime:
    at_ms: int
    kind: Literal["exact_time"] = field(default="exact_time", init=False)

    def __post_init__(self) -> None:
        if isinstance(self.at_ms, bool) or not isinstance(self.at_ms, int):
            raise ValueError("exact_time at_ms must be an integer")
        if self.at_ms < 0:
            raise ValueError("exact_time at_ms must be non-negative")


BoundaryLocator: TypeAlias = BetweenUnits | WithinUnit | ExactTime


@dataclass(frozen=True, slots=True)
class BoundaryCandidate:
    id: str
    locator: BoundaryLocator
    strength: float | None = None
    label: str | None = None
    diagnostics: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("boundary candidate id must be a non-empty string")
        if not isinstance(self.locator, (BetweenUnits, WithinUnit, ExactTime)):
            raise ValueError("boundary candidate has an unsupported locator")
        if self.strength is not None and (
            isinstance(self.strength, bool)
            or not isinstance(self.strength, (int, float))
            or not math.isfinite(float(self.strength))
        ):
            raise ValueError("boundary strength must be a finite number or null")
        if self.label is not None and not isinstance(self.label, str):
            raise ValueError("boundary label must be a string or null")


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    engine_id: str
    engine_version: str
    boundaries: tuple[BoundaryCandidate, ...]
    resolved_settings: Mapping[str, JsonValue]
    diagnostics: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.engine_id.strip() or not self.engine_version.strip():
            raise ValueError("segmentation result requires engine id and version")
        if not isinstance(self.boundaries, tuple):
            raise ValueError("segmentation boundaries must be an immutable tuple")
        ids = [candidate.id for candidate in self.boundaries]
        if len(ids) != len(set(ids)):
            raise ValueError("segmentation boundary ids must be unique")

    @property
    def candidates(self) -> tuple[BoundaryCandidate, ...]:
        """Readable alias for callers that discuss engine-native candidates."""

        return self.boundaries


def validate_detail_settings(
    settings: Mapping[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    """Validate the single shared V1 setting and materialize its default."""

    if settings is None:
        settings = {}
    if not isinstance(settings, Mapping):
        raise SegmentationSettingsError("topic segmentation settings must be an object")
    unknown = sorted(str(key) for key in settings if key != "detail")
    if unknown:
        raise SegmentationSettingsError(
            "unknown topic segmentation setting(s): " + ", ".join(unknown)
        )
    detail = settings.get("detail", "balanced")
    if detail not in DETAIL_LEVELS:
        raise SegmentationSettingsError(
            "detail must be one of: " + ", ".join(DETAIL_LEVELS)
        )
    return {"detail": detail}


@runtime_checkable
class DialogueSegmenter(Protocol):
    @property
    def catalog_definition(self) -> EngineDefinition: ...

    @property
    def definition(self) -> EngineDefinition: ...

    def validate_settings(
        self,
        settings: Mapping[str, JsonValue] | None,
    ) -> Mapping[str, JsonValue]: ...

    def preflight(
        self,
        snapshot: SegmentationSnapshot,
        settings: Mapping[str, JsonValue] | None,
    ) -> PreflightResult: ...

    def segment(
        self,
        snapshot: SegmentationSnapshot,
        settings: Mapping[str, JsonValue] | None,
        context: SegmentationContext,
    ) -> SegmentationResult: ...


__all__ = [
    "DETAIL_LEVELS",
    "TOPIC_ANALYSIS_SIDECAR_COLUMN",
    "TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION",
    "BetweenUnits",
    "BoundaryCandidate",
    "BoundaryLocator",
    "DialogueSegmenter",
    "DialogueUnit",
    "EngineDefinition",
    "ExactTime",
    "PreflightResult",
    "SegmentationCancelled",
    "SegmentationContext",
    "SegmentationEngineUnavailable",
    "SegmentationExecutionError",
    "SegmentationPreflightError",
    "SegmentationResult",
    "SegmentationSettingsError",
    "SegmentationSnapshot",
    "WithinUnit",
    "validate_detail_settings",
]
