from __future__ import annotations

from dataclasses import dataclass

import pytest

from frisket.retrieval.sources import (
    canonical_json,
    part_id,
    representation_id,
    row_composite_revision,
    row_composite_source_revision,
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


def test_representation_identity_uses_complete_space():
    assert representation_id(space={"model": "m", "task": "query"}, input_hash="h") != (
        representation_id(space={"model": "m", "task": "document"}, input_hash="h")
    )


def test_identity_json_rejects_non_finite_values():
    with pytest.raises(ValueError, match="finite"):
        canonical_json({"overlap": float("nan")})
