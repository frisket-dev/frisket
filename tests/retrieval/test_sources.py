from __future__ import annotations

from dataclasses import dataclass

import pytest

from frisket.retrieval.sources import (
    blob_source_revision,
    canonical_json,
    input_hash,
    ocr_source_revision,
    part_id,
    representation_id,
    row_composite_revision,
    row_composite_source_revision,
    transcript_source_revision,
)


@dataclass(frozen=True)
class Revision:
    source_id: str
    authority_version: int


def test_part_identity_includes_revision_recipe_locator_and_kind():
    revision = Revision("cell:7:9", 4)
    base = part_id(revision, {"version": 1}, {"start": 0, "end": 10}, kind="text")

    assert base == part_id(
        revision, {"version": 1}, {"end": 10, "start": 0}, kind="text"
    )
    assert base != part_id(
        Revision("cell:7:9", 5),
        {"version": 1},
        {"start": 0, "end": 10},
        kind="text",
    )
    assert base != part_id(
        revision, {"version": 2}, {"start": 0, "end": 10}, kind="text"
    )


def test_row_composite_preserves_member_order_and_recipe():
    forward = row_composite_revision(["a@1", "b@2"], recipe={"separator": "\n"})
    reverse = row_composite_revision(["b@2", "a@1"], recipe={"separator": "\n"})

    assert forward != reverse


def test_row_composite_source_revision_centralizes_named_index_identity():
    revision = row_composite_source_revision(
        index_id="index-7",
        source_key="row-9",
        ordered_member_revisions=("title@3", "body@5"),
        recipe={"separator": "\n"},
        content_hash="sha256:payload",
    )

    assert revision.source_id == "embedding-index:index-7:row:row-9"
    assert revision.source_kind == "row_composite"
    assert revision.version_kind == "row_composite_revision"
    assert revision.content_hash == "sha256:payload"


def test_evidence_source_revisions_bind_link_snapshot_and_exact_text():
    transcript = transcript_source_revision(
        evidence_link_stable_id="link-t",
        snapshot_hash="sha256:transcript-snapshot",
        text="speaker text",
    )
    ocr = ocr_source_revision(
        evidence_link_stable_id="link-o",
        snapshot_hash="sha256:ocr-snapshot",
        text="page text",
    )

    assert transcript.source_id == "transcript-link:link-t"
    assert (transcript.source_kind, transcript.version_kind) == (
        "transcript",
        "transcript_snapshot",
    )
    assert transcript.version_value == "sha256:transcript-snapshot"
    assert transcript.content_hash == input_hash("speaker text")
    assert ocr.source_id == "ocr-link:link-o"
    assert (ocr.source_kind, ocr.version_kind) == ("ocr_text", "ocr_snapshot")
    assert ocr.version_value == "sha256:ocr-snapshot"
    assert ocr.content_hash == input_hash("page text")


def test_blob_source_revision_uses_canonical_content_digest():
    digest = "ab" * 32
    revision = blob_source_revision(blob_hash=digest)

    assert revision.source_id == f"blob:{digest}"
    assert (revision.source_kind, revision.version_kind) == ("blob", "blob_digest")
    assert revision.version_value == digest
    assert revision.content_hash == f"sha256:{digest}"

    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        blob_source_revision(blob_hash=digest.upper())


def test_representation_identity_uses_complete_space():
    assert representation_id(space={"model": "m", "task": "query"}, input_hash="h") != (
        representation_id(space={"model": "m", "task": "document"}, input_hash="h")
    )


def test_identity_json_rejects_non_finite_values():
    with pytest.raises(ValueError, match="finite"):
        canonical_json({"overlap": float("nan")})
