"""Deterministic, bounded media metadata extraction.

This module is the extraction core used by ``media.extract_metadata`` and by
the ingest-facing projection in :mod:`frisket.ops.media_probe`.  It is
deliberately independent of the action/executor layer: callers give it one
already-materialized project blob and receive the stable details envelope.

The core has three important boundaries:

* native tools are invoked with argv lists, closed stdin or an inherited local
  descriptor, a scrubbed working directory, hard wall/output limits, and no
  user filename in argv;
* only content-derived facts are cacheable; filename and claimed MIME are
  overlaid per media-cell reference; and
* every returned envelope is canonicalizable to at most 64 KiB.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
from contextlib import ExitStack
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from fractions import Fraction
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable, Literal, Mapping

from frisket.runtime.supervisor import guarded_argv, stop_guard

from frisket.runtime.launch import limited_argv, worker_argv

if TYPE_CHECKING:
    from frisket.engine.sandbox.shim import ProcessTreeController

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from frisket.ops.media_metadata_formats import (
    CONTENT_RECOGNIZER_FINGERPRINT,
    MEDIA_METADATA_FORMAT_REGISTRY,
    MEDIA_METADATA_FORMAT_REGISTRY_FINGERPRINT as MEDIA_METADATA_FORMAT_REGISTRY_FINGERPRINT,
    MEDIA_METADATA_FORMAT_REGISTRY_VERSION as MEDIA_METADATA_FORMAT_REGISTRY_VERSION,
    _fingerprint,
    _is_unresolved_bmff_image,
    _parser_kind_corrected_signature_container,
    _recognition,
    _recognize_prefix,
    _select_recognition,
    _signature_with_stream_topology,
    _stream_kind_with_signature_container,
    canonical_json_bytes,
)

MEDIA_METADATA_SCHEMA_VERSION = "frisket.media_metadata.v1"
MEDIA_METADATA_PROJECTION_VERSION = "media_metadata_projection.v1"
MEDIA_METADATA_EXTRACTOR_VERSION = 1
MEDIA_METADATA_LIMITS_VERSION = 1
MEDIA_METADATA_PROBE_SCHEMA_VERSION = 1
MEDIA_METADATA_NORMALIZER_VERSION = 1
MEDIA_METADATA_CACHE_SCHEMA_VERSION = 4
MEDIA_METADATA_OMITTED_CLASS_REGISTRY_VERSION = 2
MEDIA_METADATA_CACHE_NAMESPACE = "_media_metadata_v1"

MAX_ADAPTER_SECONDS = 20.0
MAX_TOTAL_PROBE_SECONDS = 45.0
MAX_ADAPTER_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
MAX_ADAPTER_OPEN_FILES = 64
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 16 * 1024
MAX_SOURCE_STRING_BYTES = 8 * 1024
MAX_SOURCE_VALUES = 2_048
MAX_REPEATED_VALUES = 256
MAX_STREAMS = 64
MAX_CHAPTERS = 512
MAX_ARTWORK = 64
MAX_HDR_SIDE_DATA = 16
MAX_MUSIC_POSITION = 2_147_483_647
MAX_PAGE_DESCRIPTORS = 128
MAX_WARNINGS = 64
MAX_CONFLICTS = 128
MAX_DETAILS_BYTES = 65_536
MAX_CACHE_FACTS_BYTES = 256 * 1024
MAX_HEADER_BYTES = 1024 * 1024

_TECHNICAL_RAW_FIELDS = (
    "index",
    "duration",
    "bit_rate",
    "width",
    "height",
    "sample_rate",
    "channels",
    "avg_frame_rate",
    "r_frame_rate",
    "rotation",
)
_COLOR_FIELDS = (
    "pixel_format",
    "color_range",
    "color_space",
    "color_transfer",
    "color_primaries",
    "chroma_location",
    "field_order",
)
_HDR_VALUE_FIELDS = (
    "red_x",
    "red_y",
    "green_x",
    "green_y",
    "blue_x",
    "blue_y",
    "white_point_x",
    "white_point_y",
    "min_luminance",
    "max_luminance",
    "max_content",
    "max_average",
    "application_version",
    "itu_t_t35_country_code",
    "itu_t_t35_terminal_provider_code",
    "itu_t_t35_terminal_provider_oriented_code",
)


class MediaMetadataFields(BaseModel):
    """The normalized metadata fields shared by envelopes and typed outputs."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["image", "audio", "video", "pdf", "other"] | None = None
    format: str | None = None
    mime: str | None = None
    filename: str | None = None
    size_bytes: int | None = Field(
        default=None, json_schema_extra={"format": "filesize"}
    )
    blob_hash: str | None = None
    probe_status: Literal["ok", "partial", "unsupported", "error"] | None = None
    title: str | None = None
    creator: str | None = None
    created_at: dt.datetime | dt.date | None = None
    width_pixels: int | None = None
    height_pixels: int | None = None
    capture_device: str | None = None
    duration_seconds: float | None = None
    bitrate_bps: int | None = None
    audio_codec: str | None = None
    sample_rate_hz: int | None = None
    channel_count: int | None = None
    album: str | None = None
    video_codec: str | None = None
    frame_rate_fps: float | None = None
    page_count: int | None = None
    pdf_encrypted: bool | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _date_precision(cls, value: Any) -> Any:
        if isinstance(value, str):
            return (
                dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                if "T" in value
                else dt.date.fromisoformat(value)
            )
        return value


def _metadata_projection() -> tuple[dict[str, Any], ...]:
    fields = []
    for role, schema in MediaMetadataFields.model_json_schema()["properties"].items():
        variants = [item for item in schema["anyOf"] if item.get("type") != "null"]
        if any("enum" in item for item in variants):
            column_type = "category"
        elif all(item.get("format") in {"date", "date-time"} for item in variants):
            column_type = "date"
        else:
            json_type = variants[0]["type"]
            column_type = "text" if json_type == "string" else json_type
        fields.append(
            {
                "role": role,
                "column_type": column_type,
                **({"format": schema["format"]} if "format" in schema else {}),
            }
        )
    return tuple(fields)


MEDIA_METADATA_PROJECTION = _metadata_projection()
MEDIA_METADATA_ROLES = tuple(MediaMetadataFields.model_fields)

# Bit positions are a persisted cache contract.  Append-only changes require a
# registry-version bump; existing names must never be reordered in place.
MEDIA_METADATA_OMITTED_CLASS_REGISTRY: tuple[str, ...] = (
    "artwork",
    "binary",
    "chapters",
    "conflicts",
    "envelope_limit",
    "excessive_nesting",
    "adapter_transport",
    "lineage",
    "mapping_values",
    "non_finite",
    "pages",
    "repeated_values",
    "source_value_limit",
    "streams",
    "truncated_value",
    "warnings",
)
_MATERIAL_OMISSION_CLASSES = frozenset(
    {
        "artwork",
        "chapters",
        "conflicts",
        "envelope_limit",
        "excessive_nesting",
        "lineage",
        "mapping_values",
        "non_finite",
        "pages",
        "repeated_values",
        "source_value_limit",
        "streams",
        "truncated_value",
        "warnings",
    }
)
assert len(MEDIA_METADATA_OMITTED_CLASS_REGISTRY) <= 256
assert len(set(MEDIA_METADATA_OMITTED_CLASS_REGISTRY)) == len(
    MEDIA_METADATA_OMITTED_CLASS_REGISTRY
)

SOURCE_NAMESPACES = (
    "blob",
    "exif",
    "icc",
    "iptc",
    "xmp",
    "id3",
    "vorbis",
    "riff",
    "quicktime",
    "matroska",
    "ffprobe",
    "pdf_info",
)

WARNING_CODES = (
    "cancelled",
    "conflict",
    "corrupt",
    "encrypted",
    "internal_error",
    "invalid_value",
    "missing_dependency",
    "resource_limit",
    "timeout",
)

_STRING_IDENTIFIER_TAGS = frozenset(
    {
        "barcode",
        "body_id",
        "body_serial",
        "body_serial_number",
        "camera_id",
        "camera_serial",
        "camera_serial_number",
        "catalog_number",
        "device_id",
        "device_serial",
        "device_serial_number",
        "disc",
        "disc_total",
        "disc_number",
        "discnumber",
        "disctotal",
        "document_id",
        "ean",
        "instance_id",
        "internal_serial_number",
        "isrc",
        "lens_id",
        "lens_serial",
        "lens_serial_number",
        "media_unique_id",
        "serial_number",
        "track",
        "track_total",
        "track_number",
        "tracknumber",
        "tracktotal",
        "total_discs",
        "total_tracks",
        "totaldiscs",
        "totaltracks",
        "unique_material_identifier",
        "upc",
    }
)

ADAPTER_SEMANTIC_VERSIONS = {
    "blob": 1,
    "content_recognizer": 1,
    "exiftool": 8,
    "ffprobe": 6,
    "pypdf": 3,
    "image_header": 1,
}


ADAPTER_FINGERPRINT = _fingerprint(ADAPTER_SEMANTIC_VERSIONS)


def _bounded_string_array_schema(
    maximum: int,
    max_items: int,
    *,
    min_items: int | None = None,
    unique: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array"}
    if min_items is not None:
        schema["minItems"] = min_items
    schema["maxItems"] = max_items
    if unique:
        schema["uniqueItems"] = True
    schema["items"] = {"type": "string", "maxLength": maximum}
    return schema


def _nullable_string_schema(maximum: int) -> dict[str, Any]:
    return {"type": ["string", "null"], "maxLength": maximum}


def _normalized_schema() -> dict[str, Any]:
    schema = MediaMetadataFields.model_json_schema()
    properties = schema["properties"]
    properties["format"]["enum"] = [
        *MEDIA_METADATA_FORMAT_REGISTRY["canonical_formats"],
        None,
    ]
    schema["required"] = list(MEDIA_METADATA_ROLES)
    return schema


def _source_value_schema_defs() -> dict[str, Any]:
    """Fixed-depth JSON definitions for untrusted dynamic source metadata."""

    definitions: dict[str, Any] = {
        "source_scalar": {
            "type": ["string", "number", "boolean", "null"],
            "maxLength": MAX_SOURCE_STRING_BYTES,
        }
    }
    for depth in reversed(range(9)):
        variants: list[dict[str, Any]] = [
            {"$ref": "#/$defs/source_scalar"},
            {
                "type": "array",
                "maxItems": MAX_REPEATED_VALUES,
                "items": {
                    "$ref": (
                        "#/$defs/source_scalar"
                        if depth == 8
                        else f"#/$defs/source_value_{depth + 1}"
                    )
                },
            },
        ]
        if depth < 8:
            variants.append(
                {
                    "type": "object",
                    "maxProperties": MAX_SOURCE_VALUES,
                    "propertyNames": {"type": "string", "maxLength": 512},
                    "additionalProperties": {
                        "$ref": f"#/$defs/source_value_{depth + 1}"
                    },
                }
            )
        definitions[f"source_value_{depth}"] = {"oneOf": variants}
    definitions["source_mapping"] = {
        "type": "object",
        "maxProperties": MAX_SOURCE_VALUES,
        "propertyNames": {"type": "string", "maxLength": 512},
        "additionalProperties": {"$ref": "#/$defs/source_value_0"},
    }
    return definitions


_LINEAGE_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["selected", "candidates", "transforms"],
    "properties": {
        "selected": _nullable_string_schema(1024),
        "candidates": _bounded_string_array_schema(1024, 8, unique=True),
        "transforms": _bounded_string_array_schema(256, 8),
    },
}


# Public schema constant. Every stable record is closed; dynamic source values
# use the fixed-depth definitions above rather than an unrestricted JSON hole.
MEDIA_METADATA_ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": MEDIA_METADATA_SCHEMA_VERSION,
    "$defs": {
        **_source_value_schema_defs(),
        "stream_disposition": {
            "type": "object",
            "additionalProperties": False,
            "required": ["default", "attached_pic", "forced"],
            "properties": {
                "default": {"type": "boolean"},
                "attached_pic": {"type": "boolean"},
                "forced": {"type": "boolean"},
            },
        },
        "technical_raw": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                key: {"type": "string", "maxLength": 256}
                for key in _TECHNICAL_RAW_FIELDS
            },
        },
        "ffprobe_source": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "format_name": _nullable_string_schema(MAX_SOURCE_STRING_BYTES),
                "format_long_name": _nullable_string_schema(MAX_SOURCE_STRING_BYTES),
                "duration_seconds": {"type": ["number", "null"]},
                "bit_rate_bps": {"type": ["integer", "null"]},
                "primary_audio_index": {"type": ["integer", "null"]},
                "primary_video_index": {"type": ["integer", "null"]},
                "raw_technical": {"$ref": "#/$defs/technical_raw"},
                "tags": {"$ref": "#/$defs/source_mapping"},
                "extras": {"$ref": "#/$defs/source_mapping"},
            },
        },
        "hdr_side_data": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "values"],
            "properties": {
                "type": {"type": "string", "maxLength": 128},
                "values": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        key: {"type": "string", "maxLength": 128}
                        for key in _HDR_VALUE_FIELDS
                    },
                },
            },
        },
        "music_position": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "raw",
                "number",
                "total",
                "source_path",
                "total_raw",
                "total_source_path",
            ],
            "properties": {
                "raw": {"type": "string", "maxLength": 256},
                "number": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": MAX_MUSIC_POSITION,
                },
                "total": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": MAX_MUSIC_POSITION,
                },
                "source_path": {"type": "string", "maxLength": 1024},
                "total_raw": _nullable_string_schema(256),
                "total_source_path": _nullable_string_schema(1024),
            },
        },
        "music": {
            "type": "object",
            "additionalProperties": False,
            "required": ["track", "disc"],
            "properties": {
                "track": {
                    "oneOf": [
                        {"$ref": "#/$defs/music_position"},
                        {"type": "null"},
                    ]
                },
                "disc": {
                    "oneOf": [
                        {"$ref": "#/$defs/music_position"},
                        {"type": "null"},
                    ]
                },
            },
        },
        "date_evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["raw", "precision", "source_paths"],
            "properties": {
                "raw": {"type": "string", "maxLength": 256},
                "precision": {
                    "enum": [
                        "year",
                        "month",
                        "day",
                        "hour",
                        "minute",
                        "second",
                        "subsecond",
                    ]
                },
                "source_paths": _bounded_string_array_schema(
                    1024, 2, min_items=1, unique=True
                ),
            },
        },
        "dates": {
            "type": "object",
            "additionalProperties": False,
            "required": ["creation"],
            "properties": {
                "creation": {
                    "oneOf": [
                        {"$ref": "#/$defs/date_evidence"},
                        {"type": "null"},
                    ]
                }
            },
        },
        "stream": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "index",
                "type",
                "codec",
                "disposition",
                "language",
                "duration_seconds",
                "bit_rate_bps",
                "width_pixels",
                "height_pixels",
                "sample_rate_hz",
                "channel_count",
                "avg_frame_rate",
                "r_frame_rate",
                "vfr_evidence",
                *_COLOR_FIELDS,
                "hdr_side_data",
                "rotation_degrees",
                "raw_technical",
                "tags",
                "extras",
                "source_paths",
            ],
            "properties": {
                "index": {"type": ["integer", "null"]},
                "type": {"type": "string", "maxLength": 64},
                "codec": _nullable_string_schema(256),
                "disposition": {"$ref": "#/$defs/stream_disposition"},
                "language": _nullable_string_schema(256),
                "duration_seconds": {"type": ["number", "null"]},
                "bit_rate_bps": {"type": ["integer", "null"]},
                "width_pixels": {"type": ["integer", "null"]},
                "height_pixels": {"type": ["integer", "null"]},
                "sample_rate_hz": {"type": ["integer", "null"]},
                "channel_count": {"type": ["integer", "null"]},
                "avg_frame_rate": _nullable_string_schema(256),
                "r_frame_rate": _nullable_string_schema(256),
                "vfr_evidence": {"type": ["boolean", "null"]},
                **{key: _nullable_string_schema(128) for key in _COLOR_FIELDS},
                "hdr_side_data": {
                    "type": "array",
                    "maxItems": MAX_HDR_SIDE_DATA,
                    "items": {"$ref": "#/$defs/hdr_side_data"},
                },
                "rotation_degrees": {"type": ["integer", "null"]},
                "raw_technical": {"$ref": "#/$defs/technical_raw"},
                "tags": {"$ref": "#/$defs/source_mapping"},
                "extras": {"$ref": "#/$defs/source_mapping"},
                "source_paths": _bounded_string_array_schema(1024, 8),
            },
        },
        "chapter": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "index",
                "start_seconds",
                "end_seconds",
                "title",
                "language",
                "tags",
                "extras",
            ],
            "properties": {
                "index": {"type": ["integer", "null"]},
                "start_seconds": {"type": ["number", "null"]},
                "end_seconds": {"type": ["number", "null"]},
                "title": {"$ref": "#/$defs/source_value_0"},
                "language": {"$ref": "#/$defs/source_value_0"},
                "tags": {"$ref": "#/$defs/source_mapping"},
                "extras": {"$ref": "#/$defs/source_mapping"},
            },
        },
        "artwork": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "source_index",
                "type",
                "mime",
                "width_pixels",
                "height_pixels",
                "description",
                "source_paths",
            ],
            "properties": {
                "source_index": {"type": ["integer", "null"]},
                "type": _nullable_string_schema(64),
                "mime": _nullable_string_schema(256),
                "width_pixels": {"type": ["integer", "null"]},
                "height_pixels": {"type": ["integer", "null"]},
                "description": _nullable_string_schema(1024),
                "source_paths": _bounded_string_array_schema(1024, 8),
            },
        },
        "page": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "index",
                "width_points",
                "height_points",
                "rotation_degrees",
            ],
            "properties": {
                "index": {"type": "integer"},
                "width_points": {"type": "number"},
                "height_points": {"type": "number"},
                "rotation_degrees": {"type": "integer"},
            },
        },
        "lineage_entry": _LINEAGE_ENTRY_SCHEMA,
        "warning": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code"],
            "properties": {
                "code": {"enum": list(WARNING_CODES)},
                **{
                    key: {"type": "string", "maxLength": 300}
                    for key in ("subtype", "source", "path", "message")
                },
            },
        },
        "conflict": {
            "type": "object",
            "additionalProperties": False,
            "required": ["field", "candidate_paths", "reason"],
            "properties": {
                "field": {"type": "string", "maxLength": 256},
                "candidate_paths": _bounded_string_array_schema(1024, 8, unique=True),
                "selected_path": {"type": "string", "maxLength": 1024},
                "reason": {"type": "string", "maxLength": 256},
            },
        },
        "adapter": {
            "type": "object",
            "additionalProperties": False,
            "required": ["required", "available", "outcome", "version"],
            "properties": {
                "required": {"type": "boolean"},
                "available": {"type": "boolean"},
                "outcome": {
                    "enum": [
                        "success",
                        "not_applicable",
                        "missing_dependency",
                        "internal_error",
                        "timeout",
                        "resource_limit",
                        "cancelled",
                        "corrupt",
                        "encrypted",
                    ]
                },
                "version": _nullable_string_schema(256),
            },
        },
    },
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "limits_version",
        "normalized",
        "sources",
        "structure",
        "lineage",
        "conflicts",
        "warnings",
        "omitted",
        "extraction",
    ],
    "properties": {
        "schema_version": {"const": MEDIA_METADATA_SCHEMA_VERSION},
        "limits_version": {"const": MEDIA_METADATA_LIMITS_VERSION},
        "normalized": _normalized_schema(),
        "sources": {
            "type": "object",
            "additionalProperties": False,
            "required": list(SOURCE_NAMESPACES),
            "properties": {
                **{
                    name: {"$ref": "#/$defs/source_mapping"}
                    for name in SOURCE_NAMESPACES
                },
                "ffprobe": {"$ref": "#/$defs/ffprobe_source"},
            },
        },
        "structure": {
            "type": "object",
            "additionalProperties": False,
            "required": ["streams", "chapters", "artwork", "pages"],
            "properties": {
                "streams": {
                    "type": "array",
                    "maxItems": MAX_STREAMS,
                    "items": {"$ref": "#/$defs/stream"},
                },
                "chapters": {
                    "type": "array",
                    "maxItems": MAX_CHAPTERS,
                    "items": {"$ref": "#/$defs/chapter"},
                },
                "artwork": {
                    "type": "array",
                    "maxItems": MAX_ARTWORK,
                    "items": {"$ref": "#/$defs/artwork"},
                },
                "music": {"$ref": "#/$defs/music"},
                "dates": {"$ref": "#/$defs/dates"},
                "pages": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "count": {"type": ["integer", "null"]},
                        "descriptors": {
                            "type": "array",
                            "maxItems": MAX_PAGE_DESCRIPTORS,
                            "items": {"$ref": "#/$defs/page"},
                        },
                    },
                },
            },
        },
        "lineage": {
            "type": "object",
            "additionalProperties": False,
            "required": ["fields"],
            "properties": {
                "fields": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        role: {"$ref": "#/$defs/lineage_entry"}
                        for role in MEDIA_METADATA_ROLES
                    },
                }
            },
        },
        "conflicts": {
            "type": "array",
            "maxItems": MAX_CONFLICTS,
            "items": {"$ref": "#/$defs/conflict"},
        },
        "warnings": {
            "type": "array",
            "maxItems": MAX_WARNINGS,
            "items": {"$ref": "#/$defs/warning"},
        },
        "omitted": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "binary_values",
                "truncated_values",
                "source_values",
                "structure_items",
                "lineage_items",
                "warnings_overflow",
                "conflicts_overflow",
                "paths",
                "paths_overflow",
                "classes",
                "classes_overflow",
            ],
            "properties": {
                **{
                    key: {"type": "integer", "minimum": 0}
                    for key in (
                        "binary_values",
                        "truncated_values",
                        "source_values",
                        "structure_items",
                        "lineage_items",
                        "warnings_overflow",
                        "conflicts_overflow",
                        "paths_overflow",
                        "classes_overflow",
                    )
                },
                "paths": _bounded_string_array_schema(512, 64, unique=True),
                "classes": _bounded_string_array_schema(256, 64, unique=True),
            },
        },
        "extraction": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "probe_schema_version",
                "normalizer_version",
                "projection_registry_version",
                "content_recognizer_fingerprint",
                "adapter_fingerprint",
                "tools",
                "adapters",
                "completeness",
                "retryable",
            ],
            "properties": {
                "probe_schema_version": {"const": MEDIA_METADATA_PROBE_SCHEMA_VERSION},
                "normalizer_version": {"const": MEDIA_METADATA_NORMALIZER_VERSION},
                "projection_registry_version": {
                    "const": MEDIA_METADATA_PROJECTION_VERSION
                },
                "content_recognizer_fingerprint": {
                    "type": "string",
                    "maxLength": 128,
                },
                "adapter_fingerprint": {"type": "string", "maxLength": 128},
                "tools": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        name: {"type": "string", "maxLength": 256}
                        for name in ADAPTER_SEMANTIC_VERSIONS
                    },
                },
                "adapters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(ADAPTER_SEMANTIC_VERSIONS),
                    "properties": {
                        name: {"$ref": "#/$defs/adapter"}
                        for name in ADAPTER_SEMANTIC_VERSIONS
                    },
                },
                "completeness": {
                    "enum": [
                        "complete",
                        "terminal_unsupported",
                        "retryable_failure",
                        "degraded_missing_dependency",
                        "terminal_partial",
                    ]
                },
                "retryable": {"type": "boolean"},
            },
        },
    },
}

_MEDIA_METADATA_ENVELOPE_VALIDATOR = Draft202012Validator(
    MEDIA_METADATA_ENVELOPE_SCHEMA
)


_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_NON_PATH = re.compile(r"[^a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _truncate_utf8(text: str, maximum: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= maximum:
        return text, False
    return raw[:maximum].decode("utf-8", errors="ignore"), True


def _normalize_metadata_string_with_limit(
    value: Any, *, maximum: int = MAX_SOURCE_STRING_BYTES
) -> tuple[str, bool]:
    """Normalize text and report only an actual maximum-byte truncation."""

    text = unicodedata.normalize("NFC", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    return _truncate_utf8(text, maximum)


def normalize_metadata_string(
    value: Any, *, maximum: int = MAX_SOURCE_STRING_BYTES
) -> str:
    return _normalize_metadata_string_with_limit(value, maximum=maximum)[0]


def _path_component(value: Any) -> str:
    # Source keys are persisted as JSON property names.  Bound them before
    # path normalization so adversarial tags cannot create a schema-invalid
    # property name, and so truncation collisions are handled by the same
    # deterministic collision policy as Unicode/control-code collisions.
    text = _CAMEL_BOUNDARY.sub(
        "_", normalize_metadata_string(value, maximum=256).strip()
    )
    return _NON_PATH.sub("_", text.lower()).strip("_") or "unknown"


def _basename(value: str | None) -> str | None:
    if value is None:
        return None
    clean = normalize_metadata_string(value).strip()
    if not clean:
        return None
    return PurePosixPath(clean.replace("\\", "/")).name or None


def canonical_envelope_json(envelope: Mapping[str, Any]) -> str:
    """Return the normative compact/sorted JSON representation."""
    return canonical_json_bytes(envelope).decode("utf-8")


class _AdapterJSONError(ValueError):
    pass


def _decode_adapter_json(payload: bytes, *, empty: str) -> Any:
    """Decode bounded adapter JSON and reject non-Unicode scalar strings."""

    try:
        value = json.loads(payload.decode("utf-8", errors="strict") or empty)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _AdapterJSONError("adapter returned invalid JSON") from exc

    # CPython's JSON decoder accepts escaped lone UTF-16 surrogates even though
    # they cannot be represented by the canonical UTF-8 envelope.  Validate
    # iteratively so adversarial nesting cannot recurse in the parent process.
    pending = [value]
    try:
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                item.encode("utf-8", errors="strict")
            elif isinstance(item, Mapping):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise _AdapterJSONError("adapter returned invalid JSON") from exc
    return value


def _qualified_digest(digest: str) -> str:
    clean = normalize_metadata_string(digest).strip().lower()
    if clean.startswith("sha256:"):
        clean = clean[7:]
    if len(clean) != 64 or any(c not in "0123456789abcdef" for c in clean):
        raise ValueError("digest must be a SHA-256 hexadecimal value")
    return f"sha256:{clean}"


def _empty_normalized() -> dict[str, Any]:
    return {role: None for role in MEDIA_METADATA_ROLES}


class _OmittedState(dict[str, Any]):
    """Public omission counters with exact private distinct-label sets.

    ``json.dumps`` serializes only the dict payload, so the private sets do not
    alter the closed public envelope. Cache entries preserve their overflow
    tails in hashed content facts and restore these sets before warm overlays.
    """

    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        super().__init__(value or {})
        self.all_paths = set(str(item) for item in self.get("paths") or [])
        self.all_classes = set(str(item) for item in self.get("classes") or [])


def _empty_omitted() -> _OmittedState:
    return _OmittedState(
        {
            "binary_values": 0,
            "truncated_values": 0,
            "source_values": 0,
            "structure_items": 0,
            "lineage_items": 0,
            "warnings_overflow": 0,
            "conflicts_overflow": 0,
            "paths": [],
            "paths_overflow": 0,
            "classes": [],
            "classes_overflow": 0,
        }
    )


def _omitted_labels(omitted: Mapping[str, Any], key: str) -> set[str]:
    attribute = "all_paths" if key == "paths" else "all_classes"
    values = getattr(omitted, attribute, None)
    if isinstance(values, set):
        return set(str(item) for item in values)
    return set(str(item) for item in omitted.get(key) or [])


def _sync_omitted_labels(omitted: dict[str, Any], key: str, values: set[str]) -> None:
    ordered = sorted(values)
    omitted[key] = ordered[:64]
    omitted[f"{key}_overflow"] = max(0, len(ordered) - 64)
    attribute = "all_paths" if key == "paths" else "all_classes"
    if isinstance(omitted, _OmittedState):
        setattr(omitted, attribute, set(ordered))


def _record_omission(
    omitted: dict[str, Any],
    key: str,
    *,
    path: str | None = None,
    semantic_class: str | None = None,
    count: int = 1,
) -> None:
    omitted[key] = int(omitted.get(key) or 0) + count
    if path:
        paths = _omitted_labels(omitted, "paths")
        paths.add(normalize_metadata_string(path, maximum=512))
        _sync_omitted_labels(omitted, "paths", paths)
    if semantic_class:
        classes = _omitted_labels(omitted, "classes")
        classes.add(normalize_metadata_string(semantic_class, maximum=256))
        _sync_omitted_labels(omitted, "classes", classes)


def _warning(
    code: str,
    *,
    source: str | None = None,
    path: str | None = None,
    subtype: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code}
    for key, value in (
        ("subtype", subtype),
        ("source", source),
        ("path", path),
        ("message", message),
    ):
        if value:
            item[key] = normalize_metadata_string(value, maximum=300)
    return item


def _sort_warnings(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[bytes, dict[str, Any]] = {}
    for item in items:
        unique[canonical_json_bytes(item)] = item
    return sorted(
        unique.values(),
        key=lambda x: (
            str(x.get("code") or ""),
            str(x.get("subtype") or ""),
            str(x.get("source") or ""),
            str(x.get("path") or ""),
            str(x.get("message") or ""),
        ),
    )


def _sort_conflicts(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[bytes] = set()
    for raw in items:
        item = dict(raw)
        item["candidate_paths"] = sorted(set(item.get("candidate_paths") or []))[:8]
        encoded = canonical_json_bytes(item)
        if encoded not in seen:
            seen.add(encoded)
            normalized.append(item)
    return sorted(
        normalized,
        key=lambda x: (
            str(x.get("field") or ""),
            str(x.get("selected_path") or ""),
            str(x.get("reason") or ""),
            tuple(x.get("candidate_paths") or []),
        ),
    )


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limited: bool = False
    cancelled: bool = False


def _resource_limited_argv(argv: list[str], *, timeout: float) -> list[str]:
    return limited_argv(
        argv,
        cpu_seconds=max(1, int(math.ceil(float(timeout)))),
        memory_mb=MAX_ADAPTER_ADDRESS_SPACE_BYTES // (1024 * 1024),
        open_files=MAX_ADAPTER_OPEN_FILES,
    )


def _adapter_env(home: str) -> dict[str, str]:
    env = {
        "HOME": home,
        "USERPROFILE": home,
        "TEMP": home,
        "TMP": home,
        "TMPDIR": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            if os.environ.get(key):
                env[key] = os.environ[key]
    return env


def _kill_process(
    proc: subprocess.Popen[bytes], tree_controller: ProcessTreeController
) -> None:
    """Kill the owned tree even when its direct leader has already exited."""

    if os.name == "posix":
        stop_guard(proc)
        return

    try:
        tree_controller.signal_tree(getattr(signal, "SIGKILL", signal.SIGTERM))
    except (OSError, ProcessLookupError):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _run_bounded(
    argv: list[str],
    *,
    timeout: float = MAX_ADAPTER_SECONDS,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
    cancel_event: threading.Event | None = None,
    stdin_path: Path | None = None,
) -> _CommandResult:
    """Run a fixed argv with bounded incremental pipe drains.

    ``subprocess.run(capture_output=True)`` can allocate without bound before a
    caller gets a chance to truncate.  Two small reader threads cap bytes while
    the parent supervises wall time and kills on overflow.
    """

    if not argv or not Path(argv[0]).is_absolute():
        raise ValueError("adapter executable must be an absolute path")
    if cancel_event is not None and cancel_event.is_set():
        return _CommandResult(
            returncode=-1,
            stdout=b"",
            stderr=b"",
            cancelled=True,
        )
    deadline = time.monotonic() + max(0.01, timeout)
    supervised_argv = guarded_argv(_resource_limited_argv(argv, timeout=timeout))
    with (
        tempfile.TemporaryDirectory(prefix="frisket-metadata-adapter-") as scratch,
        ExitStack() as resources,
    ):
        input_source = (
            resources.enter_context(stdin_path.open("rb", buffering=0))
            if stdin_path is not None
            else None
        )
        popen_kwargs: dict[str, Any] = {
            "cwd": scratch,
            "env": _adapter_env(scratch),
            # A regular inherited descriptor remains seekable. This lets
            # ffprobe use its fd-only protocol without learning the blob path
            # or gaining access to file/network protocols.
            "stdin": input_source if input_source is not None else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(supervised_argv, **popen_kwargs)  # noqa: S603
        try:
            # The shared controller gives native Windows a fail-closed Job
            # Object and gives every POSIX host a process-group identity that
            # remains usable after the leader exits.
            from frisket.engine.sandbox.shim import ProcessTreeController

            tree_controller = ProcessTreeController(
                proc,
                memory_limit_bytes=MAX_ADAPTER_ADDRESS_SPACE_BYTES,
                cpu_limit_seconds=max(1, int(math.ceil(float(timeout)))),
            )
        except BaseException as exc:
            if os.name == "posix":
                stop_guard(proc)
            else:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2)
                except (OSError, subprocess.SubprocessError):
                    pass
            raise subprocess.SubprocessError(
                "adapter process-tree ownership unavailable"
            ) from exc

        try:
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            overflow = threading.Event()

            def drain(name: str, pipe: Any, limit: int) -> None:
                try:
                    while True:
                        chunk = pipe.read(64 * 1024)
                        if not chunk:
                            return
                        room = max(0, limit - len(buffers[name]))
                        if room:
                            buffers[name].extend(chunk[:room])
                        if len(chunk) > room:
                            overflow.set()
                            return
                finally:
                    try:
                        pipe.close()
                    except OSError:
                        pass

            threads = [
                threading.Thread(
                    target=drain,
                    args=("stdout", proc.stdout, stdout_limit),
                    daemon=True,
                ),
                threading.Thread(
                    target=drain,
                    args=("stderr", proc.stderr, stderr_limit),
                    daemon=True,
                ),
            ]
            for thread in threads:
                thread.start()
            timed_out = False
            cancelled = False
            while proc.poll() is None:
                if overflow.is_set():
                    _kill_process(proc, tree_controller)
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    _kill_process(proc, tree_controller)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _kill_process(proc, tree_controller)
                    break
                time.sleep(0.01)

            # A parser leader may exit successfully after spawning a helper
            # that retained pipes or the scratch cwd.  Tear down the owned tree
            # before joining drains, even on return code zero.
            _kill_process(proc, tree_controller)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_process(proc, tree_controller)
                proc.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=2)
            return _CommandResult(
                returncode=int(proc.returncode or 0),
                stdout=bytes(buffers["stdout"]),
                stderr=bytes(buffers["stderr"]),
                timed_out=timed_out,
                output_limited=overflow.is_set(),
                cancelled=cancelled,
            )
        finally:
            _kill_process(proc, tree_controller)
            tree_controller.close()


@dataclass
class _AdapterResult:
    name: str
    outcome: str
    available: bool
    version: str | None = None
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    structure: dict[str, Any] = field(default_factory=dict)
    recognitions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    omitted: dict[str, Any] = field(default_factory=_empty_omitted)


def _resolved_tool(
    env_name: str, managed_candidates: tuple[str, ...], fallback: str | None
) -> Path | None:
    def executable_file(candidate: Path) -> Path | None:
        if not candidate.is_absolute() or not candidate.is_file():
            return None
        # A path merely existing is not runtime availability.  In particular,
        # do not advertise a bind-mounted notice/config file as a parser and
        # then turn every row into an internal-error retry loop.
        if not os.access(candidate, os.X_OK):
            return None
        return candidate.resolve()

    override = os.environ.get(env_name)
    if override:
        candidate = Path(override).expanduser()
        return executable_file(candidate)
    for raw in managed_candidates:
        resolved = executable_file(Path(raw))
        if resolved is not None:
            return resolved
    if fallback is None:
        return None
    found = shutil.which(fallback)
    return Path(found).resolve() if found else None


def _resolve_exiftool_path() -> Path | None:
    return _resolved_tool(
        "FRISKET_EXIFTOOL_PATH",
        ("/opt/frisket/exiftool/exiftool", "/usr/local/bin/exiftool"),
        # First-party runtimes own this dependency. Custom runtimes opt in
        # with an explicit absolute path; never execute an arbitrary PATH
        # candidate for a parser that consumes hostile metadata.
        None,
    )


def _resolve_ffprobe_path() -> Path | None:
    return _resolved_tool(
        "FRISKET_FFPROBE_PATH",
        ("/opt/frisket/ffmpeg/ffprobe", "/usr/local/bin/ffprobe"),
        "ffprobe",
    )


_ToolStatSignature = tuple[int, int, int, int]
_VERSION_CACHE: dict[tuple[str, str, _ToolStatSignature], str] = {}
_FFPROBE_FD_INPUT_CACHE: dict[tuple[str, _ToolStatSignature], bool] = {}
_TOOL_CACHE_LOCK = threading.Lock()


def _tool_stat_signature(path: Path) -> _ToolStatSignature | None:
    """Return a cheap cache-invalidation token without hashing tool contents."""

    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def _cached_ffprobe_fd_input_support(path: Path) -> bool | None:
    signature = _tool_stat_signature(path)
    if signature is None:
        return None
    cache_key = (str(path.resolve()), signature)
    with _TOOL_CACHE_LOCK:
        return _FFPROBE_FD_INPUT_CACHE.get(cache_key)


def _ffprobe_supports_fd_input(
    path: Path,
    timeout: float = 5.0,
    cancel_event: threading.Event | None = None,
) -> bool | None:
    """Return advertised input-fd support, caching only stable answers."""

    signature = _tool_stat_signature(path)
    if signature is None:
        return None
    resolved = str(path.resolve())
    cache_key = (resolved, signature)
    with _TOOL_CACHE_LOCK:
        if cache_key in _FFPROBE_FD_INPUT_CACHE:
            return _FFPROBE_FD_INPUT_CACHE[cache_key]
    try:
        result = _run_bounded(
            [str(path), "-v", "error", "-protocols"],
            timeout=max(0.01, min(5.0, float(timeout))),
            stdout_limit=32 * 1024,
            stderr_limit=4 * 1024,
            cancel_event=cancel_event,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if (
        result.returncode != 0
        or result.cancelled
        or result.timed_out
        or result.output_limited
    ):
        # A failed or interrupted identity probe is transient. In particular,
        # do not turn one timeout into a process-lifetime dependency claim.
        return None

    in_input_section = False
    saw_input_section = False
    input_protocols: set[str] = set()
    for raw_line in result.stdout.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line == "Input:":
            in_input_section = True
            saw_input_section = True
            continue
        if line == "Output:":
            if in_input_section:
                break
            continue
        if in_input_section and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]*", line):
            input_protocols.add(line)
    if not saw_input_section:
        return None
    supported = "fd" in input_protocols
    if _tool_stat_signature(path) != signature:
        return None
    with _TOOL_CACHE_LOCK:
        for key in tuple(_FFPROBE_FD_INPUT_CACHE):
            if key[0] == resolved and key != cache_key:
                _FFPROBE_FD_INPUT_CACHE.pop(key, None)
        _FFPROBE_FD_INPUT_CACHE[cache_key] = supported
    return supported


def _tool_version(
    path: Path,
    kind: str,
    timeout: float = 5.0,
    cancel_event: threading.Event | None = None,
) -> str | None:
    signature = _tool_stat_signature(path)
    if signature is None:
        return None
    resolved = str(path.resolve())
    cache_key = (kind, resolved, signature)
    fd_cache_key = (resolved, signature)
    with _TOOL_CACHE_LOCK:
        fd_support = (
            _FFPROBE_FD_INPUT_CACHE.get(fd_cache_key) if kind == "ffprobe" else None
        )
        if kind == "ffprobe" and fd_support is False:
            return None
        if cache_key in _VERSION_CACHE and (kind != "ffprobe" or fd_support is True):
            return _VERSION_CACHE[cache_key]
    argv = [str(path), "-ver"] if kind == "exiftool" else [str(path), "-version"]
    probe_timeout = max(0.01, min(5.0, float(timeout)))
    probe_started = time.monotonic()
    try:
        result = _run_bounded(
            argv,
            timeout=probe_timeout,
            stdout_limit=8 * 1024,
            stderr_limit=4 * 1024,
            cancel_event=cancel_event,
        )
    except (OSError, subprocess.SubprocessError):
        with _TOOL_CACHE_LOCK:
            for key in tuple(_VERSION_CACHE):
                if key[:2] == (kind, resolved) and key != cache_key:
                    _VERSION_CACHE.pop(key, None)
        return None
    if result.cancelled or result.timed_out:
        # Cancellation and deadline expiry are transient execution outcomes,
        # not durable claims that a managed binary has no version.
        return None
    version: str | None = None
    if result.returncode == 0 and not result.timed_out and not result.output_limited:
        first = result.stdout.decode("utf-8", errors="replace").splitlines()
        if first:
            line = normalize_metadata_string(first[0], maximum=200).strip()
            if kind == "ffprobe":
                match = re.search(r"ffprobe version\s+([^\s]+)", line, re.I)
                version = match.group(1) if match else None
            else:
                version = line if re.fullmatch(r"\d+(?:\.\d+)+", line) else None
    if kind == "ffprobe" and version is not None:
        # Version identity is usable only with the seekable descriptor input
        # that keeps hostile media away from file and network protocols.
        remaining = probe_timeout - (time.monotonic() - probe_started)
        fd_support = (
            _ffprobe_supports_fd_input(
                path,
                timeout=max(0.01, remaining),
                cancel_event=cancel_event,
            )
            if remaining > 0
            else None
        )
        if fd_support is not True:
            with _TOOL_CACHE_LOCK:
                for key in tuple(_VERSION_CACHE):
                    if key[:2] == (kind, resolved) and key != cache_key:
                        _VERSION_CACHE.pop(key, None)
            return None
    if _tool_stat_signature(path) != signature:
        return None
    with _TOOL_CACHE_LOCK:
        for key in tuple(_VERSION_CACHE):
            if key[:2] == (kind, resolved) and key != cache_key:
                _VERSION_CACHE.pop(key, None)
        if version is not None:
            _VERSION_CACHE[cache_key] = version
    return version


def _pypdf_version() -> str | None:
    try:
        return normalize_metadata_string(package_version("pypdf"), maximum=256) or None
    except (PackageNotFoundError, ValueError):
        return None


def _adapter_remaining_seconds(started: float, timeout: float) -> float:
    return max(0.0, float(timeout) - (time.monotonic() - started))


def _read_prefix(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_HEADER_BYTES), []
    except OSError:
        return b"", [
            _warning(
                "internal_error",
                source="content_recognizer",
                message="blob could not be read",
            )
        ]


_FFPROBE_PAYLOAD_FIELDS = frozenset(
    {
        "codec_private",
        "codec_private_data",
        "data",
        "extradata",
        "frame",
        "frame_data",
        "frames",
        "packet",
        "packet_data",
        "packets",
        "payload",
    }
)
# ExifTool's family-1 ``System`` group is explicitly its filesystem pseudo-tag
# namespace, while ``File`` contains useful content facts such as dimensions,
# byte order, bit depth, format identity, and embedded comments.  The JSON
# envelope's special ungrouped ``SourceFile`` member is parsed as ``File`` by
# this adapter, so it is the sole exact File-group transport exception.
_EXIFTOOL_TRANSPORT_ROOT_GROUPS = frozenset({"exif_tool", "system"})
_EXIFTOOL_FILE_TRANSPORT_TAGS = frozenset({"source_file"})
_EXIFTOOL_JSONQ_NUMBER_RE = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z"
)
_EXIFTOOL_JSONQ_NUMERIC_TAGS = frozenset(
    {
        "audio_bitrate",
        "audio_channels",
        "audio_sample_rate",
        "bit_depth",
        "bits_per_sample",
        "channel_count",
        "duration",
        "exif_image_height",
        "exif_image_width",
        "file_size",
        "frame_rate",
        "gps_altitude",
        "gps_latitude",
        "gps_longitude",
        "image_height",
        "image_width",
        "orientation",
        "page_count",
        "rotation",
        "sample_rate",
        "video_bitrate",
    }
)
_POLICY_OMITTED = object()


def _exiftool_root_group(adapter_group: str | None) -> str:
    """Return ExifTool's family-1 group from the ``-G4:1`` group prefix."""

    clean_group = normalize_metadata_string(adapter_group or "", maximum=256).strip(
        "[]"
    )
    return _path_component(clean_group.rsplit(":", 1)[-1]) if clean_group else ""


def _restore_exiftool_jsonq_value(value: Any, *, tag: str) -> Any:
    """Restore numeric types only for explicit measurement/count leaves.

    ``StructFormat=JSONQ`` prevents ExifTool's JSON encoder from erasing
    leading zeroes in values which merely look numeric.  Unknown/vendor values
    therefore remain lexical even if they match JSON number syntax.  Only the
    closed set of technical measurements used numerically by this contract is
    converted back to the types emitted by ordinary JSON mode.
    """

    if isinstance(value, str):
        if _path_component(tag) not in _EXIFTOOL_JSONQ_NUMERIC_TAGS:
            return value
        clean = value.strip()
        if not _EXIFTOOL_JSONQ_NUMBER_RE.fullmatch(clean):
            return value
        try:
            if "." not in clean and "e" not in clean.lower():
                return int(clean)
            number = float(clean)
        except (OverflowError, ValueError):
            return value
        return number if math.isfinite(number) else value
    if isinstance(value, list):
        return [_restore_exiftool_jsonq_value(item, tag=tag) for item in value]
    if isinstance(value, Mapping):
        return {
            key: _restore_exiftool_jsonq_value(item, tag=_path_component(key))
            for key, item in value.items()
        }
    return value


def _tag_disposition(
    tag: str,
    *,
    adapter: str | None = None,
    adapter_group: str | None = None,
) -> str:
    """Classify a top-level adapter tag using explicit group identity."""

    clean_tag = _path_component(tag)
    root_group = _exiftool_root_group(adapter_group) if adapter == "exiftool" else ""
    if root_group in _EXIFTOOL_TRANSPORT_ROOT_GROUPS:
        return "always_transport_omit"
    if root_group == "file" and clean_tag in _EXIFTOOL_FILE_TRANSPORT_TAGS:
        return "always_transport_omit"
    return "safe"


def _source_namespace(group: str, tag: str) -> str:
    low = f"{group}.{tag}".lower()
    if "xmp" in low:
        return "xmp"
    if "iptc" in low or "photoshop" in low:
        return "iptc"
    if "icc" in low or "profile" in group.lower():
        return "icc"
    if "id3" in low:
        return "id3"
    if "vorbis" in low:
        return "vorbis"
    if "riff" in low or "bwf" in low or "broadcast" in low:
        return "riff"
    if "quicktime" in low or "keys" in group.lower() or "itemlist" in group.lower():
        return "quicktime"
    if "matroska" in low:
        return "matroska"
    if "pdf" in low:
        return "pdf_info"
    if any(token in low for token in ("exif", "ifd", "makernotes", "composite")):
        return "exif"
    return "blob"


def _store_source_value(
    target: dict[str, Any],
    key: str,
    value: Any,
    omitted: dict[str, Any],
    *,
    path: str,
) -> bool:
    """Store one normalized source key without silently overwriting a peer.

    Native metadata maps can contain distinct raw keys that collapse after
    NFC normalization, control-code removal, truncation, or path sanitization.
    Iteration order is canonicalized by each caller; the first canonical raw
    key wins and later colliders are counted as omitted mapping values.
    """

    if key in target:
        _record_omission(
            omitted,
            "source_values",
            path=path,
            semantic_class="mapping_values",
        )
        return False
    target[key] = value
    return True


def _bounded_json_value(
    value: Any, omitted: dict[str, Any], *, path: str, depth: int = 0
) -> Any:
    # ``source_value_0..8`` permits objects through depth 7, arrays through
    # depth 8, and only scalar array members beyond that final array layer.
    # Keep this guard byte-for-byte aligned with the closed JSON schema.
    if isinstance(value, Mapping) and depth >= 8:
        _record_omission(
            omitted, "source_values", path=path, semantic_class="excessive_nesting"
        )
        return None
    if isinstance(value, (list, tuple)) and depth > 8:
        _record_omission(
            omitted, "source_values", path=path, semantic_class="excessive_nesting"
        )
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _record_omission(
                omitted, "source_values", path=path, semantic_class="non_finite"
            )
            return None
        return value
    if isinstance(value, str):
        text, truncated = _normalize_metadata_string_with_limit(value)
        if truncated:
            _record_omission(
                omitted,
                "truncated_values",
                path=path,
                semantic_class="truncated_value",
            )
        return text
    if isinstance(value, (list, tuple)):
        items = list(value)
        if len(items) > MAX_REPEATED_VALUES:
            _record_omission(
                omitted,
                "source_values",
                path=path,
                semantic_class="repeated_values",
                count=len(items) - MAX_REPEATED_VALUES,
            )
            items = items[:MAX_REPEATED_VALUES]
        return [
            _bounded_json_value(item, omitted, path=f"{path}[{index}]", depth=depth + 1)
            for index, item in enumerate(items)
        ]
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        keys = sorted(
            value,
            key=lambda item: (
                normalize_metadata_string(item),
                str(item),
            ),
        )
        if len(keys) > MAX_REPEATED_VALUES:
            _record_omission(
                omitted,
                "source_values",
                path=path,
                semantic_class="mapping_values",
                count=len(keys) - MAX_REPEATED_VALUES,
            )
            keys = keys[:MAX_REPEATED_VALUES]
        for key in keys:
            clean_key = normalize_metadata_string(key, maximum=256)
            bounded = _bounded_json_value(
                value[key], omitted, path=f"{path}.{clean_key}", depth=depth + 1
            )
            _store_source_value(
                out,
                clean_key,
                bounded,
                omitted,
                path=f"{path}.{clean_key}",
            )
        return out
    return normalize_metadata_string(value)


def _bounded_tag_value(
    value: Any,
    omitted: dict[str, Any],
    *,
    path: str,
    depth: int = 0,
) -> Any:
    """Bound one native tag value, omitting actual byte-valued leaves."""

    if isinstance(value, Mapping) and depth >= 8:
        _record_omission(
            omitted, "source_values", path=path, semantic_class="excessive_nesting"
        )
        return _POLICY_OMITTED
    if isinstance(value, (list, tuple)) and depth > 8:
        _record_omission(
            omitted, "source_values", path=path, semantic_class="excessive_nesting"
        )
        return _POLICY_OMITTED
    if isinstance(value, str):
        return _bounded_json_value(value, omitted, path=path, depth=depth)
    if isinstance(value, (bytes, bytearray, memoryview)):
        _record_omission(
            omitted,
            "binary_values",
            path=path,
            semantic_class="binary",
        )
        return _POLICY_OMITTED
    if isinstance(value, (list, tuple)):
        items = list(value)
        if len(items) > MAX_REPEATED_VALUES:
            _record_omission(
                omitted,
                "source_values",
                path=path,
                semantic_class="repeated_values",
                count=len(items) - MAX_REPEATED_VALUES,
            )
            items = items[:MAX_REPEATED_VALUES]
        out: list[Any] = []
        for index, item in enumerate(items):
            bounded = _bounded_tag_value(
                item,
                omitted,
                path=f"{path}[{index}]",
                depth=depth + 1,
            )
            if bounded is not _POLICY_OMITTED:
                out.append(bounded)
        return out
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        keys = sorted(
            value,
            key=lambda item: (
                normalize_metadata_string(item),
                str(item),
            ),
        )
        if len(keys) > MAX_REPEATED_VALUES:
            _record_omission(
                omitted,
                "source_values",
                path=path,
                semantic_class="mapping_values",
                count=len(keys) - MAX_REPEATED_VALUES,
            )
            keys = keys[:MAX_REPEATED_VALUES]
        for raw_key in keys:
            clean_key = normalize_metadata_string(raw_key, maximum=256)
            child_path = f"{path}.{_path_component(raw_key)}"
            bounded = _bounded_tag_value(
                value[raw_key],
                omitted,
                path=child_path,
                depth=depth + 1,
            )
            if bounded is not _POLICY_OMITTED:
                _store_source_value(
                    out,
                    clean_key,
                    bounded,
                    omitted,
                    path=child_path,
                )
        return out
    return _bounded_json_value(value, omitted, path=path, depth=depth)


def _format_from_tool(
    file_type: str | None, mime: str | None
) -> tuple[str, str | None, str | None] | None:
    raw_tag = normalize_metadata_string(file_type or "", maximum=256).strip()
    tag = _NON_PATH.sub("", raw_tag.lower()) if raw_tag else ""
    bounded_mime = normalize_metadata_string(mime or "", maximum=257)
    clean_mime = ""
    if len(bounded_mime.encode("utf-8")) <= 256:
        clean_mime = bounded_mime.lower().split(";", 1)[0].strip()
    tool_identity = MEDIA_METADATA_FORMAT_REGISTRY["tool_aliases"].get(tag)
    mime_identity = MEDIA_METADATA_FORMAT_REGISTRY["mime_aliases"].get(clean_mime)
    mime_match = re.fullmatch(
        r"(?P<family>[a-z][a-z0-9!#$&^_.+-]{0,63})/"
        r"[a-z0-9][a-z0-9!#$&^_.+-]{0,190}",
        clean_mime,
    )
    generic_identity = (
        (mime_match.group("family"), None, clean_mime)
        if mime_match is not None and mime_match.group("family") in {"audio", "video"}
        else None
    )
    if clean_mime in MEDIA_METADATA_FORMAT_REGISTRY["unsupported_tool_mime_pairs"].get(
        tag, ()
    ):
        # Generic MIME-family evidence can establish the media kind without
        # incorrectly promoting (for example) MPEG Layer II to canonical MP3.
        return generic_identity
    if tool_identity is not None:
        if (
            mime_identity is not None
            and tag in set(MEDIA_METADATA_FORMAT_REGISTRY["ambiguous_tool_aliases"])
            and tool_identity[1] == mime_identity[1]
        ):
            return tuple(mime_identity)
        return tuple(tool_identity)
    # A concrete but unregistered parser FileType cannot establish a canonical
    # format. It also must not erase useful audio/video MIME-family evidence:
    # keep the kind and bounded MIME while leaving ``format`` null.
    if raw_tag:
        return generic_identity
    return tuple(mime_identity) if mime_identity is not None else generic_identity


def _exiftool_adapter(
    path: Path,
    *,
    timeout: float = MAX_ADAPTER_SECONDS,
    cancel_event: threading.Event | None = None,
) -> _AdapterResult:
    executable = _resolve_exiftool_path()
    if executable is None:
        return _AdapterResult(
            name="exiftool",
            outcome="missing_dependency",
            available=False,
            warnings=[
                _warning(
                    "missing_dependency",
                    source="exiftool",
                    message="ExifTool is unavailable",
                )
            ],
        )
    adapter_started = time.monotonic()
    version = _tool_version(
        executable,
        "exiftool",
        min(5.0, max(0.01, float(timeout))),
        cancel_event,
    )
    remaining = _adapter_remaining_seconds(adapter_started, timeout)
    if version is None:
        if cancel_event is not None and cancel_event.is_set():
            outcome = "cancelled"
            message = "ExifTool was cancelled during its identity probe"
        elif remaining <= 0:
            outcome = "timeout"
            message = "ExifTool exceeded its time limit during its identity probe"
        else:
            outcome = "missing_dependency"
            message = "ExifTool failed its version probe"
        return _AdapterResult(
            name="exiftool",
            outcome=outcome,
            available=False,
            warnings=[_warning(outcome, source="exiftool", message=message)],
        )
    try:
        if cancel_event is not None and cancel_event.is_set():
            result = _CommandResult(-1, b"", b"", cancelled=True)
        elif remaining <= 0:
            result = _CommandResult(-1, b"", b"", timed_out=True)
        else:
            result = _run_bounded(
                [
                    str(executable),
                    "-config",
                    "",
                    "-json",
                    "-G4:1",
                    "-s",
                    "-struct",
                    "-n",
                    "-api",
                    "StructFormat=JSONQ",
                    "-api",
                    "LargeFileSupport=1",
                    "-api",
                    "RequestAll=3",
                    "--b",
                    "--",
                    str(path.resolve()),
                ],
                timeout=remaining,
                cancel_event=cancel_event,
            )
    except (OSError, subprocess.SubprocessError):
        return _AdapterResult(
            name="exiftool",
            outcome="internal_error",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "internal_error",
                    source="exiftool",
                    message="ExifTool could not be started",
                )
            ],
        )
    if result.cancelled:
        return _AdapterResult(
            name="exiftool",
            outcome="cancelled",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "cancelled",
                    source="exiftool",
                    message="ExifTool was cancelled",
                )
            ],
        )
    if result.timed_out:
        return _AdapterResult(
            name="exiftool",
            outcome="timeout",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "timeout",
                    source="exiftool",
                    message="ExifTool exceeded its time limit",
                )
            ],
        )
    if result.output_limited:
        return _AdapterResult(
            name="exiftool",
            outcome="resource_limit",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "resource_limit",
                    source="exiftool",
                    message="ExifTool output exceeded its limit",
                )
            ],
        )
    if result.returncode != 0:
        return _AdapterResult(
            name="exiftool",
            outcome="corrupt",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "corrupt",
                    source="exiftool",
                    message="ExifTool could not parse the file",
                )
            ],
        )
    try:
        rows = _decode_adapter_json(result.stdout, empty="[]")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise _AdapterJSONError("ExifTool returned an unexpected JSON shape")
        row = rows[0]
    except _AdapterJSONError:
        return _AdapterResult(
            name="exiftool",
            outcome="internal_error",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "internal_error",
                    source="exiftool",
                    message="ExifTool returned invalid JSON",
                )
            ],
        )
    sources = {name: {} for name in SOURCE_NAMESPACES}
    omitted = _empty_omitted()
    recognitions: list[dict[str, Any]] = []
    raw_identity: dict[str, Any] = {}
    accepted = 0
    for raw_key in sorted(
        row, key=lambda item: (normalize_metadata_string(item), str(item))
    ):
        raw_key_text = str(raw_key)
        match = re.match(r"^\[(?P<group>[^]]+)\](?P<tag>.*)$", raw_key_text)
        if match:
            group = match.group("group")
            tag_raw = match.group("tag")
        elif ":" in raw_key_text:
            group, tag_raw = raw_key_text.rsplit(":", 1)
        else:
            group = "File"
            tag_raw = raw_key_text
        tag = _path_component(tag_raw)
        group_path = ".".join(
            _path_component(component) for component in group.split(":")
        )
        full_path = f"{group_path}.{tag}"
        root_group = _exiftool_root_group(group)
        if root_group == "file" and tag in {"file_type", "mime_type"}:
            raw_identity[tag] = row[raw_key]
        value = _restore_exiftool_jsonq_value(row[raw_key], tag=tag)
        disposition = _tag_disposition(
            tag,
            adapter="exiftool",
            adapter_group=group,
        )
        # ExifTool's numeric mode is useful for measurements but can coerce
        # numeric-looking identifiers into JSON numbers.  Keep identifier tags
        # lexical so leading zeroes and slash-delimited positions remain data,
        # not arithmetic.
        if (
            tag in _STRING_IDENTIFIER_TAGS
            and value is not None
            and not isinstance(value, (Mapping, list, tuple))
        ):
            value = normalize_metadata_string(value)
        if disposition == "always_transport_omit":
            _record_omission(
                omitted,
                "source_values",
                path=full_path,
                semantic_class="adapter_transport",
            )
            continue
        if accepted >= MAX_SOURCE_VALUES:
            _record_omission(
                omitted,
                "source_values",
                path=full_path,
                semantic_class="source_value_limit",
            )
            continue
        namespace = _source_namespace(group, tag)
        bounded = _bounded_tag_value(
            value,
            omitted,
            path=f"sources.{namespace}.{full_path}",
        )
        if bounded is not _POLICY_OMITTED:
            if _store_source_value(
                sources[namespace],
                full_path,
                bounded,
                omitted,
                path=f"sources.{namespace}.{full_path}",
            ):
                accepted += 1
    identity = _format_from_tool(
        str(raw_identity.get("file_type") or ""),
        str(raw_identity.get("mime_type") or ""),
    )
    if identity:
        kind, fmt, mime = identity
        recognitions.append(
            _recognition(kind, fmt, mime, offset=0, confidence=85, source="exiftool")
        )
    return _AdapterResult(
        name="exiftool",
        outcome="success",
        available=True,
        version=version,
        sources={name: values for name, values in sources.items() if values},
        recognitions=recognitions,
        omitted=omitted,
    )


def _finite_decimal(value: Any, *, places: int = 6) -> float | None:
    if value is None or value == "":
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite() or decimal < 0:
        return None
    try:
        quantum = Decimal(1).scaleb(-places)
        rounded = decimal.quantize(quantum, rounding=ROUND_HALF_EVEN)
        result = float(rounded)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any, *, minimum: int = 0) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        return None
    if decimal < minimum or decimal > 9_223_372_036_854_775_807:
        return None
    return int(decimal)


def _rational_rate(value: Any) -> float | None:
    rate = _rational_fraction(value)
    if rate is None:
        return None
    try:
        decimal = Decimal(rate.numerator) / Decimal(rate.denominator)
        rounded = decimal.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
        result = float(rounded)
    except (InvalidOperation, OverflowError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) else None


def _technical_raw_text(value: Any, *, maximum: int = 256) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return None
    text = normalize_metadata_string(value, maximum=maximum).strip()
    return text or None


def _technical_candidate_missing(value: Any) -> bool:
    text = _technical_raw_text(value)
    return text is None or text.upper() in {"N/A", "UNKNOWN"}


def _invalid_technical_candidate(
    *,
    value: Any,
    field: str,
    path: str,
    raw_key: str,
    raw_target: dict[str, str],
    warnings: list[dict[str, Any]],
) -> None:
    if _technical_candidate_missing(value):
        return
    raw = _technical_raw_text(value)
    if raw is not None:
        raw_target[raw_key] = raw
    warnings.append(
        _warning(
            "invalid_value",
            subtype=field,
            source="ffprobe",
            path=path,
            message=f"Invalid ffprobe {field.replace('_', ' ')}",
        )
    )


def _rational_fraction(value: Any) -> Fraction | None:
    if _technical_candidate_missing(value):
        return None
    text = _technical_raw_text(value)
    if text is None:
        return None
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _rotation_value(value: Any) -> int | None:
    if _technical_candidate_missing(value):
        return None
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite():
            return None
        rounded = decimal.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not (
        Decimal(-9_223_372_036_854_775_808)
        <= rounded
        <= Decimal(9_223_372_036_854_775_807)
    ):
        return None
    return int(rounded)


def _bounded_technical_string(value: Any, *, maximum: int = 128) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return None
    return normalize_metadata_string(value, maximum=maximum).strip() or None


def _hdr_side_data_summaries(
    stream: Mapping[str, Any],
    *,
    omitted: dict[str, Any],
    path: str,
) -> list[dict[str, Any]]:
    raw_items = stream.get("side_data_list")
    if not isinstance(raw_items, list):
        return []
    summaries: dict[bytes, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        raw_type = _bounded_technical_string(item.get("side_data_type"))
        lowered = (raw_type or "").lower()
        if "mastering display" in lowered:
            canonical_type = "mastering_display"
        elif "content light" in lowered:
            canonical_type = "content_light_level"
        elif "hdr10+" in lowered or "smpte2094-40" in lowered:
            canonical_type = "hdr10_plus"
        elif "dolby vision" in lowered:
            canonical_type = "dolby_vision"
        elif "ambient viewing" in lowered:
            canonical_type = "ambient_viewing_environment"
        else:
            continue
        values = {
            key: clean
            for key in _HDR_VALUE_FIELDS
            if (clean := _bounded_technical_string(item.get(key))) is not None
        }
        summary = {"type": canonical_type, "values": values}
        summaries[canonical_json_bytes(summary)] = summary
    ordered = sorted(
        summaries.values(),
        key=lambda item: (item["type"], canonical_json_bytes(item["values"])),
    )
    if len(ordered) > MAX_HDR_SIDE_DATA:
        _record_omission(
            omitted,
            "structure_items",
            path=path,
            semantic_class="streams",
            count=len(ordered) - MAX_HDR_SIDE_DATA,
        )
    return ordered[:MAX_HDR_SIDE_DATA]


def _stream_disposition(stream: Mapping[str, Any]) -> dict[str, bool]:
    raw = (
        stream.get("disposition")
        if isinstance(stream.get("disposition"), Mapping)
        else {}
    )
    return {
        "default": bool(raw.get("default")),
        "attached_pic": bool(raw.get("attached_pic")),
        "forced": bool(raw.get("forced")),
    }


def _stream_rotation_candidate(stream: Mapping[str, Any]) -> Any:
    tags = stream.get("tags") if isinstance(stream.get("tags"), Mapping) else {}
    candidate = tags.get("rotate")
    if candidate is None:
        side_data = stream.get("side_data_list")
        if isinstance(side_data, list):
            for item in side_data:
                if isinstance(item, Mapping) and item.get("rotation") is not None:
                    candidate = item.get("rotation")
                    break
    return candidate


_FFPROBE_FORMAT_CONSUMED_FIELDS = frozenset(
    {"bit_rate", "duration", "filename", "format_long_name", "format_name", "tags"}
)
_FFPROBE_STREAM_CONSUMED_FIELDS = frozenset(
    {
        "avg_frame_rate",
        "bit_rate",
        "channels",
        "chroma_location",
        "codec_name",
        "codec_type",
        "color_primaries",
        "color_range",
        "color_space",
        "color_transfer",
        "disposition",
        "duration",
        "field_order",
        "height",
        "index",
        "pix_fmt",
        "r_frame_rate",
        "sample_rate",
        "side_data_list",
        "tags",
        "width",
    }
)
_FFPROBE_CHAPTER_CONSUMED_FIELDS = frozenset({"end_time", "id", "start_time", "tags"})


def _ffprobe_scalar_extras(
    values: Mapping[str, Any],
    *,
    consumed: frozenset[str],
    omitted: dict[str, Any],
    path: str,
    budget: dict[str, int],
) -> dict[str, Any]:
    """Retain normalized, bounded FFprobe scalars not projected elsewhere."""

    candidates: list[tuple[str, Any]] = []
    for raw_key in sorted(
        values,
        key=lambda item: (normalize_metadata_string(item), str(item)),
    ):
        key = _path_component(raw_key)
        if key in consumed:
            continue
        value = values[raw_key]
        if key in _FFPROBE_PAYLOAD_FIELDS:
            _record_omission(
                omitted,
                "binary_values",
                path=f"{path}.{key}",
                semantic_class="binary",
            )
            continue
        if value is not None and not isinstance(value, (bool, int, float, str)):
            continue
        candidates.append((key, value))

    out: dict[str, Any] = {}
    for position, (key, value) in enumerate(candidates):
        if budget.get("seen", 0) >= MAX_SOURCE_VALUES:
            _record_omission(
                omitted,
                "source_values",
                path=path,
                semantic_class="source_value_limit",
                count=len(candidates) - position,
            )
            break
        budget["seen"] = budget.get("seen", 0) + 1
        bounded = _bounded_tag_value(
            value,
            omitted,
            path=f"{path}.{key}",
        )
        if bounded is not _POLICY_OMITTED:
            _store_source_value(
                out,
                key,
                bounded,
                omitted,
                path=f"{path}.{key}",
            )
    return out


def _safe_tag_map(
    tags: Any,
    *,
    omitted: dict[str, Any],
    path: str,
    budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    if not isinstance(tags, Mapping):
        return {}
    budget = budget if budget is not None else {"seen": 0}
    out: dict[str, Any] = {}
    ordered = sorted(
        tags,
        key=lambda item: (normalize_metadata_string(item), str(item)),
    )
    for position, raw_key in enumerate(ordered):
        if budget.get("seen", 0) >= MAX_SOURCE_VALUES:
            _record_omission(
                omitted,
                "source_values",
                path=path,
                semantic_class="source_value_limit",
                count=len(ordered) - position,
            )
            break
        budget["seen"] = budget.get("seen", 0) + 1
        key = _path_component(raw_key)
        bounded = _bounded_tag_value(
            tags[raw_key],
            omitted,
            path=f"{path}.{key}",
        )
        if bounded is not _POLICY_OMITTED:
            _store_source_value(
                out,
                key,
                bounded,
                omitted,
                path=f"{path}.{key}",
            )
    return out


def _format_from_ffprobe(
    format_name: Any, streams: list[Mapping[str, Any]]
) -> tuple[str, str | None, str | None] | None:
    names = {
        part.strip().lower()
        for part in normalize_metadata_string(format_name or "", maximum=1024).split(
            ","
        )
        if part.strip()
    }
    has_video = any(
        stream.get("codec_type") == "video"
        and not _stream_disposition(stream)["attached_pic"]
        for stream in streams
    )
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    kind = "video" if has_video else "audio" if has_audio else "other"
    if "mp3" in names and not any(
        stream.get("codec_type") == "audio"
        and normalize_metadata_string(stream.get("codec_name") or "").lower() == "mp3"
        for stream in streams
    ):
        # FFmpeg's `mp3` demuxer also carries MPEG Layer I/II. V1 only claims
        # canonical MP3, so demuxer identity requires exact codec evidence.
        names.remove("mp3")
    families = MEDIA_METADATA_FORMAT_REGISTRY["ffprobe_families"]
    matroska_names = set(families["matroska"])
    if names & matroska_names:
        return (
            kind,
            "webm" if "webm" in names else "matroska",
            "video/webm"
            if kind == "video" and "webm" in names
            else "audio/webm"
            if "webm" in names
            else "video/x-matroska"
            if kind == "video"
            else "audio/x-matroska",
        )
    if names & set(families["iso_bmff"]):
        return (kind, "mp4", "video/mp4" if kind == "video" else "audio/mp4")
    aliases = {
        "aac": ("audio", "aac", "audio/aac"),
        "mp3": ("audio", "mp3", "audio/mpeg"),
        "flac": ("audio", "flac", "audio/flac"),
        "wav": ("audio", "wav", "audio/wav"),
        "ogg": (kind, "ogg", "video/ogg" if kind == "video" else "audio/ogg"),
        "avi": (
            kind if kind in {"audio", "video"} else "video",
            "avi",
            "video/x-msvideo",
        ),
        "mpeg": (
            kind if kind in {"audio", "video"} else "video",
            "mpeg",
            "audio/mpeg" if kind == "audio" else "video/mpeg",
        ),
        "mpegts": (
            kind if kind in {"audio", "video"} else "video",
            "mpegts",
            "video/mp2t",
        ),
        "mxf": (
            kind if kind in {"audio", "video"} else "video",
            "mxf",
            "application/mxf",
        ),
    }
    for name in sorted(names & set(families["aliases"])):
        if name in aliases:
            return aliases[name]
    # FFmpeg supports many valid containers beyond the canonical projection
    # registry (ASF/WMA/WMV and FLV are common examples). Stream topology is
    # still authoritative evidence for the media kind. Preserve the bounded
    # raw demuxer name under ``sources.ffprobe.format_name`` and deliberately
    # leave the normalized canonical format/MIME null.
    return (kind, None, None) if kind in {"audio", "video"} else None


def _ffprobe_has_combined_iso_bmff_identity(result: _AdapterResult) -> bool:
    source = result.sources.get("ffprobe")
    if not isinstance(source, Mapping):
        return False
    names = {
        part.strip().lower()
        for part in normalize_metadata_string(
            source.get("format_name") or "", maximum=1024
        ).split(",")
        if part.strip()
    }
    iso_names = set(MEDIA_METADATA_FORMAT_REGISTRY["ffprobe_families"]["iso_bmff"])
    return len(names & iso_names) > 1


def _artwork_descriptor_from_stream(
    stream: Mapping[str, Any], *, position: int
) -> dict[str, Any] | None:
    """Project one sanitized attached-picture stream into a byte-free summary."""

    disposition = stream.get("disposition")
    if not isinstance(disposition, Mapping) or not disposition.get("attached_pic"):
        return None
    codec = normalize_metadata_string(stream.get("codec") or "", maximum=64).lower()
    mime = MEDIA_METADATA_FORMAT_REGISTRY["artwork_codec_mimes"].get(codec)
    tags = stream.get("tags") if isinstance(stream.get("tags"), Mapping) else {}
    description = next(
        (
            normalize_metadata_string(value, maximum=1024).strip()
            for key in ("title", "description")
            if isinstance((value := tags.get(key)), str) and value.strip()
        ),
        None,
    )
    return {
        "source_index": _integer(stream.get("index")),
        "type": "attached_pic",
        "mime": mime,
        "width_pixels": _integer(stream.get("width_pixels"), minimum=1),
        "height_pixels": _integer(stream.get("height_pixels"), minimum=1),
        "description": description,
        "source_paths": [f"structure.streams.{position}"],
    }


def _ffprobe_adapter(
    path: Path,
    *,
    timeout: float = MAX_ADAPTER_SECONDS,
    cancel_event: threading.Event | None = None,
) -> _AdapterResult:
    executable = _resolve_ffprobe_path()
    if executable is None:
        return _AdapterResult(
            name="ffprobe",
            outcome="missing_dependency",
            available=False,
            warnings=[
                _warning(
                    "missing_dependency",
                    source="ffprobe",
                    message="ffprobe is unavailable",
                )
            ],
        )
    adapter_started = time.monotonic()
    version = _tool_version(
        executable,
        "ffprobe",
        min(5.0, max(0.01, float(timeout))),
        cancel_event,
    )
    remaining = _adapter_remaining_seconds(adapter_started, timeout)
    if version is None:
        fd_support = _cached_ffprobe_fd_input_support(executable)
        if cancel_event is not None and cancel_event.is_set():
            outcome = "cancelled"
            message = "ffprobe was cancelled during its identity probe"
        elif remaining <= 0:
            outcome = "timeout"
            message = "ffprobe exceeded its time limit during its identity probe"
        elif fd_support is False:
            outcome = "missing_dependency"
            message = "ffprobe lacks the required seekable fd input protocol"
        else:
            outcome = "missing_dependency"
            message = "ffprobe failed its version or required fd capability probe"
        return _AdapterResult(
            name="ffprobe",
            outcome=outcome,
            available=False,
            warnings=[_warning(outcome, source="ffprobe", message=message)],
        )
    try:
        if cancel_event is not None and cancel_event.is_set():
            result = _CommandResult(-1, b"", b"", cancelled=True)
        elif remaining <= 0:
            result = _CommandResult(-1, b"", b"", timed_out=True)
        else:
            result = _run_bounded(
                [
                    str(executable),
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "fd",
                    "-fd",
                    "0",
                    "-show_format",
                    "-show_streams",
                    "-show_chapters",
                    "-of",
                    "json",
                    "-i",
                    "fd:",
                ],
                timeout=remaining,
                cancel_event=cancel_event,
                stdin_path=path,
            )
    except (OSError, subprocess.SubprocessError):
        return _AdapterResult(
            name="ffprobe",
            outcome="internal_error",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "internal_error",
                    source="ffprobe",
                    message="ffprobe could not be started",
                )
            ],
        )
    if result.cancelled:
        return _AdapterResult(
            name="ffprobe",
            outcome="cancelled",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "cancelled",
                    source="ffprobe",
                    message="ffprobe was cancelled",
                )
            ],
        )
    if result.timed_out:
        return _AdapterResult(
            name="ffprobe",
            outcome="timeout",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "timeout",
                    source="ffprobe",
                    message="ffprobe exceeded its time limit",
                )
            ],
        )
    if result.output_limited:
        return _AdapterResult(
            name="ffprobe",
            outcome="resource_limit",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "resource_limit",
                    source="ffprobe",
                    message="ffprobe output exceeded its limit",
                )
            ],
        )
    if result.returncode != 0:
        return _AdapterResult(
            name="ffprobe",
            outcome="corrupt",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "corrupt",
                    source="ffprobe",
                    message="ffprobe could not parse the file",
                )
            ],
        )
    try:
        body = _decode_adapter_json(result.stdout, empty="{}")
        if not isinstance(body, Mapping):
            raise _AdapterJSONError("ffprobe returned an unexpected JSON shape")
    except _AdapterJSONError:
        return _AdapterResult(
            name="ffprobe",
            outcome="internal_error",
            available=True,
            version=version,
            warnings=[
                _warning(
                    "internal_error",
                    source="ffprobe",
                    message="ffprobe returned invalid JSON",
                )
            ],
        )
    raw_streams = body.get("streams") if isinstance(body.get("streams"), list) else []
    streams: list[Mapping[str, Any]] = [
        stream for stream in raw_streams if isinstance(stream, Mapping)
    ]
    fmt = body.get("format") if isinstance(body.get("format"), Mapping) else {}
    omitted = _empty_omitted()
    warnings: list[dict[str, Any]] = []
    tag_budget = {"seen": 0}
    if fmt.get("filename") is not None:
        _record_omission(
            omitted,
            "source_values",
            path="sources.ffprobe.extras.filename",
            semantic_class="adapter_transport",
        )
    format_tags = _safe_tag_map(
        fmt.get("tags"),
        omitted=omitted,
        path="format.tags",
        budget=tag_budget,
    )
    format_extras = _ffprobe_scalar_extras(
        fmt,
        consumed=_FFPROBE_FORMAT_CONSUMED_FIELDS,
        omitted=omitted,
        path="sources.ffprobe.extras",
        budget=tag_budget,
    )
    structure_streams: list[dict[str, Any]] = []
    for stream in sorted(streams, key=lambda item: _integer(item.get("index")) or 0):
        if len(structure_streams) >= MAX_STREAMS:
            _record_omission(
                omitted,
                "structure_items",
                path="structure.streams",
                semantic_class="streams",
            )
            continue
        position = len(structure_streams)
        raw_technical: dict[str, str] = {}

        def parsed(
            raw_key: str,
            parser: Any,
            *,
            subtype: str | None = None,
        ) -> Any:
            value = stream.get(raw_key)
            result = parser(value)
            if result is None:
                _invalid_technical_candidate(
                    value=value,
                    field=subtype or raw_key,
                    path=f"structure.streams.{position}.raw_technical.{raw_key}",
                    raw_key=raw_key,
                    raw_target=raw_technical,
                    warnings=warnings,
                )
            return result

        index = parsed("index", _integer, subtype="stream_index")
        codec_type = (
            _bounded_technical_string(stream.get("codec_type"), maximum=64) or "other"
        )
        stream_tags = _safe_tag_map(
            stream.get("tags"),
            omitted=omitted,
            path=f"streams.{index}.tags",
            budget=tag_budget,
        )
        stream_extras = _ffprobe_scalar_extras(
            stream,
            consumed=_FFPROBE_STREAM_CONSUMED_FIELDS,
            omitted=omitted,
            path=f"structure.streams.{position}.extras",
            budget=tag_budget,
        )
        raw_language = stream_tags.get("language")
        avg_frame_rate = _technical_raw_text(stream.get("avg_frame_rate"))
        r_frame_rate = _technical_raw_text(stream.get("r_frame_rate"))
        avg_rate = _rational_fraction(avg_frame_rate)
        real_video = (
            codec_type == "video" and not _stream_disposition(stream)["attached_pic"]
        )
        if real_video and avg_rate is None:
            _invalid_technical_candidate(
                value=stream.get("avg_frame_rate"),
                field="avg_frame_rate",
                path=f"structure.streams.{position}.avg_frame_rate",
                raw_key="avg_frame_rate",
                raw_target=raw_technical,
                warnings=warnings,
            )
        r_rate = _rational_fraction(r_frame_rate)
        if real_video and r_rate is None:
            _invalid_technical_candidate(
                value=stream.get("r_frame_rate"),
                field="r_frame_rate",
                path=f"structure.streams.{position}.r_frame_rate",
                raw_key="r_frame_rate",
                raw_target=raw_technical,
                warnings=warnings,
            )
        vfr_evidence = (
            avg_rate != r_rate
            if real_video and avg_rate is not None and r_rate is not None
            else None
        )
        rotation_candidate = _stream_rotation_candidate(stream)
        rotation = _rotation_value(rotation_candidate)
        if rotation is None:
            _invalid_technical_candidate(
                value=rotation_candidate,
                field="rotation",
                path=f"structure.streams.{position}.raw_technical.rotation",
                raw_key="rotation",
                raw_target=raw_technical,
                warnings=warnings,
            )
        descriptor = {
            "index": index,
            "type": codec_type,
            "codec": _bounded_technical_string(stream.get("codec_name"), maximum=256),
            "disposition": _stream_disposition(stream),
            "language": (
                normalize_metadata_string(raw_language, maximum=256).strip() or None
                if isinstance(raw_language, str)
                else None
            ),
            "duration_seconds": parsed("duration", _finite_decimal),
            "bit_rate_bps": parsed(
                "bit_rate", lambda value: _integer(value, minimum=1)
            ),
            "width_pixels": parsed("width", lambda value: _integer(value, minimum=1)),
            "height_pixels": parsed("height", lambda value: _integer(value, minimum=1)),
            "sample_rate_hz": parsed(
                "sample_rate", lambda value: _integer(value, minimum=1)
            ),
            "channel_count": parsed(
                "channels", lambda value: _integer(value, minimum=1)
            ),
            "avg_frame_rate": avg_frame_rate,
            "r_frame_rate": r_frame_rate,
            "vfr_evidence": vfr_evidence,
            **{
                output_key: _bounded_technical_string(stream.get(input_key))
                for output_key, input_key in (
                    ("pixel_format", "pix_fmt"),
                    ("color_range", "color_range"),
                    ("color_space", "color_space"),
                    ("color_transfer", "color_transfer"),
                    ("color_primaries", "color_primaries"),
                    ("chroma_location", "chroma_location"),
                    ("field_order", "field_order"),
                )
            },
            "hdr_side_data": _hdr_side_data_summaries(
                stream,
                omitted=omitted,
                path=f"structure.streams.{position}.hdr_side_data",
            ),
            "rotation_degrees": rotation,
            "raw_technical": raw_technical,
            "tags": stream_tags,
            "extras": stream_extras,
            # JSON paths address array positions, not ffprobe's possibly sparse
            # global stream identifiers.  ``index`` remains available above as
            # the source's stable global stream ID.
            "source_paths": [f"structure.streams.{position}"],
        }
        structure_streams.append(descriptor)
    artwork: list[dict[str, Any]] = []
    for position, stream in enumerate(structure_streams):
        descriptor = _artwork_descriptor_from_stream(stream, position=position)
        if descriptor is None:
            continue
        if len(artwork) >= MAX_ARTWORK:
            _record_omission(
                omitted,
                "structure_items",
                path="structure.artwork",
                semantic_class="artwork",
            )
            continue
        artwork.append(descriptor)
    artwork.sort(
        key=lambda item: (
            item["source_index"] is None,
            item["source_index"] if item["source_index"] is not None else 0,
            item["type"] or "",
            item["mime"] or "",
            item["description"] or "",
        )
    )
    chapters: list[dict[str, Any]] = []
    raw_chapters = (
        body.get("chapters") if isinstance(body.get("chapters"), list) else []
    )
    for position, chapter in enumerate(raw_chapters):
        if not isinstance(chapter, Mapping):
            continue
        if len(chapters) >= MAX_CHAPTERS:
            _record_omission(
                omitted,
                "structure_items",
                path="structure.chapters",
                semantic_class="chapters",
            )
            continue
        tags = _safe_tag_map(
            chapter.get("tags"),
            omitted=omitted,
            path=f"chapters.{position}.tags",
            budget=tag_budget,
        )
        chapter_extras = _ffprobe_scalar_extras(
            chapter,
            consumed=_FFPROBE_CHAPTER_CONSUMED_FIELDS,
            omitted=omitted,
            path=f"structure.chapters.{position}.extras",
            budget=tag_budget,
        )
        chapters.append(
            {
                "index": _integer(chapter.get("id"))
                if chapter.get("id") is not None
                else position,
                "start_seconds": _finite_decimal(chapter.get("start_time")),
                "end_seconds": _finite_decimal(chapter.get("end_time")),
                "title": tags.get("title"),
                "language": tags.get("language"),
                "tags": tags,
                "extras": chapter_extras,
            }
        )
    format_raw_technical: dict[str, str] = {}
    format_duration = _finite_decimal(fmt.get("duration"))
    if format_duration is None:
        _invalid_technical_candidate(
            value=fmt.get("duration"),
            field="duration",
            path="sources.ffprobe.raw_technical.duration",
            raw_key="duration",
            raw_target=format_raw_technical,
            warnings=warnings,
        )
    format_bit_rate = _integer(fmt.get("bit_rate"), minimum=1)
    if format_bit_rate is None:
        _invalid_technical_candidate(
            value=fmt.get("bit_rate"),
            field="bit_rate",
            path="sources.ffprobe.raw_technical.bit_rate",
            raw_key="bit_rate",
            raw_target=format_raw_technical,
            warnings=warnings,
        )
    source = {
        "format_name": normalize_metadata_string(fmt.get("format_name") or "") or None,
        "format_long_name": normalize_metadata_string(fmt.get("format_long_name") or "")
        or None,
        "duration_seconds": format_duration,
        "bit_rate_bps": format_bit_rate,
        "raw_technical": format_raw_technical,
        "tags": format_tags,
        "extras": format_extras,
    }
    identity = _format_from_ffprobe(fmt.get("format_name"), streams)
    recognitions = []
    if identity:
        kind, canonical_format, mime = identity
        recognitions.append(
            _recognition(
                # Stream topology is authoritative for the media kind of
                # ambiguous containers (audio-only MP4/MOV/WebM/Matroska).
                # Signature bytes identify the container, not whether it has
                # a non-attached video stream.
                kind,
                canonical_format,
                mime,
                offset=0,
                confidence=100 if canonical_format is not None else 80,
                source="ffprobe",
            )
        )
    return _AdapterResult(
        name="ffprobe",
        outcome="success",
        available=True,
        version=version,
        sources={"ffprobe": source},
        structure={
            "streams": structure_streams,
            "chapters": chapters,
            "artwork": artwork,
        },
        recognitions=recognitions,
        warnings=warnings,
        omitted=omitted,
    )


def _pypdf_adapter(
    path: Path,
    *,
    timeout: float = MAX_ADAPTER_SECONDS,
    cancel_event: threading.Event | None = None,
) -> _AdapterResult:
    expected_version = _pypdf_version()
    if expected_version is None:
        return _AdapterResult(
            name="pypdf",
            outcome="missing_dependency",
            available=False,
            warnings=[
                _warning(
                    "missing_dependency",
                    source="pypdf",
                    message="pypdf is unavailable",
                )
            ],
        )
    try:
        result = _run_bounded(
            worker_argv("pypdf", str(path.resolve())),
            timeout=timeout,
            cancel_event=cancel_event,
        )
    except (OSError, subprocess.SubprocessError):
        return _AdapterResult(
            name="pypdf",
            outcome="internal_error",
            available=True,
            warnings=[
                _warning(
                    "internal_error",
                    source="pypdf",
                    message="PDF parser worker could not be started",
                )
            ],
        )
    if result.cancelled:
        return _AdapterResult(
            name="pypdf",
            outcome="cancelled",
            available=True,
            warnings=[
                _warning(
                    "cancelled",
                    source="pypdf",
                    message="PDF parsing was cancelled",
                )
            ],
        )
    if result.timed_out:
        return _AdapterResult(
            name="pypdf",
            outcome="timeout",
            available=True,
            warnings=[
                _warning(
                    "timeout",
                    source="pypdf",
                    message="PDF parsing exceeded its time limit",
                )
            ],
        )
    if result.output_limited:
        return _AdapterResult(
            name="pypdf",
            outcome="resource_limit",
            available=True,
            warnings=[
                _warning(
                    "resource_limit",
                    source="pypdf",
                    message="PDF parser output exceeded its limit",
                )
            ],
        )
    try:
        body = _decode_adapter_json(result.stdout, empty="{}")
        if not isinstance(body, Mapping):
            raise _AdapterJSONError("pypdf returned an unexpected JSON shape")
    except _AdapterJSONError:
        body = {"parse_error": True}
    if body.get("dependency_missing"):
        return _AdapterResult(
            name="pypdf",
            outcome="missing_dependency",
            available=False,
            warnings=[
                _warning(
                    "missing_dependency", source="pypdf", message="pypdf is unavailable"
                )
            ],
        )
    if result.returncode != 0 or body.get("parse_error"):
        return _AdapterResult(
            name="pypdf",
            outcome="corrupt",
            available=True,
            warnings=[
                _warning(
                    "corrupt",
                    source="pypdf",
                    message="PDF parser could not read the file",
                )
            ],
        )
    omitted = _empty_omitted()
    metadata = body.get("metadata") if isinstance(body.get("metadata"), Mapping) else {}
    source: dict[str, Any] = {}
    key_map = {
        "/Title": "title",
        "/Author": "author",
        "/Subject": "subject",
        "/Creator": "creator",
        "/Producer": "producer",
        "/CreationDate": "creation_date",
        "/ModDate": "modify_date",
        "/Keywords": "keywords",
    }
    for raw, clean in key_map.items():
        if metadata.get(raw) is not None:
            bounded = _bounded_tag_value(
                metadata[raw],
                omitted,
                path=f"sources.pdf_info.{clean}",
            )
            if bounded is not _POLICY_OMITTED:
                source[clean] = bounded
    encrypted = bool(body.get("encrypted"))
    source["encrypted"] = encrypted
    page_count = _integer(body.get("page_count"))
    source["page_count"] = page_count
    pages = body.get("pages") if isinstance(body.get("pages"), list) else []
    page_descriptors: list[dict[str, Any]] = []
    bounded_pages = pages[:MAX_PAGE_DESCRIPTORS]
    for position, page in enumerate(bounded_pages):
        if not isinstance(page, Mapping):
            _record_omission(
                omitted,
                "structure_items",
                path=f"structure.pages.descriptors.{position}",
                semantic_class="pages",
            )
            continue
        index = _integer(page.get("index"))
        width = _finite_decimal(page.get("width_points"))
        height = _finite_decimal(page.get("height_points"))
        rotation = _integer(
            page.get("rotation_degrees"), minimum=-9_223_372_036_854_775_808
        )
        if width is None or height is None or rotation is None:
            _record_omission(
                omitted,
                "structure_items",
                path=f"structure.pages.descriptors.{position}",
                semantic_class="pages",
            )
            continue
        page_descriptors.append(
            {
                "index": position if index is None else index,
                "width_points": width,
                "height_points": height,
                "rotation_degrees": rotation,
            }
        )
    # The isolated worker intentionally serializes at most 128 descriptors,
    # but it still reports the document's full page count.  Account for the
    # pages that were clipped inside the worker rather than looking only at
    # the already-bounded list returned over stdout.
    page_descriptor_overflow = max(
        0,
        (page_count or len(pages)) - len(bounded_pages),
    )
    if page_descriptor_overflow:
        _record_omission(
            omitted,
            "structure_items",
            path="structure.pages.descriptors",
            semantic_class="pages",
            count=page_descriptor_overflow,
        )
    worker_version = (
        normalize_metadata_string(body.get("version") or "", maximum=256) or None
    )
    if worker_version != expected_version:
        return _AdapterResult(
            name="pypdf",
            outcome="missing_dependency",
            available=False,
            warnings=[
                _warning(
                    "missing_dependency",
                    source="pypdf",
                    message="pypdf worker loaded a different package version",
                )
            ],
        )
    return _AdapterResult(
        name="pypdf",
        outcome="encrypted" if encrypted else "success",
        available=True,
        version=worker_version,
        sources={"pdf_info": source},
        structure={"pages": {"count": page_count, "descriptors": page_descriptors}},
        warnings=[
            _warning("encrypted", source="pypdf", message="PDF declares encryption")
        ]
        if encrypted
        else [],
        omitted=omitted,
    )


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return (width, height) if width and height else None
    if data.startswith(b"\xff\xd8"):
        index = 2
        sof = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            segment = struct.unpack(">H", data[index : index + 2])[0]
            if marker in sof and index + 7 < len(data):
                height, width = struct.unpack(">HH", data[index + 3 : index + 7])
                return (width, height) if width and height else None
            index += max(segment, 2)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        index = 12
        while index + 8 <= len(data):
            chunk = data[index : index + 4]
            size = struct.unpack("<I", data[index + 4 : index + 8])[0]
            payload = data[index + 8 : index + 8 + size]
            if chunk == b"VP8X" and len(payload) >= 10:
                return 1 + int.from_bytes(payload[4:7], "little"), 1 + int.from_bytes(
                    payload[7:10], "little"
                )
            if (
                chunk == b"VP8 "
                and len(payload) >= 10
                and payload[3:6] == b"\x9d\x01\x2a"
            ):
                width = int.from_bytes(payload[6:8], "little") & 0x3FFF
                height = int.from_bytes(payload[8:10], "little") & 0x3FFF
                return (width, height) if width and height else None
            if chunk == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
                bits = int.from_bytes(payload[1:5], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
            index += 8 + size + size % 2
    return None


def _merge_omitted(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key in (
        "binary_values",
        "truncated_values",
        "source_values",
        "structure_items",
        "lineage_items",
        "warnings_overflow",
        "conflicts_overflow",
    ):
        target[key] = int(target.get(key) or 0) + int(source.get(key) or 0)
    for key in ("paths", "classes"):
        values = _omitted_labels(target, key) | _omitted_labels(source, key)
        _sync_omitted_labels(target, key, values)


def _record_omissions_for_paths(
    omitted: dict[str, Any],
    key: str,
    paths: Iterable[str],
    *,
    semantic_class: str,
) -> None:
    """Record a deterministic batch without repeatedly copying label sets."""

    raw_paths = [str(path) for path in paths if path]
    normalized_paths = {
        normalize_metadata_string(path, maximum=512) for path in raw_paths
    }
    if not normalized_paths:
        return
    # Counters describe omitted values, while the bounded display list contains
    # distinct normalized paths.  Two long paths may collapse to one 512-byte
    # label without changing the exact value count.
    omitted[key] = int(omitted.get(key) or 0) + len(raw_paths)
    all_paths = _omitted_labels(omitted, "paths") | normalized_paths
    _sync_omitted_labels(omitted, "paths", all_paths)
    all_classes = _omitted_labels(omitted, "classes")
    all_classes.add(normalize_metadata_string(semantic_class, maximum=256))
    _sync_omitted_labels(omitted, "classes", all_classes)


def _source_value_leaf_paths(value: Any, *, path: str) -> Iterable[str]:
    """Yield the scalar leaves charged to the global source-value budget."""

    if isinstance(value, Mapping):
        for raw_key in sorted(
            value,
            key=lambda item: (normalize_metadata_string(item), str(item)),
        ):
            yield from _source_value_leaf_paths(
                value[raw_key], path=f"{path}.{raw_key}"
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _source_value_leaf_paths(item, path=f"{path}[{index}]")
        return
    yield path


def _enforce_global_source_value_limit(
    sources: dict[str, dict[str, Any]],
    structure: dict[str, Any],
    omitted: dict[str, Any],
    *,
    reserved_values: int = 0,
) -> int:
    """Apply the 2,048-leaf source-value budget across every adapter.

    Adapter-local bounds protect subprocess parsing in isolation.  This second
    pass is deliberately global so two individually valid adapters cannot
    jointly exceed the envelope contract.  Scalars inside structured maps and
    arrays each consume a slot; empty containers consume none.  When the limit
    is reached, later leaves are removed in canonical namespace/key/container
    order and every removed leaf contributes one exact omission. Space for
    fixed blob identity is reserved and header evidence is charged first so
    vendor keys cannot crowd out facts required by normalization.
    """

    slots: list[
        tuple[str, dict[str, Any], str, tuple[tuple[dict[str, Any], str], ...]]
    ] = []
    blob_values = sources.get("blob")
    protected_blob_keys = ("header_width_pixels", "header_height_pixels")
    if isinstance(blob_values, dict):
        for key in protected_blob_keys:
            if key in blob_values:
                slots.append((f"sources.blob.{key}", blob_values, key, ()))
    tag_namespaces = (
        # Before normalization adds the fixed digest/identity leaves, ``blob``
        # also contains dynamic ExifTool groups which did not map to a named
        # metadata family. Those vendor facts participate in the same global
        # leaf budget as XMP, EXIF, and the other dynamic namespaces.
        "blob",
        "exif",
        "icc",
        "iptc",
        "xmp",
        "id3",
        "vorbis",
        "riff",
        "quicktime",
        "matroska",
    )
    for namespace in tag_namespaces:
        values = sources.get(namespace)
        if not isinstance(values, dict):
            continue
        for key in sorted(values):
            if namespace == "blob" and key in protected_blob_keys:
                continue
            slots.append((f"sources.{namespace}.{key}", values, key, ()))

    ffprobe = sources.get("ffprobe")
    if isinstance(ffprobe, dict):
        for mapping_name in ("tags", "extras"):
            values = ffprobe.get(mapping_name)
            if not isinstance(values, dict):
                continue
            for key in sorted(values):
                slots.append((f"sources.ffprobe.{mapping_name}.{key}", values, key, ()))

    pdf_info = sources.get("pdf_info")
    if isinstance(pdf_info, dict):
        for key in sorted(pdf_info):
            if key not in {"encrypted", "page_count"}:
                slots.append((f"sources.pdf_info.{key}", pdf_info, key, ()))

    for collection_name in ("streams", "chapters"):
        collection = structure.get(collection_name)
        if not isinstance(collection, list):
            continue
        for position, descriptor in enumerate(collection):
            if not isinstance(descriptor, dict):
                continue
            for mapping_name in ("tags", "extras"):
                values = descriptor.get(mapping_name)
                if not isinstance(values, dict):
                    continue
                for key in sorted(values):
                    derivatives: tuple[tuple[dict[str, Any], str], ...] = ()
                    if mapping_name == "tags" and key in {"title", "language"}:
                        if collection_name == "chapters" or key == "language":
                            derivatives = ((descriptor, key),)
                    slots.append(
                        (
                            f"structure.{collection_name}.{position}.{mapping_name}.{key}",
                            values,
                            key,
                            derivatives,
                        )
                    )

    if (
        isinstance(reserved_values, bool)
        or not 0 <= reserved_values < MAX_SOURCE_VALUES
    ):
        raise ValueError("reserved_values must fit within the source-value limit")
    accepted = int(reserved_values)
    omitted_paths: list[str] = []

    def retain(value: Any, *, path: str) -> tuple[bool, Any]:
        nonlocal accepted
        if isinstance(value, Mapping):
            was_empty = not value
            bounded: dict[str, Any] = {}
            for raw_key in sorted(
                value,
                key=lambda item: (normalize_metadata_string(item), str(item)),
            ):
                key = str(raw_key)
                keep, child = retain(value[raw_key], path=f"{path}.{key}")
                if keep:
                    bounded[key] = child
            return (bool(bounded) or was_empty), bounded
        if isinstance(value, (list, tuple)):
            was_empty = not value
            bounded_items: list[Any] = []
            for index, item in enumerate(value):
                keep, child = retain(item, path=f"{path}[{index}]")
                if keep:
                    bounded_items.append(child)
            return (bool(bounded_items) or was_empty), bounded_items
        if accepted < MAX_SOURCE_VALUES:
            accepted += 1
            return True, value
        omitted_paths.append(path)
        return False, None

    for path, container, key, derivatives in slots:
        # A previous root cannot remove a sibling root, but keep this tolerant
        # of repeated derivative references in malformed adapter fixtures.
        if key not in container:
            continue
        keep, bounded = retain(container[key], path=path)
        if keep:
            container[key] = bounded
            for derivative_container, derivative_key in derivatives:
                derivative_container[derivative_key] = bounded
            continue
        del container[key]
        for derivative_container, derivative_key in derivatives:
            derivative_container[derivative_key] = None
    if not omitted_paths:
        return 0
    _record_omissions_for_paths(
        omitted,
        "source_values",
        omitted_paths,
        semantic_class="source_value_limit",
    )
    return len(omitted_paths)


def _find_candidate(
    sources: Mapping[str, Mapping[str, Any]],
    namespaces: Iterable[str],
    aliases: Iterable[str],
) -> tuple[Any, str] | tuple[None, None]:
    ordered_aliases = tuple(aliases)

    def walk(
        values: Mapping[str, Any], *, prefix: str = "", depth: int = 0
    ) -> Iterable[tuple[str, Any]]:
        if depth > 8:
            return
        for raw_key in sorted(values, key=lambda item: normalize_metadata_string(item)):
            key = normalize_metadata_string(raw_key, maximum=256)
            path = f"{prefix}.{key}" if prefix else key
            value = values[raw_key]
            yield path, value
            if isinstance(value, Mapping):
                yield from walk(value, prefix=path, depth=depth + 1)

    for namespace in namespaces:
        values = sources.get(namespace) or {}
        candidates = list(walk(values))
        for alias in ordered_aliases:
            for path, value in candidates:
                if path.rsplit(".", 1)[-1] != alias:
                    continue
                if isinstance(value, list):
                    value = next(
                        (item for item in value if item not in (None, "")), None
                    )
                if value not in (None, "") and not isinstance(value, Mapping):
                    qualified_path = f"sources.{namespace}.{path}"
                    # Lineage and closed date/music evidence cap paths at 1024
                    # characters.  Skipping an overlong candidate preserves
                    # path resolvability; truncating it would manufacture a
                    # provenance pointer that cannot resolve.
                    if len(qualified_path) > 1024:
                        continue
                    return value, qualified_path
    return None, None


def _primary_stream(streams: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    candidates = [stream for stream in streams if stream.get("type") == kind]
    if kind == "video":
        candidates = [
            stream
            for stream in candidates
            if not (stream.get("disposition") or {}).get("attached_pic")
        ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda stream: (
            -int(bool((stream.get("disposition") or {}).get("default"))),
            int(stream.get("index") or 0),
        ),
    )[0]


def _find_stream_candidate(
    stream: Mapping[str, Any] | None,
    aliases: Iterable[str],
    *,
    position: int | None = None,
) -> tuple[Any, str] | tuple[None, None]:
    if not isinstance(stream, Mapping):
        return None, None
    tags = stream.get("tags")
    if not isinstance(tags, Mapping):
        return None, None
    path_index = position if position is not None else stream.get("index")
    for alias in aliases:
        value = tags.get(alias)
        if isinstance(value, list):
            value = next((item for item in value if item not in (None, "")), None)
        if value not in (None, "") and not isinstance(value, Mapping):
            return value, f"structure.streams.{path_index}.tags.{alias}"
    return None, None


_MUSIC_POSITION_RE = re.compile(r"^\s*([0-9]+)(?:\s*/\s*([0-9]+))?\s*$")


def _music_position_integer(value: Any) -> int | None:
    parsed = _integer(value, minimum=1)
    return parsed if parsed is not None and parsed <= MAX_MUSIC_POSITION else None


def _music_position(
    value: Any,
    path: str | None,
    *,
    total_value: Any = None,
    total_path: str | None = None,
    subtype: str,
    warnings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if value in (None, "") or path is None or isinstance(value, (Mapping, list, tuple)):
        return None
    clean_path = normalize_metadata_string(path)
    if len(clean_path) > 1024:
        return None
    raw = normalize_metadata_string(value, maximum=256).strip()
    if not raw:
        return None
    match = _MUSIC_POSITION_RE.fullmatch(raw)
    number: int | None = None
    total: int | None = None
    total_raw: str | None = None
    total_source_path: str | None = None
    valid = match is not None
    if match is not None:
        number = _music_position_integer(match.group(1))
        total = (
            _music_position_integer(match.group(2))
            if match.group(2) is not None
            else None
        )
        valid = number is not None and (
            match.group(2) is None or (total is not None and total >= number)
        )
        if total is not None and number is not None and total < number:
            total = None
    if not valid:
        namespace = path.split(".", 2)[1] if path.startswith("sources.") else "ffprobe"
        warnings.append(
            _warning(
                "invalid_value",
                subtype=subtype,
                source=namespace,
                path=path,
                message=f"Invalid {subtype} number/total pair",
            )
        )
    if match is not None and match.group(2) is None and total_value not in (None, ""):
        if not isinstance(total_value, (Mapping, list, tuple)):
            total_raw = (
                normalize_metadata_string(total_value, maximum=256).strip() or None
            )
        total_source_path = (
            normalize_metadata_string(total_path) if total_path else None
        )
        if total_source_path is not None and len(total_source_path) > 1024:
            total_source_path = None
            total_raw = None
        parsed_total = _music_position_integer(total_raw)
        if parsed_total is not None and number is not None and parsed_total >= number:
            total = parsed_total
        else:
            namespace = (
                total_path.split(".", 2)[1]
                if total_path and total_path.startswith("sources.")
                else "ffprobe"
            )
            warnings.append(
                _warning(
                    "invalid_value",
                    subtype=f"{subtype}_total",
                    source=namespace,
                    path=total_path,
                    message=f"Invalid {subtype} total",
                )
            )
    return {
        "raw": raw,
        "number": number,
        "total": total,
        "source_path": clean_path,
        "total_raw": total_raw,
        "total_source_path": total_source_path,
    }


def _date_precision(value: Any) -> str | None:
    if value in (None, "") or isinstance(value, (Mapping, list, tuple)):
        return None
    text = normalize_metadata_string(value, maximum=256).strip()
    try:
        from datetime import date

        if match := re.fullmatch(r"D:(\d{4})(\d{2})?", text):
            year = int(match.group(1))
            month = int(match.group(2) or "1")
            date(year, month, 1)
            return "month" if match.group(2) else "year"
        if match := re.fullmatch(r"(\d{4})", text):
            year = int(match.group(1))
            date(year, 1, 1)
            return "year"
        if match := re.fullmatch(r"(\d{4})[-:](\d{2})", text):
            year, month = (int(item) for item in match.groups())
            date(year, month, 1)
            return "month"
    except ValueError:
        return None
    normalized = _iso_date(text)
    if normalized is None:
        return None
    if "T" not in normalized:
        return "day"
    clock = normalized.split("T", 1)[1]
    clock = re.split(r"Z|[+-]\d{2}:?\d{2}$", clock, maxsplit=1)[0]
    if "." in clock or "," in clock:
        return "subsecond"
    parts = clock.split(":")
    if len(parts) >= 3:
        return "second"
    if len(parts) == 2:
        return "minute"
    return "hour"


def _iso_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = normalize_metadata_string(value, maximum=256).strip()
    # PDF D:YYYYMMDD... form.
    match = re.match(
        r"^D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?"
        r"(Z|[+-]\d{2}'?\d{2}'?)?$",
        text,
    )
    if match:
        year, month, day, hour, minute, second, offset = match.groups()
        text = f"{year}-{month}-{day}"
        if hour:
            text += f"T{hour}"
            if minute:
                text += f":{minute}"
            if second:
                text += f":{second}"
            if offset:
                if offset == "Z":
                    text += "Z"
                else:
                    digits = offset.replace("'", "")
                    text += f"{digits[:3]}:{digits[3:]}"
    text = text.replace(" ", "T", 1) if re.match(r"^\d{4}:\d{2}:\d{2} ", text) else text
    if re.match(r"^\d{4}:\d{2}:\d{2}", text):
        text = f"{text[:4]}-{text[5:7]}-{text[8:]}"
    # Day-or-finer only; month/year literals remain in their source namespace.
    if not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T)", text):
        return None
    try:
        from datetime import date, datetime

        if "T" in text:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            date.fromisoformat(text)
    except ValueError:
        return None
    return text


def _lineage_entry(
    selected: str | None,
    candidates: list[str] | None = None,
    transforms: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "selected": selected,
        "candidates": sorted(set(candidates or []))[:8],
        "transforms": list(transforms or [])[:8],
    }


def _normalize(
    recognition: Mapping[str, Any] | None,
    sources: dict[str, dict[str, Any]],
    structure: dict[str, Any],
    adapters: Mapping[str, Mapping[str, Any]],
    *,
    digest: str,
    size_bytes: int,
    omitted: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    normalized = _empty_normalized()
    lineage: dict[str, Any] = {"fields": {}}
    warnings: list[dict[str, Any]] = []
    normalized["blob_hash"] = _qualified_digest(digest)
    normalized["size_bytes"] = int(size_bytes)
    sources["blob"]["blob_hash"] = normalized["blob_hash"]
    sources["blob"]["size_bytes"] = normalized["size_bytes"]
    lineage["fields"]["blob_hash"] = _lineage_entry("sources.blob.blob_hash")
    lineage["fields"]["size_bytes"] = _lineage_entry("sources.blob.size_bytes")
    kind = str(recognition.get("kind")) if recognition else "other"
    if kind not in {"image", "audio", "video", "pdf"}:
        kind = "other"
    normalized["kind"] = kind
    if recognition:
        normalized["format"] = recognition.get("format")
        normalized["mime"] = recognition.get("mime")
        sources["blob"]["detected_kind"] = normalized["kind"]
        sources["blob"]["detected_format"] = normalized["format"]
        sources["blob"]["detected_mime"] = normalized["mime"]
        lineage["fields"]["kind"] = _lineage_entry("sources.blob.detected_kind")
        lineage["fields"]["format"] = _lineage_entry("sources.blob.detected_format")
        lineage["fields"]["mime"] = _lineage_entry("sources.blob.detected_mime")
    streams = (
        structure.get("streams") if isinstance(structure.get("streams"), list) else []
    )
    primary_audio = _primary_stream(streams, "audio")
    primary_video = _primary_stream(streams, "video")
    primary_audio_position = next(
        (
            position
            for position, stream in enumerate(streams)
            if stream is primary_audio
        ),
        None,
    )
    primary_video_position = next(
        (
            position
            for position, stream in enumerate(streams)
            if stream is primary_video
        ),
        None,
    )
    if primary_audio:
        sources["ffprobe"]["primary_audio_index"] = primary_audio.get("index")
        normalized["audio_codec"] = primary_audio.get("codec")
        normalized["sample_rate_hz"] = primary_audio.get("sample_rate_hz")
        normalized["channel_count"] = primary_audio.get("channel_count")
        for role, key in (
            ("audio_codec", "codec"),
            ("sample_rate_hz", "sample_rate_hz"),
            ("channel_count", "channel_count"),
        ):
            if normalized[role] is not None:
                lineage["fields"][role] = _lineage_entry(
                    f"structure.streams.{primary_audio_position}.{key}"
                )
    if primary_video:
        sources["ffprobe"]["primary_video_index"] = primary_video.get("index")
        normalized["video_codec"] = primary_video.get("codec")
        normalized["frame_rate_fps"] = _rational_rate(
            primary_video.get("avg_frame_rate")
        )
        width = primary_video.get("width_pixels")
        height = primary_video.get("height_pixels")
        rotation = primary_video.get("rotation_degrees")
        width_key = "width_pixels"
        height_key = "height_pixels"
        if (
            width is not None
            and height is not None
            and rotation is not None
            and abs(rotation) % 180 == 90
        ):
            width, height = height, width
            width_key, height_key = height_key, width_key
        normalized["width_pixels"] = width
        normalized["height_pixels"] = height
        for role, key in (
            ("video_codec", "codec"),
            ("frame_rate_fps", "avg_frame_rate"),
            ("width_pixels", width_key),
            ("height_pixels", height_key),
        ):
            if normalized[role] is not None:
                transforms = (
                    ["display_rotation"]
                    if role in {"width_pixels", "height_pixels"}
                    and rotation is not None
                    else []
                )
                lineage["fields"][role] = _lineage_entry(
                    f"structure.streams.{primary_video_position}.{key}",
                    candidates=(
                        [f"structure.streams.{primary_video_position}.rotation_degrees"]
                        if transforms
                        else []
                    ),
                    transforms=transforms,
                )
    if kind == "image":
        width, width_path = _find_candidate(
            sources,
            ("blob", "exif"),
            ("header_width_pixels", "image_width", "exif_image_width"),
        )
        height, height_path = _find_candidate(
            sources,
            ("blob", "exif"),
            ("header_height_pixels", "image_height", "exif_image_height"),
        )
        width_int = _integer(width, minimum=1)
        height_int = _integer(height, minimum=1)
        orientation, orientation_path = _find_candidate(
            sources, ("exif",), ("orientation",)
        )
        orientation_int = _integer(orientation, minimum=1)
        if (
            width_int is not None
            and height_int is not None
            and orientation_int in {5, 6, 7, 8}
        ):
            width_int, height_int = height_int, width_int
            width_path, height_path = height_path, width_path
        normalized["width_pixels"] = width_int
        normalized["height_pixels"] = height_int
        if width_int is not None:
            lineage["fields"]["width_pixels"] = _lineage_entry(
                width_path,
                candidates=[orientation_path] if orientation_path else [],
                transforms=["exif_orientation"] if orientation_path else [],
            )
        if height_int is not None:
            lineage["fields"]["height_pixels"] = _lineage_entry(
                height_path,
                candidates=[orientation_path] if orientation_path else [],
                transforms=["exif_orientation"] if orientation_path else [],
            )
    ffprobe = sources.get("ffprobe") or {}
    duration = _finite_decimal(ffprobe.get("duration_seconds"))
    duration_path = "sources.ffprobe.duration_seconds" if duration is not None else None
    stream_durations: list[tuple[int, float, str]] = []
    seen_duration_positions: set[int] = set()
    for position, stream in (
        (primary_audio_position, primary_audio),
        (primary_video_position, primary_video),
    ):
        if position is None or position in seen_duration_positions or not stream:
            continue
        seen_duration_positions.add(position)
        stream_duration = _finite_decimal(stream.get("duration_seconds"))
        if stream_duration is not None:
            stream_durations.append(
                (
                    position,
                    stream_duration,
                    f"structure.streams.{position}.duration_seconds",
                )
            )
    if duration is None and stream_durations:
        _, duration, duration_path = max(
            stream_durations, key=lambda item: (item[1], -item[0])
        )
    elif duration is not None:
        for _, stream_duration, stream_path in stream_durations:
            tolerance = max(1.0, max(duration, stream_duration) * 0.01)
            if abs(duration - stream_duration) > tolerance:
                warnings.append(
                    _warning(
                        "conflict",
                        subtype="duration",
                        source="ffprobe",
                        path=stream_path,
                        message=(
                            "Container and selected stream durations diverge "
                            "beyond tolerance"
                        ),
                    )
                )
    normalized["duration_seconds"] = duration
    if duration is not None:
        lineage["fields"]["duration_seconds"] = _lineage_entry(
            duration_path,
            candidates=[item[2] for item in stream_durations],
        )
    bitrate = _integer(ffprobe.get("bit_rate_bps"))
    bitrate_path = "sources.ffprobe.bit_rate_bps" if bitrate is not None else None
    if bitrate is None and duration is not None and duration >= 0.000001:
        computed = Decimal(size_bytes * 8) / Decimal(str(duration))
        if computed <= Decimal(9_223_372_036_854_775_807):
            bitrate = int(computed.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
            bitrate_path = "sources.blob.size_bytes"
    normalized["bitrate_bps"] = bitrate
    if bitrate is not None:
        computed_bitrate = bitrate_path == "sources.blob.size_bytes"
        lineage["fields"]["bitrate_bps"] = _lineage_entry(
            bitrate_path,
            candidates=[duration_path] if computed_bitrate and duration_path else [],
            transforms=["computed_bitrate"] if computed_bitrate else [],
        )

    descriptive_order = {
        "image": ("xmp", "iptc", "exif"),
        "audio": ("id3", "vorbis", "riff", "quicktime", "ffprobe"),
        "video": ("quicktime", "xmp", "matroska", "ffprobe"),
        "pdf": ("xmp", "pdf_info"),
        "other": tuple(),
    }[kind]
    track_value: Any = None
    track_path: str | None = None
    track_total_value: Any = None
    track_total_path: str | None = None
    disc_value: Any = None
    disc_path: str | None = None
    disc_total_value: Any = None
    disc_total_path: str | None = None
    if kind == "audio":
        music_namespaces = ("id3", "vorbis", "riff", "ffprobe")
        track_value, track_path = _find_candidate(
            sources, music_namespaces, ("track", "track_number", "tracknumber")
        )
        track_total_value, track_total_path = _find_candidate(
            sources,
            music_namespaces,
            ("track_total", "tracktotal", "total_tracks", "totaltracks"),
        )
        disc_value, disc_path = _find_candidate(
            sources,
            music_namespaces,
            ("disc", "disc_number", "discnumber"),
        )
        disc_total_value, disc_total_path = _find_candidate(
            sources,
            music_namespaces,
            ("disc_total", "disctotal", "total_discs", "totaldiscs"),
        )
        if track_value is None:
            track_value, track_path = _find_stream_candidate(
                primary_audio,
                ("track", "track_number", "tracknumber"),
                position=primary_audio_position,
            )
        if track_total_value is None:
            track_total_value, track_total_path = _find_stream_candidate(
                primary_audio,
                ("track_total", "tracktotal", "total_tracks", "totaltracks"),
                position=primary_audio_position,
            )
        if disc_value is None:
            disc_value, disc_path = _find_stream_candidate(
                primary_audio,
                ("disc", "disc_number", "discnumber"),
                position=primary_audio_position,
            )
        if disc_total_value is None:
            disc_total_value, disc_total_path = _find_stream_candidate(
                primary_audio,
                ("disc_total", "disctotal", "total_discs", "totaldiscs"),
                position=primary_audio_position,
            )
    structure["music"] = {
        "track": _music_position(
            track_value,
            track_path,
            total_value=track_total_value,
            total_path=track_total_path,
            subtype="track",
            warnings=warnings,
        ),
        "disc": _music_position(
            disc_value,
            disc_path,
            total_value=disc_total_value,
            total_path=disc_total_path,
            subtype="disc",
            warnings=warnings,
        ),
    }
    title, title_path = _find_candidate(
        sources, descriptive_order, ("title", "object_name", "headline")
    )
    creator, creator_path = _find_candidate(
        sources, descriptive_order, ("artist", "author", "creator", "by_line")
    )
    album, album_path = _find_candidate(sources, descriptive_order, ("album",))
    descriptive_stream = (
        primary_audio if kind == "audio" else primary_video if kind == "video" else None
    )
    if title is None:
        title, title_path = _find_stream_candidate(
            descriptive_stream,
            ("title", "object_name", "headline"),
            position=(
                primary_audio_position if kind == "audio" else primary_video_position
            ),
        )
    if creator is None:
        creator, creator_path = _find_stream_candidate(
            descriptive_stream,
            ("artist", "author", "creator", "by_line"),
            position=(
                primary_audio_position if kind == "audio" else primary_video_position
            ),
        )
    if album is None and kind == "audio":
        album, album_path = _find_stream_candidate(
            descriptive_stream, ("album",), position=primary_audio_position
        )
    normalized["title"] = (
        normalize_metadata_string(title).strip() or None if title is not None else None
    )
    normalized["creator"] = (
        normalize_metadata_string(creator).strip() or None
        if creator is not None
        else None
    )
    normalized["album"] = (
        normalize_metadata_string(album).strip() or None
        if album is not None and kind == "audio"
        else None
    )
    for role, path in (
        ("title", title_path),
        ("creator", creator_path),
        ("album", album_path),
    ):
        if normalized[role] is not None:
            lineage["fields"][role] = _lineage_entry(path)
    date_names = {
        "video": ("creation_time", "create_date", "creation_date"),
        "pdf": ("create_date", "creation_date"),
    }.get(kind, tuple())
    date_candidates: list[str] = []
    date_transforms = ["iso8601_parse"]
    if kind == "image":
        date_value: Any = None
        date_path: str | None = None
        for namespaces, aliases in (
            (("exif",), ("date_time_original",)),
            (("xmp",), ("create_date",)),
            (("exif",), ("create_date",)),
        ):
            date_value, date_path = _find_candidate(sources, namespaces, aliases)
            if date_value is not None:
                break
    else:
        date_value, date_path = _find_candidate(sources, descriptive_order, date_names)
        if kind == "audio":
            origination_date, origination_date_path = _find_candidate(
                sources, ("riff",), ("origination_date",)
            )
            origination_time, origination_time_path = _find_candidate(
                sources, ("riff",), ("origination_time",)
            )
            if origination_date is not None and origination_time is not None:
                clean_date = normalize_metadata_string(
                    origination_date, maximum=128
                ).strip()
                clean_time = normalize_metadata_string(
                    origination_time, maximum=128
                ).strip()
                date_value = f"{clean_date}T{clean_time}"
                date_path = origination_date_path
                date_candidates = [
                    path
                    for path in (origination_date_path, origination_time_path)
                    if path
                ]
                date_transforms.insert(0, "bwf_origination_join")
        if kind == "video" and date_value is None:
            date_value, date_path = _find_stream_candidate(
                primary_video, date_names, position=primary_video_position
            )
    normalized["created_at"] = _iso_date(date_value)
    precision = _date_precision(date_value)
    date_source_paths = list(
        dict.fromkeys(path for path in (date_candidates or [date_path]) if path)
    )[:2]
    structure["dates"] = {
        "creation": (
            {
                "raw": normalize_metadata_string(date_value, maximum=256).strip(),
                "precision": precision,
                "source_paths": date_source_paths,
            }
            if precision is not None and date_source_paths
            else None
        )
    }
    if normalized["created_at"] is not None:
        lineage["fields"]["created_at"] = _lineage_entry(
            date_path,
            candidates=date_candidates,
            transforms=date_transforms,
        )
    elif date_value is not None and precision not in {"year", "month"}:
        warnings.append(
            _warning(
                "invalid_value",
                source=(date_path or "").split(".")[1] if date_path else None,
                path=date_path,
                message="Creation date is not a supported ISO-8601 value",
            )
        )
    make: Any = None
    model: Any = None
    make_path: str | None = None
    model_path: str | None = None
    if kind == "video":
        for namespace in ("quicktime", "xmp"):
            candidate_make, candidate_make_path = _find_candidate(
                sources, (namespace,), ("make",)
            )
            candidate_model, candidate_model_path = _find_candidate(
                sources, (namespace,), ("model",)
            )
            if candidate_make is not None and candidate_model is not None:
                make, make_path = candidate_make, candidate_make_path
                model, model_path = candidate_model, candidate_model_path
                break
        if make is None or model is None:
            stream_make, stream_make_path = _find_stream_candidate(
                primary_video, ("make",), position=primary_video_position
            )
            stream_model, stream_model_path = _find_stream_candidate(
                primary_video, ("model",), position=primary_video_position
            )
            if stream_make is not None and stream_model is not None:
                make, make_path = stream_make, stream_make_path
                model, model_path = stream_model, stream_model_path
    elif kind == "image":
        make, make_path = _find_candidate(
            sources, ("xmp", "quicktime", "exif"), ("make",)
        )
        model, model_path = _find_candidate(
            sources, ("xmp", "quicktime", "exif"), ("model",)
        )
    device = normalize_metadata_string(
        " ".join(
            normalize_metadata_string(item).strip()
            for item in (make, model)
            if item not in (None, "")
        ).strip()
    )
    normalized["capture_device"] = device or None
    if device:
        lineage["fields"]["capture_device"] = _lineage_entry(
            make_path or model_path,
            candidates=[path for path in (make_path, model_path) if path],
            transforms=["make_model_join"],
        )
    if kind == "pdf":
        pages = (
            structure.get("pages")
            if isinstance(structure.get("pages"), Mapping)
            else {}
        )
        normalized["page_count"] = _integer(pages.get("count"))
        encrypted, encrypted_path = _find_candidate(
            sources, ("pdf_info",), ("encrypted",)
        )
        normalized["pdf_encrypted"] = bool(encrypted) if encrypted is not None else None
        if normalized["page_count"] is not None:
            lineage["fields"]["page_count"] = _lineage_entry("structure.pages.count")
        if normalized["pdf_encrypted"] is not None:
            lineage["fields"]["pdf_encrypted"] = _lineage_entry(encrypted_path)

    required = {
        "image": ("content_recognizer", "exiftool"),
        "audio": ("content_recognizer", "ffprobe", "exiftool"),
        "video": ("content_recognizer", "ffprobe", "exiftool"),
        "pdf": ("content_recognizer", "pypdf", "exiftool"),
        "other": ("content_recognizer",),
    }[kind]
    outcomes = [
        str((adapters.get(name) or {}).get("outcome") or "not_applicable")
        for name in required
    ]
    if kind == "other":
        status = "unsupported"
    elif all(outcome == "success" for outcome in outcomes):
        status = "ok"
    elif any(
        outcome
        in {
            "timeout",
            "cancelled",
            "resource_limit",
            "internal_error",
            "missing_dependency",
            "corrupt",
            "encrypted",
        }
        for outcome in outcomes
    ):
        usable = bool(normalized.get("format")) and any(
            normalized.get(role) is not None
            for role in (
                "width_pixels",
                "duration_seconds",
                "page_count",
                "pdf_encrypted",
                "title",
                "audio_codec",
                "video_codec",
            )
        )
        status = (
            "partial"
            if usable or any(outcome == "missing_dependency" for outcome in outcomes)
            else "error"
        )
    else:
        status = "partial"
    material_omission = bool(
        _omitted_labels(omitted, "classes") & _MATERIAL_OMISSION_CLASSES
    ) or any(
        int(omitted.get(key) or 0)
        for key in (
            "truncated_values",
            "structure_items",
            "lineage_items",
            "warnings_overflow",
            "conflicts_overflow",
        )
    )
    if material_omission:
        status = "partial" if status == "ok" else status
    normalized["probe_status"] = status
    lineage["fields"]["probe_status"] = _lineage_entry("extraction.adapters")
    # Every role may appear in lineage, but never any other key.
    lineage["fields"] = {
        key: lineage["fields"][key]
        for key in MEDIA_METADATA_ROLES
        if key in lineage["fields"]
    }
    return normalized, lineage, warnings


def _cache_completeness(
    status: str, adapters: Mapping[str, Mapping[str, Any]]
) -> tuple[str, bool]:
    outcomes = {str(value.get("outcome")) for value in adapters.values()}
    if outcomes & {"timeout", "cancelled", "internal_error"}:
        return "retryable_failure", True
    if "missing_dependency" in outcomes:
        return "degraded_missing_dependency", False
    if status == "ok":
        return "complete", False
    if status == "unsupported":
        return "terminal_unsupported", False
    if status in {"partial", "error"}:
        # Corruption, encryption, parser output bounds, and deterministic
        # normalization/envelope bounds are content-stable for this contract.
        return "terminal_partial", False
    return "retryable_failure", True


def _adapter_public(result: _AdapterResult, *, required: bool) -> dict[str, Any]:
    return {
        "required": bool(required),
        "available": bool(result.available),
        "outcome": result.outcome,
        "version": result.version,
    }


def _mark_resource_limited(envelope: dict[str, Any]) -> None:
    normalized = envelope.get("normalized") or {}
    if normalized.get("probe_status") == "ok":
        normalized["probe_status"] = "partial"
    extraction = envelope.get("extraction") or {}
    # Envelope pruning is deterministic and therefore cacheable only when it
    # is the extraction's sole limitation.  Do not let a later normalization
    # bound disguise a transient adapter failure or missing runtime dependency
    # as a reusable terminal result.
    if extraction.get("completeness") in {"complete", "terminal_partial"}:
        extraction["completeness"] = "terminal_partial"
        extraction["retryable"] = False


_REQUIRED_CANDIDATE_TRANSFORMS = frozenset(
    {
        "bwf_origination_join",
        "computed_bitrate",
        "display_rotation",
        "exif_orientation",
        "make_model_join",
    }
)


def _lineage_requires_all_candidates(entry: Mapping[str, Any]) -> bool:
    transforms = {str(value) for value in entry.get("transforms") or []}
    return bool(transforms & _REQUIRED_CANDIDATE_TRANSFORMS)


def _remove_lineage_references(
    envelope: dict[str, Any],
    removed_path: str,
    *,
    prefix: bool = False,
) -> None:
    """Keep normalized values and lineage consistent while pruning details.

    A selected source is part of the promoted value's provenance.  If a hard
    envelope limit eventually forces that source out, the promoted value must
    leave with it; candidate-only references can simply be removed.
    """

    fields = (envelope.get("lineage") or {}).get("fields") or {}

    def removed(path: object) -> bool:
        if not isinstance(path, str):
            return False
        return path == removed_path or (prefix and path.startswith(removed_path + "."))

    removed_paths: list[str] = []
    for role, entry in list(fields.items()):
        if not isinstance(entry, dict):
            continue
        candidates = [
            str(path) for path in entry.get("candidates") or [] if isinstance(path, str)
        ]
        selected = entry.get("selected")
        required_candidate_removed = _lineage_requires_all_candidates(entry) and any(
            removed(path) for path in candidates
        )
        if removed(selected) or required_candidate_removed:
            if role in (envelope.get("normalized") or {}):
                envelope["normalized"][role] = None
            removed_paths.extend(
                dict.fromkeys(
                    path for path in (selected, *candidates) if isinstance(path, str)
                )
            )
            del fields[role]
            continue
        retained_candidates = [path for path in candidates if not removed(path)]
        removed_paths.extend(
            str(path) for path in candidates if path not in retained_candidates
        )
        entry["candidates"] = retained_candidates
    if removed_paths:
        _record_omissions_for_paths(
            envelope["omitted"],
            "lineage_items",
            removed_paths,
            semantic_class="lineage",
        )


def _remove_structured_provenance_references(
    envelope: dict[str, Any],
    removed_path: str,
    *,
    prefix: bool = False,
) -> None:
    """Remove derived music/date evidence whose source no longer exists."""

    def removed(path: object) -> bool:
        if not isinstance(path, str):
            return False
        return path == removed_path or (prefix and path.startswith(removed_path + "."))

    structure = envelope.get("structure")
    if not isinstance(structure, dict):
        return
    removed_references: list[str] = []
    removed_items: list[str] = []

    music = structure.get("music")
    if isinstance(music, dict):
        for position_name in ("track", "disc"):
            position = music.get(position_name)
            if not isinstance(position, dict):
                continue
            source_path = position.get("source_path")
            total_source_path = position.get("total_source_path")
            if removed(source_path):
                removed_references.extend(
                    str(path)
                    for path in (source_path, total_source_path)
                    if isinstance(path, str)
                )
                removed_items.append(f"structure.music.{position_name}")
                music[position_name] = None
            elif removed(total_source_path):
                removed_references.append(str(total_source_path))
                removed_items.append(f"structure.music.{position_name}.total")
                position["total"] = None
                position["total_raw"] = None
                position["total_source_path"] = None

    dates = structure.get("dates")
    if isinstance(dates, dict):
        creation = dates.get("creation")
        if isinstance(creation, dict):
            source_paths = [
                str(path)
                for path in creation.get("source_paths") or []
                if isinstance(path, str)
            ]
            if any(removed(path) for path in source_paths):
                removed_references.extend(source_paths)
                removed_items.append("structure.dates.creation")
                dates["creation"] = None

    if removed_references:
        _record_omissions_for_paths(
            envelope["omitted"],
            "lineage_items",
            removed_references,
            semantic_class="lineage",
        )
    if removed_items:
        _record_omissions_for_paths(
            envelope["omitted"],
            "structure_items",
            removed_items,
            semantic_class="lineage",
        )


def _remove_provenance_references(
    envelope: dict[str, Any],
    removed_path: str,
    *,
    prefix: bool = False,
) -> None:
    _remove_lineage_references(envelope, removed_path, prefix=prefix)
    _remove_structured_provenance_references(envelope, removed_path, prefix=prefix)


def _metadata_path_resolves(document: Mapping[str, Any], path: object) -> bool:
    """Resolve the envelope's dotted provenance paths, including flat tag keys."""

    if not isinstance(path, str) or not path:
        return False
    parts = path.split(".")
    current: Any = document
    position = 0
    while position < len(parts):
        if isinstance(current, Mapping):
            matched_key: str | None = None
            matched_end = position
            # ExifTool source keys are registered dotted paths (for example
            # ``exif.date_time_original``).  Prefer the longest matching key
            # while ordinary object fields continue to resolve one segment.
            for end in range(len(parts), position, -1):
                candidate = ".".join(parts[position:end])
                if candidate in current:
                    matched_key = candidate
                    matched_end = end
                    break
            if matched_key is None:
                return False
            current = current[matched_key]
            position = matched_end
            continue
        if isinstance(current, list):
            try:
                index = int(parts[position])
            except ValueError:
                return False
            if index < 0 or index >= len(current):
                return False
            current = current[index]
            position += 1
            continue
        return False
    return True


def _remove_unresolved_lineage(envelope: dict[str, Any]) -> None:
    fields = (envelope.get("lineage") or {}).get("fields") or {}
    removed_paths: list[str] = []
    for role, entry in list(fields.items()):
        if not isinstance(entry, dict):
            del fields[role]
            continue
        selected = entry.get("selected")
        candidates = [
            str(candidate)
            for candidate in entry.get("candidates") or []
            if isinstance(candidate, str)
        ]
        required_candidate_missing = _lineage_requires_all_candidates(entry) and any(
            not _metadata_path_resolves(envelope, candidate) for candidate in candidates
        )
        if (
            selected is not None and not _metadata_path_resolves(envelope, selected)
        ) or required_candidate_missing:
            if role in (envelope.get("normalized") or {}):
                envelope["normalized"][role] = None
            removed_paths.extend(
                dict.fromkeys(
                    path for path in (selected, *candidates) if isinstance(path, str)
                )
            )
            del fields[role]
            continue
        retained_candidates = [
            candidate
            for candidate in candidates
            if _metadata_path_resolves(envelope, candidate)
        ]
        removed_paths.extend(
            str(candidate)
            for candidate in candidates
            if candidate not in retained_candidates
        )
        entry["candidates"] = retained_candidates
    if removed_paths:
        _record_omissions_for_paths(
            envelope["omitted"],
            "lineage_items",
            removed_paths,
            semantic_class="lineage",
        )
        _mark_resource_limited(envelope)


def _bound_warning_collection(
    envelope: dict[str, Any],
    *,
    required: dict[str, Any] | None = None,
) -> None:
    items = _sort_warnings(
        [*list(envelope.get("warnings") or []), *([required] if required else [])]
    )
    retained = items[:MAX_WARNINGS]
    if required is not None and required not in retained:
        retained = _sort_warnings([*retained[: MAX_WARNINGS - 1], required])
    overflow = len(items) - len(retained)
    if overflow:
        _record_omission(
            envelope["omitted"],
            "warnings_overflow",
            path="warnings",
            semantic_class="warnings",
            count=overflow,
        )
        _mark_resource_limited(envelope)
    envelope["warnings"] = retained


def _bound_conflict_collection(envelope: dict[str, Any]) -> None:
    items = _sort_conflicts(envelope.get("conflicts") or [])
    retained = items[:MAX_CONFLICTS]
    overflow = len(items) - len(retained)
    if overflow:
        _record_omission(
            envelope["omitted"],
            "conflicts_overflow",
            path="conflicts",
            semantic_class="conflicts",
            count=overflow,
        )
        _mark_resource_limited(envelope)
    envelope["conflicts"] = retained


def _resource_limit_warning(*, minimal: bool = False) -> dict[str, Any]:
    return _warning(
        "resource_limit",
        source="normalizer",
        message=(
            "Metadata envelope was reduced to the minimal bounded form"
            if minimal
            else "Metadata envelope was reduced to its size limit"
        ),
    )


def _mark_envelope_reduced(envelope: dict[str, Any]) -> None:
    _mark_resource_limited(envelope)
    _bound_warning_collection(envelope, required=_resource_limit_warning())


def _replace_with_minimal_warning(envelope: dict[str, Any]) -> None:
    required = _resource_limit_warning(minimal=True)
    existing = _sort_warnings(envelope.get("warnings") or [])
    dropped = sum(item != required for item in existing)
    if dropped:
        _record_omission(
            envelope["omitted"],
            "warnings_overflow",
            path="warnings",
            semantic_class="warnings",
            count=dropped,
        )
    envelope["warnings"] = [required]


def _path_is_within(path: object, parent: str) -> bool:
    return isinstance(path, str) and (
        path == parent or path.startswith(parent + ".") or path.startswith(parent + "[")
    )


def _canonical_member_size(key: str, value: Any, *, has_siblings: bool) -> int:
    key_bytes = json.dumps(
        key, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    value_bytes = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(key_bytes) + 1 + len(value_bytes) + int(has_siblings)


def _finalize_bounds(envelope: dict[str, Any]) -> dict[str, Any]:
    warning_overflow_before = int(envelope["omitted"].get("warnings_overflow") or 0)
    conflict_overflow_before = int(envelope["omitted"].get("conflicts_overflow") or 0)
    _bound_warning_collection(envelope)
    _bound_conflict_collection(envelope)
    if (
        int(envelope["omitted"].get("warnings_overflow") or 0) > warning_overflow_before
        or int(envelope["omitted"].get("conflicts_overflow") or 0)
        > conflict_overflow_before
    ):
        # The bookkeeping warning participates in the same bound.  When the
        # collection was already full it displaces one more warning, which is
        # itself included in the exact overflow count.
        _bound_warning_collection(envelope, required=_resource_limit_warning())
    _remove_unresolved_lineage(envelope)
    envelope_size = len(canonical_json_bytes(envelope))
    if envelope_size <= MAX_DETAILS_BYTES:
        return envelope

    # Drop low-frequency source facts in deterministic order first.  Track an
    # exact per-member byte estimate and serialize the whole envelope only at
    # phase boundaries; serializing after every deletion turns hostile tag
    # collections into quadratic work.
    protected_lineage_paths: set[str] = set()
    for entry in ((envelope.get("lineage") or {}).get("fields") or {}).values():
        if not isinstance(entry, Mapping):
            continue
        selected = entry.get("selected")
        if isinstance(selected, str):
            protected_lineage_paths.add(selected)
        if _lineage_requires_all_candidates(entry):
            protected_lineage_paths.update(
                str(path)
                for path in entry.get("candidates") or []
                if isinstance(path, str)
            )
    protected_blob_keys = {
        "blob_hash",
        "size_bytes",
        "detected_kind",
        "detected_format",
        "detected_mime",
        "filename",
        "claimed_mime",
    }
    candidates: list[tuple[str, dict[str, Any], str]] = []
    for namespace in reversed(SOURCE_NAMESPACES):
        values = envelope["sources"].get(namespace) or {}
        for key in sorted(list(values), reverse=True):
            source_path = f"sources.{namespace}.{key}"
            if namespace == "blob" and key in protected_blob_keys:
                continue
            # A selected value can be nested inside a structured source fact.
            # Protect its parent here; the minimal fallback below removes the
            # value and its promotion atomically if even that cannot fit.
            if any(
                _path_is_within(path, source_path) for path in protected_lineage_paths
            ):
                continue
            candidates.append((source_path, values, key))

    required_savings = envelope_size - MAX_DETAILS_BYTES
    # Omission labels and the mandatory warning consume some of the savings.
    # A fixed reserve keeps the common path to one batch and one verification.
    savings_target = required_savings + 12 * 1024
    estimated_savings = 0
    removed_paths: list[str] = []
    removed_members = 0
    next_candidate = 0
    while next_candidate < len(candidates) and estimated_savings < savings_target:
        source_path, values, key = candidates[next_candidate]
        next_candidate += 1
        if key not in values:
            continue
        estimated_savings += _canonical_member_size(
            key, values[key], has_siblings=len(values) > 1
        )
        removed_paths.extend(_source_value_leaf_paths(values[key], path=source_path))
        del values[key]
        _remove_provenance_references(envelope, source_path, prefix=True)
        removed_members += 1
    if removed_members:
        if removed_paths:
            _record_omissions_for_paths(
                envelope["omitted"],
                "source_values",
                removed_paths,
                semantic_class="envelope_limit",
            )
        else:
            _record_omission(
                envelope["omitted"],
                "source_values",
                semantic_class="envelope_limit",
                count=0,
            )
        _mark_envelope_reduced(envelope)
        if len(canonical_json_bytes(envelope)) <= MAX_DETAILS_BYTES:
            return envelope

    # If unusually long omission paths consumed the reserve, remove the rest
    # of the eligible source facts as one more batch before touching structure.
    remaining_paths: list[str] = []
    remaining_members = 0
    for source_path, values, key in candidates[next_candidate:]:
        if key not in values:
            continue
        remaining_paths.extend(_source_value_leaf_paths(values[key], path=source_path))
        del values[key]
        _remove_provenance_references(envelope, source_path, prefix=True)
        remaining_members += 1
    if remaining_members:
        if remaining_paths:
            _record_omissions_for_paths(
                envelope["omitted"],
                "source_values",
                remaining_paths,
                semantic_class="envelope_limit",
            )
        else:
            _record_omission(
                envelope["omitted"],
                "source_values",
                semantic_class="envelope_limit",
                count=0,
            )
        _mark_envelope_reduced(envelope)
        if len(canonical_json_bytes(envelope)) <= MAX_DETAILS_BYTES:
            return envelope

    for key in ("chapters", "artwork", "streams"):
        items = envelope["structure"].get(key)
        if isinstance(items, list) and items:
            _remove_provenance_references(envelope, f"structure.{key}", prefix=True)
            _record_omission(
                envelope["omitted"],
                "structure_items",
                path=f"structure.{key}",
                semantic_class="envelope_limit",
                count=len(items),
            )
            envelope["structure"][key] = []
    pages = envelope["structure"].get("pages")
    if isinstance(pages, dict) and isinstance(pages.get("descriptors"), list):
        descriptors = pages["descriptors"]
        _record_omission(
            envelope["omitted"],
            "structure_items",
            path="structure.pages.descriptors",
            semantic_class="envelope_limit",
            count=len(descriptors),
        )
        pages["descriptors"] = []
    _mark_envelope_reduced(envelope)
    if len(canonical_json_bytes(envelope)) <= MAX_DETAILS_BYTES:
        return envelope

    # Closed minimal envelope.  It is intentionally far below 48 KiB.
    minimal = copy.deepcopy(envelope)
    retained_blob_keys = {
        "blob_hash",
        "size_bytes",
        "detected_kind",
        "detected_format",
        "detected_mime",
        "filename",
        "claimed_mime",
    }
    minimal_source_paths: list[str] = []
    minimal_source_members = 0
    for namespace, values in minimal["sources"].items():
        for key in list(values):
            if namespace != "blob" or key not in retained_blob_keys:
                source_path = f"sources.{namespace}.{key}"
                minimal_source_paths.extend(
                    _source_value_leaf_paths(values[key], path=source_path)
                )
                _remove_provenance_references(minimal, source_path, prefix=True)
                minimal_source_members += 1
    if minimal_source_members:
        if minimal_source_paths:
            _record_omissions_for_paths(
                minimal["omitted"],
                "source_values",
                minimal_source_paths,
                semantic_class="envelope_limit",
            )
        else:
            _record_omission(
                minimal["omitted"],
                "source_values",
                semantic_class="envelope_limit",
                count=0,
            )
    _remove_provenance_references(minimal, "structure.streams", prefix=True)
    _remove_provenance_references(minimal, "structure.chapters", prefix=True)
    _remove_provenance_references(minimal, "structure.artwork", prefix=True)
    _remove_provenance_references(minimal, "structure.pages", prefix=True)
    minimal["sources"] = {name: {} for name in SOURCE_NAMESPACES}
    for key in (
        "blob_hash",
        "size_bytes",
        "detected_kind",
        "detected_format",
        "detected_mime",
        "filename",
        "claimed_mime",
    ):
        if key in envelope["sources"].get("blob", {}):
            minimal["sources"]["blob"][key] = envelope["sources"]["blob"][key]
    minimal["structure"] = {"streams": [], "chapters": [], "artwork": [], "pages": {}}
    if minimal["conflicts"]:
        _record_omission(
            minimal["omitted"],
            "conflicts_overflow",
            path="conflicts",
            semantic_class="conflicts",
            count=len(minimal["conflicts"]),
        )
    minimal["conflicts"] = []
    _replace_with_minimal_warning(minimal)
    for role, value in list(minimal["normalized"].items()):
        if isinstance(value, str):
            cap = (
                1024
                if role in {"title", "creator", "album", "capture_device", "filename"}
                else 256
            )
            truncated, changed = _truncate_utf8(value, cap)
            minimal["normalized"][role] = truncated or None
            source_key = {
                "kind": "detected_kind",
                "format": "detected_format",
                "mime": "detected_mime",
                "filename": "filename",
                "blob_hash": "blob_hash",
            }.get(role)
            if source_key in minimal["sources"]["blob"]:
                minimal["sources"]["blob"][source_key] = truncated
            if changed:
                _record_omission(
                    minimal["omitted"],
                    "truncated_values",
                    path=f"normalized.{role}",
                    semantic_class="truncated_value",
                )
    claimed_mime = minimal["sources"]["blob"].get("claimed_mime")
    if isinstance(claimed_mime, str):
        bounded_claimed_mime, changed = _truncate_utf8(claimed_mime, 256)
        minimal["sources"]["blob"]["claimed_mime"] = bounded_claimed_mime
        if changed:
            _record_omission(
                minimal["omitted"],
                "truncated_values",
                path="sources.blob.claimed_mime",
                semantic_class="truncated_value",
            )
    _mark_resource_limited(minimal)
    _remove_unresolved_lineage(minimal)
    if len(canonical_json_bytes(minimal)) > 48 * 1024:
        # Hostile source keys can make the 64 public omission labels larger
        # than the envelope they describe once JSON escaping is included. The
        # exact private set remains available to the cache sidecar; only the
        # emergency minimal envelope hides its public display prefix.
        all_paths = _omitted_labels(minimal["omitted"], "paths")
        minimal["omitted"]["paths"] = []
        minimal["omitted"]["paths_overflow"] = len(all_paths)
    if len(canonical_json_bytes(minimal)) > 48 * 1024:
        raise RuntimeError("minimal media metadata envelope exceeded its invariant")
    return minimal


def extract_media_metadata(
    path: str | Path,
    *,
    digest: str,
    size_bytes: int,
    filename: str | None,
    claimed_mime: str | None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Extract one deterministic v1 details envelope.

    Pass ``filename=None, claimed_mime=None`` to build a digest-global content
    template suitable for cache/anchor storage.  Per-row callers then use
    :func:`overlay_reference_evidence`.
    """

    qualified_digest = _qualified_digest(digest)
    if isinstance(size_bytes, bool) or int(size_bytes) < 0:
        raise ValueError("size_bytes must be a nonnegative integer")
    source_path = Path(path)
    started = time.monotonic()
    prefix, read_warnings = _read_prefix(source_path)
    recognitions = _recognize_prefix(prefix)
    sources: dict[str, dict[str, Any]] = {name: {} for name in SOURCE_NAMESPACES}
    structure: dict[str, Any] = {
        "streams": [],
        "chapters": [],
        "artwork": [],
        "pages": {},
    }
    warnings: list[dict[str, Any]] = list(read_warnings)
    omitted = _empty_omitted()

    def remaining_adapter_seconds() -> float:
        remaining = MAX_TOTAL_PROBE_SECONDS - (time.monotonic() - started)
        return max(0.0, min(MAX_ADAPTER_SECONDS, remaining))

    exiftool = _exiftool_adapter(
        source_path,
        timeout=max(0.01, remaining_adapter_seconds()),
        cancel_event=cancel_event,
    )
    for parser_identity in exiftool.recognitions:
        recognitions = [
            _parser_kind_corrected_signature_container(signature, parser_identity)
            for signature in recognitions
        ]
    recognitions.extend(exiftool.recognitions)
    for namespace, values in exiftool.sources.items():
        sources[namespace].update(values)
    warnings.extend(exiftool.warnings)
    _merge_omitted(omitted, exiftool.omitted)
    recognition, conflicts = _select_recognition(recognitions)
    kind = str(recognition.get("kind")) if recognition else "other"

    ffprobe = _AdapterResult(
        name="ffprobe",
        outcome="not_applicable",
        # Availability here is only a cheap not-applicable diagnostic. The
        # adapter performs its version probe under the shared deadline and
        # cancellation event if it is actually selected below.
        available=_resolve_ffprobe_path() is not None,
    )
    pypdf = _AdapterResult(
        name="pypdf",
        outcome="not_applicable",
        available=_pypdf_version() is not None,
    )
    image_header = _AdapterResult(
        name="image_header", outcome="not_applicable", available=True
    )
    # Unknown signatures still need one bounded parser pass: formats such as
    # MXF have no short, stable magic probe here, so requiring prior
    # recognition would make ffprobe recognition circular when ExifTool is
    # unavailable. Known image/PDF signatures remain excluded.
    # ffprobe exposes unresolved generic HEIF as ordinary ISO-BMFF video. If
    # ExifTool could not provide an exact image family, retain unsupported/
    # degraded status instead of silently normalizing it to MP4.
    unresolved_bmff_image = _is_unresolved_bmff_image(prefix)
    should_run_ffprobe = kind in {"audio", "video"} or (
        recognition is None and not unresolved_bmff_image
    )
    if (
        remaining_adapter_seconds() > 0
        and not (cancel_event is not None and cancel_event.is_set())
        and should_run_ffprobe
    ):
        ffprobe = _ffprobe_adapter(
            source_path,
            timeout=max(0.01, remaining_adapter_seconds()),
            cancel_event=cancel_event,
        )
        ffprobe_recognitions = list(ffprobe.recognitions)
        if recognition is None and _ffprobe_has_combined_iso_bmff_identity(ffprobe):
            # The combined MOV/MP4 demuxer name does not distinguish an
            # ftyp-less QuickTime file from MP4. Without prior exact container
            # evidence, keep the row unsupported rather than guessing MP4.
            ffprobe_recognitions = [
                candidate
                for candidate in ffprobe_recognitions
                if candidate.get("format") != "mp4"
            ]
        topology_candidates: list[dict[str, Any]] = []
        for candidate in ffprobe_recognitions:
            recognitions = [
                _signature_with_stream_topology(signature, candidate)
                for signature in recognitions
            ]
            topology_candidates.append(
                _stream_kind_with_signature_container(recognition, candidate)
            )
        recognitions.extend(topology_candidates)
        for namespace, values in ffprobe.sources.items():
            sources[namespace].update(values)
        structure.update(ffprobe.structure)
        warnings.extend(ffprobe.warnings)
        _merge_omitted(omitted, ffprobe.omitted)
        recognition, new_conflicts = _select_recognition(recognitions)
        # ffprobe can correct container-only signature/parser assumptions.
        # Recompute from the transformed candidate set so superseded topology
        # disagreements do not survive as false polyglot conflicts.
        conflicts = new_conflicts
        kind = str(recognition.get("kind")) if recognition else kind
    elif should_run_ffprobe and remaining_adapter_seconds() <= 0:
        ffprobe.outcome = "timeout"
        ffprobe.warnings = [
            _warning(
                "timeout",
                source="ffprobe",
                message="ffprobe was not started because the total probe limit was reached",
            )
        ]
        warnings.extend(ffprobe.warnings)
    elif should_run_ffprobe and cancel_event is not None and cancel_event.is_set():
        ffprobe.outcome = "cancelled"
        ffprobe.warnings = [
            _warning(
                "cancelled",
                source="ffprobe",
                message="ffprobe was not started because extraction was cancelled",
            )
        ]
        warnings.extend(ffprobe.warnings)
    if (
        remaining_adapter_seconds() > 0
        and not (cancel_event is not None and cancel_event.is_set())
        and kind == "pdf"
    ):
        pypdf = _pypdf_adapter(
            source_path,
            timeout=max(0.01, remaining_adapter_seconds()),
            cancel_event=cancel_event,
        )
        for namespace, values in pypdf.sources.items():
            sources[namespace].update(values)
        structure.update(pypdf.structure)
        warnings.extend(pypdf.warnings)
        _merge_omitted(omitted, pypdf.omitted)
    elif kind == "pdf" and remaining_adapter_seconds() <= 0:
        pypdf.outcome = "timeout"
        pypdf.warnings = [
            _warning(
                "timeout",
                source="pypdf",
                message="pypdf was not started because the total probe limit was reached",
            )
        ]
        warnings.extend(pypdf.warnings)
    elif kind == "pdf" and cancel_event is not None and cancel_event.is_set():
        pypdf.outcome = "cancelled"
        pypdf.warnings = [
            _warning(
                "cancelled",
                source="pypdf",
                message="pypdf was not started because extraction was cancelled",
            )
        ]
        warnings.extend(pypdf.warnings)
    if kind == "image":
        dimensions = image_dimensions(prefix)
        if dimensions:
            width, height = dimensions
            sources["blob"]["header_width_pixels"] = width
            sources["blob"]["header_height_pixels"] = height
            image_header.outcome = "success"
        elif recognition and recognition.get("format") in {
            "jpeg",
            "png",
            "gif",
            "webp",
        }:
            image_header.outcome = "corrupt"
            warnings.append(
                _warning(
                    "corrupt",
                    source="image_header",
                    message="Image dimensions were not readable from the bounded header",
                )
            )

    normalization_reserved_values = 2 + (3 if recognition else 0)
    reservable_streams = (
        structure.get("streams") if isinstance(structure.get("streams"), list) else []
    )
    normalization_reserved_values += int(
        _primary_stream(reservable_streams, "audio") is not None
    )
    normalization_reserved_values += int(
        _primary_stream(reservable_streams, "video") is not None
    )
    source_values_omitted = _enforce_global_source_value_limit(
        sources,
        structure,
        omitted,
        reserved_values=normalization_reserved_values,
    )
    if source_values_omitted or "source_value_limit" in _omitted_labels(
        omitted, "classes"
    ):
        warnings.append(
            _warning(
                "resource_limit",
                subtype="source_value_limit",
                source="normalizer",
                path="sources",
                message=(
                    f"Source metadata exceeded the global {MAX_SOURCE_VALUES}-value "
                    "limit"
                ),
            )
        )

    content_adapter = _AdapterResult(
        name="content_recognizer",
        # An empty prefix is a valid unsupported result only when the read
        # itself succeeded. Treat an inaccessible/disappeared materialization
        # as transient so it can never become a reusable unsupported cache hit.
        outcome=(
            "internal_error"
            if read_warnings
            else ("success" if recognition else "not_applicable")
        ),
        available=True,
        version="1",
    )
    blob_adapter = _AdapterResult(
        name="blob", outcome="success", available=True, version="1"
    )
    required_names = {
        "image": {"blob", "content_recognizer", "exiftool"},
        "audio": {"blob", "content_recognizer", "ffprobe", "exiftool"},
        "video": {"blob", "content_recognizer", "ffprobe", "exiftool"},
        "pdf": {"blob", "content_recognizer", "pypdf", "exiftool"},
        "other": {"blob", "content_recognizer"},
    }.get(kind, {"blob", "content_recognizer"})
    results = (blob_adapter, content_adapter, image_header, exiftool, ffprobe, pypdf)
    adapters = {
        result.name: _adapter_public(result, required=result.name in required_names)
        for result in results
    }
    normalized, lineage, normalize_warnings = _normalize(
        recognition,
        sources,
        structure,
        adapters,
        digest=qualified_digest,
        size_bytes=int(size_bytes),
        omitted=omitted,
    )
    warnings.extend(normalize_warnings)
    if (
        kind == "image"
        and image_header.outcome == "corrupt"
        and normalized.get("width_pixels") is None
        and normalized.get("height_pixels") is None
    ):
        normalized["probe_status"] = "partial" if normalized.get("format") else "error"
    completeness, retryable = _cache_completeness(
        str(normalized["probe_status"]), adapters
    )
    envelope: dict[str, Any] = {
        "schema_version": MEDIA_METADATA_SCHEMA_VERSION,
        "limits_version": MEDIA_METADATA_LIMITS_VERSION,
        "normalized": normalized,
        "sources": sources,
        "structure": structure,
        "lineage": lineage,
        "conflicts": conflicts,
        "warnings": warnings,
        "omitted": omitted,
        "extraction": {
            "probe_schema_version": MEDIA_METADATA_PROBE_SCHEMA_VERSION,
            "normalizer_version": MEDIA_METADATA_NORMALIZER_VERSION,
            "projection_registry_version": MEDIA_METADATA_PROJECTION_VERSION,
            "content_recognizer_fingerprint": CONTENT_RECOGNIZER_FINGERPRINT,
            "adapter_fingerprint": ADAPTER_FINGERPRINT,
            "tools": {
                name: item["version"]
                for name, item in adapters.items()
                if item.get("version") is not None
            },
            "adapters": adapters,
            "completeness": completeness,
            "retryable": retryable,
        },
    }
    envelope = overlay_reference_evidence(
        envelope, filename=filename, claimed_mime=claimed_mime
    )
    return _finalize_bounds(envelope)


def overlay_reference_evidence(
    envelope: Mapping[str, Any],
    *,
    filename: str | None,
    claimed_mime: str | None,
) -> dict[str, Any]:
    """Return a deep-copied envelope with only media-cell evidence overlaid."""

    out = copy.deepcopy(dict(envelope))
    normalized = out.setdefault("normalized", _empty_normalized())
    sources = out.setdefault("sources", {name: {} for name in SOURCE_NAMESPACES})
    blob = sources.setdefault("blob", {})
    for key in ("filename", "claimed_mime"):
        blob.pop(key, None)
    out["conflicts"] = [
        conflict
        for conflict in out.get("conflicts") or []
        if not str(conflict.get("reason") or "").startswith("reference_")
    ]
    basename = _basename(filename)
    normalized["filename"] = basename
    if basename:
        blob["filename"] = basename
        out.setdefault("lineage", {}).setdefault("fields", {})["filename"] = (
            _lineage_entry("sources.blob.filename", transforms=["basename"])
        )
    else:
        out.setdefault("lineage", {}).setdefault("fields", {}).pop("filename", None)
    clean_claimed = (
        normalize_metadata_string(claimed_mime or "").split(";", 1)[0].strip().lower()
        or None
    )
    if clean_claimed:
        blob["claimed_mime"] = clean_claimed
        detected = normalized.get("mime")
        if detected and detected != clean_claimed:
            out["conflicts"].append(
                {
                    "field": "mime",
                    "candidate_paths": [
                        "sources.blob.claimed_mime",
                        "sources.blob.detected_mime",
                    ],
                    "selected_path": "sources.blob.detected_mime",
                    "reason": "reference_claim_conflicts_with_content",
                }
            )
    if basename and normalized.get("format"):
        suffix = Path(basename).suffix.lower().lstrip(".")
        expected = MEDIA_METADATA_FORMAT_REGISTRY["extension_aliases"].get(suffix)
        if expected and expected != normalized.get("format"):
            out["conflicts"].append(
                {
                    "field": "format",
                    "candidate_paths": [
                        "sources.blob.filename",
                        "sources.blob.detected_format",
                    ],
                    "selected_path": "sources.blob.detected_format",
                    "reason": "reference_extension_conflicts_with_content",
                }
            )
    out["conflicts"] = _sort_conflicts(out["conflicts"])
    return _finalize_bounds(out)


def projection_values(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Return all 23 typed values plus the byte-equivalent details object."""

    normalized = (
        envelope.get("normalized")
        if isinstance(envelope.get("normalized"), Mapping)
        else {}
    )
    return {
        **{role: normalized.get(role) for role in MEDIA_METADATA_ROLES},
        "details": copy.deepcopy(dict(envelope)),
    }


def _cache_omitted_label_tails(template: Mapping[str, Any]) -> dict[str, list[str]]:
    """Serialize exact private omission labels outside the public envelope."""

    omitted = template.get("omitted")
    if not isinstance(omitted, Mapping):
        raise ValueError("media metadata cache requires omission state")
    tails: dict[str, list[str]] = {}
    for key, maximum in (("paths", 512), ("classes", 256)):
        public = omitted.get(key)
        overflow = omitted.get(f"{key}_overflow")
        if (
            not isinstance(public, list)
            or type(overflow) is not int
            or overflow < 0
            or any(not isinstance(item, str) for item in public)
        ):
            raise ValueError("media metadata cache has invalid omission labels")
        if public != sorted(set(public)) or any(
            normalize_metadata_string(item, maximum=maximum) != item for item in public
        ):
            raise ValueError("media metadata cache has invalid omission labels")
        ordered = sorted(_omitted_labels(omitted, key))
        if len(ordered) != len(public) + overflow or ordered[: len(public)] != public:
            raise ValueError(
                "media metadata cache cannot recover exact omission labels"
            )
        tails[key] = ordered[len(public) :]
    return tails


def _cache_content_facts(envelope: Mapping[str, Any]) -> dict[str, Any]:
    template = overlay_reference_evidence(envelope, filename=None, claimed_mime=None)
    return {
        "envelope": template,
        "omitted_labels": _cache_omitted_label_tails(template),
    }


def media_metadata_cache_facts_size(envelope: Mapping[str, Any]) -> int:
    """Return the canonical byte weight including private omission labels."""

    return len(canonical_json_bytes(_cache_content_facts(envelope)))


def _rehydrated_cache_template(
    facts: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate cache-only omission tails and restore the private label sets."""

    if set(facts) != {"envelope", "omitted_labels"}:
        return None
    raw_template = facts.get("envelope")
    sidecar = facts.get("omitted_labels")
    if not isinstance(raw_template, Mapping) or not isinstance(sidecar, Mapping):
        return None
    if set(sidecar) != {"paths", "classes"}:
        return None
    try:
        template = copy.deepcopy(dict(raw_template))
    except RecursionError:
        return None
    omitted = template.get("omitted")
    if not isinstance(omitted, dict):
        return None
    restored = _OmittedState(omitted)
    for key, maximum in (("paths", 512), ("classes", 256)):
        public = omitted.get(key)
        overflow = omitted.get(f"{key}_overflow")
        tail = sidecar.get(key)
        if (
            not isinstance(public, list)
            or type(overflow) is not int
            or overflow < 0
            or not isinstance(tail, list)
            or len(tail) != overflow
            or any(not isinstance(item, str) for item in [*public, *tail])
        ):
            return None
        if (
            public != sorted(set(public))
            or tail != sorted(set(tail))
            or any(
                normalize_metadata_string(item, maximum=maximum) != item
                for item in [*public, *tail]
            )
        ):
            return None
        combined = [*public, *tail]
        if combined != sorted(set(combined)):
            return None
        setattr(
            restored,
            "all_paths" if key == "paths" else "all_classes",
            set(combined),
        )
    template["omitted"] = restored
    return template


def cache_entry_from_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Build the owned content-cache proposal for an extracted envelope."""

    content_facts = _cache_content_facts(envelope)
    encoded = canonical_json_bytes(content_facts)
    if len(encoded) > MAX_CACHE_FACTS_BYTES:
        raise ValueError("media metadata cache facts exceed the cache bound")
    return {
        "cache_schema_version": MEDIA_METADATA_CACHE_SCHEMA_VERSION,
        "generation": 0,
        "extractor_version": MEDIA_METADATA_EXTRACTOR_VERSION,
        "content_facts_hash": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "content_facts": content_facts,
    }


def media_metadata_cache_compatible(entry: Mapping[str, Any]) -> bool:
    """Whether a cache entry may directly satisfy the current extraction policy."""

    if type(entry.get("generation")) is not int or entry["generation"] < 0:
        return False
    expected = {
        "cache_schema_version": MEDIA_METADATA_CACHE_SCHEMA_VERSION,
        "extractor_version": MEDIA_METADATA_EXTRACTOR_VERSION,
    }
    if any(entry.get(key) != value for key, value in expected.items()):
        return False
    facts = entry.get("content_facts")
    if not isinstance(facts, Mapping):
        return False
    try:
        encoded = canonical_json_bytes(facts)
    except (RecursionError, TypeError, ValueError):
        return False
    if len(encoded) > MAX_CACHE_FACTS_BYTES:
        return False
    expected_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if entry.get("content_facts_hash") != expected_hash:
        return False
    template = _rehydrated_cache_template(facts)
    if template is None:
        return False
    if type(template.get("limits_version")) is not int or (
        template["limits_version"] != MEDIA_METADATA_LIMITS_VERSION
    ):
        return False
    try:
        if not _MEDIA_METADATA_ENVELOPE_VALIDATOR.is_valid(template):
            return False
    except (RecursionError, TypeError, ValueError):
        return False
    extraction = template["extraction"]
    if extraction.get("content_recognizer_fingerprint") != (
        CONTENT_RECOGNIZER_FINGERPRINT
    ):
        return False
    if extraction.get("adapter_fingerprint") != ADAPTER_FINGERPRINT:
        return False

    normalized = template["normalized"]
    kind = normalized.get("kind")
    required = {
        "image": {"blob", "content_recognizer", "exiftool"},
        "audio": {"blob", "content_recognizer", "ffprobe", "exiftool"},
        "video": {"blob", "content_recognizer", "ffprobe", "exiftool"},
        "pdf": {"blob", "content_recognizer", "pypdf", "exiftool"},
        "other": {"blob", "content_recognizer"},
    }.get(str(kind))
    if required is None:
        return False
    adapters = extraction["adapters"]
    if any(item["required"] != (name in required) for name, item in adapters.items()):
        return False
    outcomes = {name: item["outcome"] for name, item in adapters.items()}
    if set(outcomes.values()) & {"timeout", "cancelled", "internal_error"}:
        return False
    if "missing_dependency" in outcomes.values():
        return False
    selected = {outcomes[name] for name in required}
    completeness = extraction.get("completeness")
    retryable = extraction.get("retryable")
    status = normalized.get("probe_status")
    if retryable is not False:
        return False
    if completeness == "complete":
        return status == "ok" and selected == {"success"}
    if completeness == "terminal_unsupported":
        return (
            status == "unsupported"
            and kind == "other"
            and outcomes.get("blob") == "success"
            and outcomes.get("content_recognizer") == "not_applicable"
        )
    if completeness == "terminal_partial":
        omitted = template["omitted"]
        material_omission = bool(
            _omitted_labels(omitted, "classes") & _MATERIAL_OMISSION_CLASSES
        ) or any(
            int(omitted.get(key) or 0)
            for key in (
                "truncated_values",
                "structure_items",
                "lineage_items",
                "warnings_overflow",
                "conflicts_overflow",
            )
        )
        return status in {"partial", "error"} and (
            material_omission
            or bool(set(outcomes.values()) & {"corrupt", "encrypted", "resource_limit"})
        )
    return False


def envelope_from_cache(
    entry: Mapping[str, Any],
    *,
    digest: str,
    size_bytes: int,
) -> dict[str, Any]:
    """Rehydrate the row-independent envelope from a compatible cache entry."""

    if not media_metadata_cache_compatible(entry):
        raise ValueError("incompatible media metadata cache entry")
    facts = entry["content_facts"]
    template = _rehydrated_cache_template(facts)
    if template is None:  # The compatibility check above already validated this.
        raise ValueError("incompatible media metadata cache entry")
    qualified = _qualified_digest(digest)
    if isinstance(size_bytes, bool) or int(size_bytes) < 0:
        raise ValueError("size_bytes must be a nonnegative integer")
    target_size_bytes = int(size_bytes)
    normalized = template.get("normalized")
    sources = template.get("sources")
    blob_source = sources.get("blob") if isinstance(sources, Mapping) else None
    if (
        not isinstance(normalized, Mapping)
        or not isinstance(blob_source, Mapping)
        or normalized.get("blob_hash") != qualified
        or type(normalized.get("size_bytes")) is not int
        or normalized.get("size_bytes") != target_size_bytes
        or blob_source.get("blob_hash") != qualified
        or type(blob_source.get("size_bytes")) is not int
        or blob_source.get("size_bytes") != target_size_bytes
    ):
        raise ValueError("media metadata cache does not match the target blob")
    envelope = copy.deepcopy(template)
    envelope["normalized"]["blob_hash"] = qualified
    envelope["normalized"]["size_bytes"] = target_size_bytes
    envelope["sources"]["blob"]["blob_hash"] = qualified
    envelope["sources"]["blob"]["size_bytes"] = target_size_bytes
    return overlay_reference_evidence(envelope, filename=None, claimed_mime=None)


__all__ = [
    "ADAPTER_FINGERPRINT",
    "CONTENT_RECOGNIZER_FINGERPRINT",
    "MEDIA_METADATA_CACHE_NAMESPACE",
    "MEDIA_METADATA_ENVELOPE_SCHEMA",
    "MEDIA_METADATA_EXTRACTOR_VERSION",
    "MEDIA_METADATA_OMITTED_CLASS_REGISTRY",
    "MEDIA_METADATA_PROJECTION",
    "MEDIA_METADATA_ROLES",
    "MediaMetadataFields",
    "canonical_envelope_json",
    "cache_entry_from_envelope",
    "envelope_from_cache",
    "extract_media_metadata",
    "image_dimensions",
    "media_metadata_cache_facts_size",
    "media_metadata_cache_compatible",
    "normalize_metadata_string",
    "overlay_reference_evidence",
    "projection_values",
]
