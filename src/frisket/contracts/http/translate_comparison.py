"""Typed JSON transport for the scratch translate-comparison preview."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, JsonValue, RootModel

from frisket.contracts.http.models import WireModel


TRANSLATE_COMPARISON_SCHEMA_VERSION = "frisket.translate_compare_preview.v1"


class TranslateComparisonRequest(WireModel):
    """JSON input owned jointly by the browser and scratch preview service."""

    model_config = ConfigDict(extra="ignore", strict=True)

    engines: list[str]
    text: str
    target_language: str = "English"
    language: list[str] | None = None


class TranslateComparisonCompatibilityRequest(RootModel[dict[str, JsonValue]]):
    """Lossless object fallback for service-owned legacy input normalization.

    The scratch service predates the public DTO and deliberately owns malformed
    object refusals. Non-object JSON remains FastAPI's ordinary 422 boundary.
    """

    model_config = ConfigDict(strict=True)


TranslateComparisonRequestBody = Annotated[
    TranslateComparisonRequest | TranslateComparisonCompatibilityRequest,
    Field(union_mode="left_to_right"),
]


class TranslateComparisonSource(WireModel):
    """The fixed scratch-source receipt; no persisted source is implied."""

    scratch: Literal[True]
    text_length: int


class TranslateComparisonEngineResult(WireModel):
    """The fixed result each current comparison producer emits."""

    engine: str
    translation: str
    detected_language: str | None
    runtime_ms: int
    errors: list[str]


class TranslateComparisonResponse(WireModel):
    """Closed comparison envelope with explicitly JSON-shaped extensions."""

    schema_version: Literal[TRANSLATE_COMPARISON_SCHEMA_VERSION]
    source: TranslateComparisonSource
    target_language: str
    engines: list[str]
    results: list[TranslateComparisonEngineResult]
    warnings: list[dict[str, JsonValue]]
    errors: list[dict[str, JsonValue]]


__all__ = [
    "TRANSLATE_COMPARISON_SCHEMA_VERSION",
    "TranslateComparisonCompatibilityRequest",
    "TranslateComparisonEngineResult",
    "TranslateComparisonRequest",
    "TranslateComparisonRequestBody",
    "TranslateComparisonResponse",
    "TranslateComparisonSource",
]
