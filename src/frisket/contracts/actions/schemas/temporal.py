"""Temporal extraction and segmentation action schemas.

The action contract deliberately distinguishes an unsaved panel draft from a
source-bound typed value.  Draft coordinates are bound to the selected source
timeline by the executor; typed values retain their own canonical anchor and
must pass contextual timeline validation before use.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, ValidationError, field_validator, model_validator

from frisket.contracts.actions.schemas._base import (
    ContractModel,
    StrictString,
    _is_json_value,
)
from frisket.contracts.actions.schemas._validators import (
    StrictInputRef,
)
from frisket.features.temporal_values import normalize_temporal_value


_TEMPORAL_TYPE_BY_SCHEMA_VERSION = {
    "frisket.timeline_point.v1": "timeline_point",
    "frisket.timeline_points.v1": "timeline_points",
    "frisket.timeline_range.v1": "timeline_range",
    "frisket.timeline_ranges.v1": "timeline_ranges",
}


def _is_embedded_duration_bounds_error(exc: ValidationError) -> bool:
    """Recognize typed coordinates that exceed their own declared timeline."""

    for error in exc.errors(include_url=False):
        message = str(error.get("msg", ""))
        context_error = str((error.get("ctx") or {}).get("error", ""))
        if "exceeds timeline duration_ms" in message or (
            "exceeds timeline duration_ms" in context_error
        ):
            return True
    return False


class TemporalDraftPoint(ContractModel):
    at_ms: int = Field(ge=0, strict=True)
    id: StrictString | None = None
    label: StrictString | None = None

    @field_validator("id", "label")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("invalid_temporal_value")
        return stripped


class TemporalDraftRange(ContractModel):
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    id: StrictString | None = None
    label: StrictString | None = None

    @field_validator("id", "label")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("invalid_temporal_value")
        return stripped

    @model_validator(mode="after")
    def _validate_interval(self) -> "TemporalDraftRange":
        if self.end_ms <= self.start_ms:
            raise ValueError("invalid_range")
        return self


class TemporalDraftRangeSelection(ContractModel):
    kind: Literal["draft_range"]
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    id: StrictString | None = None
    label: StrictString | None = None

    @field_validator("id", "label")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("invalid_temporal_value")
        return stripped

    @model_validator(mode="after")
    def _validate_interval(self) -> "TemporalDraftRangeSelection":
        if self.end_ms <= self.start_ms:
            raise ValueError("invalid_range")
        return self

    def as_item(self) -> TemporalDraftRange:
        return TemporalDraftRange(
            start_ms=self.start_ms,
            end_ms=self.end_ms,
            id=self.id,
            label=self.label,
        )


class TemporalDraftPointsSelection(ContractModel):
    kind: Literal["draft_points"]
    items: list[TemporalDraftPoint] = Field(default_factory=list)


class TemporalDraftRangesSelection(ContractModel):
    kind: Literal["draft_ranges"]
    items: list[TemporalDraftRange] = Field(default_factory=list)


class TemporalColumnSelection(ContractModel):
    kind: Literal["column"]
    column: StrictInputRef = Field(min_length=1)


class TemporalTypedValueSelection(ContractModel):
    kind: Literal["typed_value"]
    value: dict[str, Any]

    @field_validator("value")
    @classmethod
    def _validate_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            if not _is_json_value(value):
                raise ValueError("invalid_temporal_value")
            type_name = _TEMPORAL_TYPE_BY_SCHEMA_VERSION[value.get("schema_version")]
            # Structural canonicalization is safe at the contract boundary: it
            # expands only schema defaults and makes no claim that the timeline
            # anchor exists in the current project. Contextual binding remains
            # the executor's responsibility.
            return normalize_temporal_value(type_name, value)
        except ValidationError as exc:
            if _is_embedded_duration_bounds_error(exc):
                raise ValueError("range_out_of_bounds") from exc
            raise ValueError("invalid_temporal_value") from exc
        except (
            LookupError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError("invalid_temporal_value") from exc


TemporalSelectionInput = Annotated[
    Union[
        TemporalDraftRangeSelection,
        TemporalDraftPointsSelection,
        TemporalDraftRangesSelection,
        TemporalColumnSelection,
        TemporalTypedValueSelection,
    ],
    Field(discriminator="kind"),
]


__all__ = [
    "TemporalColumnSelection",
    "TemporalDraftPoint",
    "TemporalDraftPointsSelection",
    "TemporalDraftRange",
    "TemporalDraftRangeSelection",
    "TemporalDraftRangesSelection",
    "TemporalSelectionInput",
    "TemporalTypedValueSelection",
]
