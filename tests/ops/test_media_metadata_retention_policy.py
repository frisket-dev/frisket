from __future__ import annotations


import pytest
from jsonschema import Draft202012Validator

import frisket.ops.media_metadata as metadata
from frisket.actions.media_metadata import MetadataOutput, MetadataParams
from frisket.actions.registry import ACTION_REGISTRY
from pydantic import ValidationError


def test_runtime_schema_remains_authoritative_for_typed_details_outputs() -> None:
    schema = metadata.MEDIA_METADATA_ENVELOPE_SCHEMA
    Draft202012Validator.check_schema(schema)
    terminal = ACTION_REGISTRY.get("media.extract_metadata").definition.run
    object_fields = terminal.resolve_output_fields(MetadataParams(source="asset"))
    columns_fields = terminal.resolve_output_fields(
        MetadataParams(source="asset", output_mode="columns")
    )
    assert [(field.key, field.column_type) for field in object_fields] == [
        ("details", "json")
    ]
    assert len(columns_fields) == 24
    assert [field.key for field in columns_fields[:-1]] == list(
        metadata.MEDIA_METADATA_ROLES
    )
    assert columns_fields[-1].key == "details"
    assert columns_fields[-1].column_type == "json"
    # The action advertises an ordinary JSON cell; its runtime output validator
    # still enforces the full versioned domain envelope and its byte bound.
    with pytest.raises(ValidationError, match="invalid media metadata envelope"):
        MetadataOutput(details={"normalized": {"kind": "image"}})


@pytest.mark.parametrize(
    "group", ["System", "Copy1:System", "ExifTool", "Copy1:ExifTool"]
)
@pytest.mark.parametrize("tag", ["directory", "file_size", "image_width", "unknown"])
def test_exiftool_transport_groups_are_omitted_by_adapter_identity(
    group: str, tag: str
) -> None:
    assert (
        metadata._tag_disposition(
            tag,
            adapter="exiftool",
            adapter_group=group,
        )
        == "always_transport_omit"
    )
    assert (
        metadata._tag_disposition(
            tag,
            adapter="exiftool",
            adapter_group="Copy1:XMP",
        )
        == "safe"
    )


@pytest.mark.parametrize(
    "tag",
    [
        "bits_per_sample",
        "comment",
        "current_iptc_digest",
        "exif_byte_order",
        "file_type",
        "file_type_extension",
        "id3_size",
        "image_height",
        "image_width",
        "mime_type",
        "page_count",
        "x_resolution",
        "y_resolution",
    ],
)
def test_exiftool_file_content_facts_are_retained(tag: str) -> None:
    assert (
        metadata._tag_disposition(
            tag,
            adapter="exiftool",
            adapter_group="Copy1:File",
        )
        == "safe"
    )


def test_exiftool_source_file_transport_field_is_omitted() -> None:
    assert (
        metadata._tag_disposition(
            "source_file",
            adapter="exiftool",
            adapter_group="File",
        )
        == "always_transport_omit"
    )


@pytest.mark.parametrize(
    "tag",
    [
        "attachment_data",
        "certificate_data",
        "codec_private",
        "data",
        "extradata",
        "javascript",
        "raw_data",
        "signature_value",
        "thumbnail_image",
        "xfa_data",
    ],
)
@pytest.mark.parametrize("group", ["PDF", "PDFInfo", "XMP"])
def test_scalar_names_do_not_trigger_content_omission(tag: str, group: str) -> None:
    assert (
        metadata._tag_disposition(
            tag,
            adapter="exiftool",
            adapter_group=group,
        )
        == "safe"
    )


@pytest.mark.parametrize(
    "tag",
    [
        "api_key",
        "certificate",
        "gps_latitude",
        "owner_name",
        "password",
        "thumbnail",
        "unknown",
    ],
)
def test_metadata_is_not_removed_by_sensitivity_like_name(tag: str) -> None:
    assert metadata._tag_disposition(tag) == "safe"
