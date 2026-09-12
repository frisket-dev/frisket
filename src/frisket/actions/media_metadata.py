"""Typed metadata projections over an invocation-owned local reader."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from jsonschema import Draft202012Validator, ValidationError
from pydantic import Field, StrictBool, field_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    MediaMetadataReader,
    Row,
    RowResult,
)
from frisket.ops.media_metadata import (
    MAX_DETAILS_BYTES,
    MEDIA_METADATA_ENVELOPE_SCHEMA,
    MediaMetadataFields,
    canonical_json_bytes,
    projection_values,
)


class MediaColumn(ColumnRef[Any]):
    accepted_column_types: ClassVar[tuple[str, ...]] = (
        "image",
        "audio",
        "video",
        "file",
    )


class MetadataParams(ActionParams):
    source: MediaColumn
    output_mode: Literal["object", "columns"] = Field(
        default="columns",
        title="Output shape",
        description="Write useful typed fields plus details, or one metadata object.",
    )
    refresh: StrictBool = Field(
        default=False,
        title="Re-read file metadata",
        description="Ignore a compatible cached probe and read the local metadata again.",
    )


_DETAILS_VALIDATOR = Draft202012Validator(MEDIA_METADATA_ENVELOPE_SCHEMA)


class MetadataOutput(MediaMetadataFields):
    details: dict[str, Any] | None = Field(
        default=None,
        description="Complete bounded, versioned media metadata envelope.",
    )

    @field_validator("details")
    @classmethod
    def _bounded_details(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            try:
                _DETAILS_VALIDATOR.validate(value)
            except ValidationError as exc:
                raise ValueError("invalid media metadata envelope") from exc
            if len(canonical_json_bytes(value)) > MAX_DETAILS_BYTES:
                raise ValueError("media metadata envelope exceeds its byte limit")
        return value


def _active_outputs(params: MetadataParams) -> tuple[str, ...]:
    return (
        ("details",)
        if params.output_mode == "object"
        else tuple(MetadataOutput.model_fields)
    )


async def extract_metadata(
    params: MetadataParams, row: Row, metadata: MediaMetadataReader
) -> RowResult[MetadataOutput]:
    envelope = await metadata.read(params.source.read(row), refresh=params.refresh)
    values = (
        {key: None for key in _active_outputs(params)}
        if envelope is None
        else {"details": envelope}
        if params.output_mode == "object"
        else projection_values(envelope)
    )
    return RowResult(output=MetadataOutput.model_validate(values))


EXTRACT_METADATA = action(
    examples=(
        MetadataParams(source="media_source"),
        MetadataParams(source="media_source", output_mode="object"),
    ),
    name="extract_metadata",
    title="Extract media metadata",
    description=(
        "Extract local metadata from blob-backed image, audio, video, or file cells "
        "as one details object or useful typed fields with details."
    ),
    category=ActionCategory.EXTRACT,
    run=map_rows(extract_metadata, active_outputs=_active_outputs),
)
