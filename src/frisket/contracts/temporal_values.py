"""Strict, source-bound temporal values used by core column types.

This module owns only the context-free part of the temporal contract.  It can
prove that a value is well-formed and canonically hash it, but it deliberately
cannot prove that the referenced artifact exists or maps to an action target.
Those checks require a project and belong at contextual ingress/action time.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

__all__ = [
    "JAVASCRIPT_SAFE_INTEGER",
    "TimelineAnchor",
    "TimelinePointItem",
    "TimelinePointValue",
    "TimelinePointsValue",
    "TimelineRangeItem",
    "TimelineRangeValue",
    "TimelineRangesValue",
    "TemporalValue",
    "canonical_temporal_hash",
    "canonical_temporal_json",
    "is_valid_temporal_value",
    "normalize_temporal_value",
    "parse_temporal_value",
    "temporal_model_for_type",
]


JAVASCRIPT_SAFE_INTEGER = 9_007_199_254_740_991

_MAX_ARTIFACT_STABLE_ID_CHARS = 512
_MAX_ITEM_ID_CHARS = 256
_MAX_LABEL_CHARS = 4_096
_MAX_METADATA_DEPTH = 64

_ARTIFACT_STABLE_ID_RE = re.compile(r"^source_artifact:[A-Za-z0-9][A-Za-z0-9._:-]*$")
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class _StrictTemporalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TimelineAnchor(_StrictTemporalModel):
    """Identity of one immutable artifact-local presentation timeline."""

    artifact_stable_id: str = Field(
        min_length=len("source_artifact:x"),
        max_length=_MAX_ARTIFACT_STABLE_ID_CHARS,
    )
    fingerprint: str = Field(min_length=71, max_length=71)
    duration_ms: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=JAVASCRIPT_SAFE_INTEGER,
    )

    @field_validator("artifact_stable_id")
    @classmethod
    def _valid_artifact_stable_id(cls, value: str) -> str:
        if _ARTIFACT_STABLE_ID_RE.fullmatch(value) is None:
            raise ValueError("artifact_stable_id must use source_artifact:<opaque-id>")
        return value

    @field_validator("fingerprint")
    @classmethod
    def _valid_fingerprint(cls, value: str) -> str:
        if _FINGERPRINT_RE.fullmatch(value) is None:
            raise ValueError("fingerprint must be sha256 followed by 64 lowercase hex")
        return value


def _assert_json_value(
    value: Any,
    *,
    path: str,
    depth: int = 0,
    ancestors: set[int] | None = None,
) -> None:
    """Reject Python-only, nonfinite, cyclic, or pathologically deep metadata."""

    if depth > _MAX_METADATA_DEPTH:
        raise ValueError(f"{path} exceeds maximum JSON nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a nonfinite number")
        return
    if not isinstance(value, (list, dict)):
        raise ValueError(f"{path} contains a non-JSON value")

    container_id = id(value)
    active = ancestors if ancestors is not None else set()
    if container_id in active:
        raise ValueError(f"{path} contains a cyclic value")
    active.add(container_id)
    try:
        if isinstance(value, list):
            for index, item in enumerate(value):
                _assert_json_value(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    ancestors=active,
                )
            return
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            _assert_json_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                ancestors=active,
            )
    finally:
        active.remove(container_id)


class _TemporalItem(_StrictTemporalModel):
    id: str = Field(min_length=1, max_length=_MAX_ITEM_ID_CHARS)
    label: str | None = Field(default=None, max_length=_MAX_LABEL_CHARS)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("temporal item id must not be blank")
        return value

    @field_validator("metadata")
    @classmethod
    def _json_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _assert_json_value(value, path="metadata")
        return value


class TimelinePointItem(_TemporalItem):
    at_ms: int = Field(
        strict=True,
        ge=0,
        le=JAVASCRIPT_SAFE_INTEGER,
    )


class TimelineRangeItem(_TemporalItem):
    start_ms: int = Field(
        strict=True,
        ge=0,
        le=JAVASCRIPT_SAFE_INTEGER,
    )
    end_ms: int = Field(
        strict=True,
        ge=0,
        le=JAVASCRIPT_SAFE_INTEGER,
    )

    @model_validator(mode="after")
    def _ordered_nonempty_range(self) -> TimelineRangeItem:
        if self.start_ms >= self.end_ms:
            raise ValueError("timeline range must satisfy start_ms < end_ms")
        return self


def _canonical_json(data: Any) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class _TemporalValueBase(_StrictTemporalModel):
    timeline: TimelineAnchor

    def _check_point_bounds(self, item: TimelinePointItem) -> None:
        duration_ms = self.timeline.duration_ms
        if duration_ms is not None and item.at_ms > duration_ms:
            raise ValueError("timeline point exceeds timeline duration_ms")

    def _check_range_bounds(self, item: TimelineRangeItem) -> None:
        duration_ms = self.timeline.duration_ms
        if duration_ms is not None and item.end_ms > duration_ms:
            raise ValueError("timeline range exceeds timeline duration_ms")


class TimelinePointValue(_TemporalValueBase):
    schema_version: Literal["frisket.timeline_point.v1"]
    item: TimelinePointItem

    @model_validator(mode="after")
    def _point_is_in_timeline(self) -> TimelinePointValue:
        self._check_point_bounds(self.item)
        return self


class TimelinePointsValue(_TemporalValueBase):
    schema_version: Literal["frisket.timeline_points.v1"]
    items: list[TimelinePointItem]

    @model_validator(mode="after")
    def _points_are_valid(self) -> TimelinePointsValue:
        _require_unique_item_ids(self.items)
        for item in self.items:
            self._check_point_bounds(item)
        return self


class TimelineRangeValue(_TemporalValueBase):
    schema_version: Literal["frisket.timeline_range.v1"]
    item: TimelineRangeItem

    @model_validator(mode="after")
    def _range_is_in_timeline(self) -> TimelineRangeValue:
        self._check_range_bounds(self.item)
        return self


class TimelineRangesValue(_TemporalValueBase):
    schema_version: Literal["frisket.timeline_ranges.v1"]
    items: list[TimelineRangeItem]

    @model_validator(mode="after")
    def _ranges_are_valid(self) -> TimelineRangesValue:
        _require_unique_item_ids(self.items)
        for item in self.items:
            self._check_range_bounds(item)
        return self


TemporalValue: TypeAlias = (
    TimelinePointValue | TimelinePointsValue | TimelineRangeValue | TimelineRangesValue
)

_TEMPORAL_MODELS: dict[str, type[TemporalValue]] = {
    "timeline_point": TimelinePointValue,
    "timeline_points": TimelinePointsValue,
    "timeline_range": TimelineRangeValue,
    "timeline_ranges": TimelineRangesValue,
}


def _require_unique_item_ids(
    items: list[TimelinePointItem] | list[TimelineRangeItem],
) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise ValueError(f"duplicate temporal item id: {item.id!r}")
        seen.add(item.id)


def temporal_model_for_type(type_name: str) -> type[TemporalValue]:
    """Return the strict model for one registered temporal column type."""

    try:
        return _TEMPORAL_MODELS[type_name]
    except KeyError as exc:
        raise ValueError(f"unknown temporal column type: {type_name!r}") from exc


def parse_temporal_value(type_name: str, value: Any) -> TemporalValue:
    """Validate ``value`` against the exact schema for ``type_name``."""

    model = temporal_model_for_type(type_name)
    if isinstance(value, model):
        return value
    return model.model_validate(value)


def normalize_temporal_value(type_name: str, value: Any) -> dict[str, Any]:
    """Return a JSON-safe, default-expanded canonical storage object."""

    return parse_temporal_value(type_name, value).model_dump(mode="json")


def is_valid_temporal_value(type_name: str, value: Any) -> bool:
    """Context-free registry predicate; project/timeline checks are separate."""

    try:
        parse_temporal_value(type_name, value)
    except (
        LookupError,
        OverflowError,
        RecursionError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        return False
    return True


def canonical_temporal_json(type_name: str, value: Any) -> str:
    """Canonical normalized JSON used for equality, staleness, and receipts."""

    return _canonical_json(normalize_temporal_value(type_name, value))


def canonical_temporal_hash(type_name: str, value: Any) -> str:
    """SHA-256 identity of a validated, normalized temporal value."""

    encoded = canonical_temporal_json(type_name, value).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
