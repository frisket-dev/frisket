from __future__ import annotations

import asyncio
import hashlib

import pytest

from frisket.retrieval.content import (
    ImageSurface,
    PreparedDocumentSurface,
    PreparedPageExtent,
    TextChunkRecipe,
    TextSurface,
    TranscriptChunkRecipe,
    TranscriptSegment,
    TranscriptSurface,
    produce_image,
    produce_prepared_document,
    produce_text,
    produce_transcript,
)
from frisket.retrieval.contracts import PageRange, SourceRevision, TimeRange
from frisket.retrieval.encoding import ImageEncodingInput, TextEncodingInput


def _digest(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _source(*, version: int = 7) -> SourceRevision:
    return SourceRevision(
        source_id="cell:11:13",
        source_kind="cell",
        version_kind="retrieval_revision",
        version_value=version,
    )


def _text_surface(text: str, *, version: int = 7) -> TextSurface:
    return TextSurface(
        source=_source(version=version),
        text_surface_id="text-surface:11:13:7",
        content_hash=_digest(text),
        text=text,
    )


def test_text_chunks_use_unicode_codepoint_offsets_overlap_and_exact_tail() -> None:
    surface = _text_surface("A🙂BCDEFG")
    recipe = TextChunkRecipe(max_codepoints=4, overlap_codepoints=1)

    produced = produce_text(surface, recipe, max_parts=10)

    assert [item.input.text for item in produced.items] == ["A🙂BC", "CDEF", "FG"]
    assert [
        (item.part.locator.text.start, item.part.locator.text.end)
        for item in produced.items
        if item.part.locator.text is not None
    ] == [(0, 4), (3, 7), (6, 8)]
    assert all(isinstance(item.input, TextEncodingInput) for item in produced.items)
    assert produced.coverage.state == "ready"
    assert produced.continuation is None


def test_text_chunks_prefer_a_nearby_paragraph_boundary_under_the_hard_limit() -> None:
    text = "First paragraph.\n\nSecond paragraph carries on."
    produced = produce_text(
        _text_surface(text),
        TextChunkRecipe(max_codepoints=22, overlap_codepoints=3),
        max_parts=10,
    )

    first = produced.items[0]
    assert first.input.text == "First paragraph.\n\n"
    assert first.part.locator.text is not None
    assert first.part.locator.text.end == len("First paragraph.\n\n")
    assert produced.items[1].part.locator.text is not None
    assert produced.items[1].part.locator.text.start == first.part.locator.text.end - 3
    assert all(len(item.input.text) <= 22 for item in produced.items)


def test_text_batches_resume_to_the_same_parts_and_reject_wrong_identity() -> None:
    surface = _text_surface("abcdefghijk")
    recipe = TextChunkRecipe(max_codepoints=5, overlap_codepoints=2)
    complete = produce_text(surface, recipe, max_parts=20)

    first = produce_text(surface, recipe, max_parts=1)
    assert first.coverage.state == "pending"
    assert first.continuation is not None
    second = produce_text(surface, recipe, max_parts=1, cursor=first.continuation)
    assert second.continuation is not None
    third = produce_text(surface, recipe, max_parts=20, cursor=second.continuation)

    resumed = first.items + second.items + third.items
    assert [item.part.part_id for item in resumed] == [
        item.part.part_id for item in complete.items
    ]
    assert [item.input for item in resumed] == [item.input for item in complete.items]
    with pytest.raises(ValueError, match="cursor does not belong"):
        produce_text(
            _text_surface(surface.text, version=8),
            recipe,
            max_parts=1,
            cursor=first.continuation,
        )
    with pytest.raises(ValueError, match="cursor does not belong"):
        produce_text(
            surface,
            TextChunkRecipe(max_codepoints=6, overlap_codepoints=2),
            max_parts=1,
            cursor=first.continuation,
        )


def test_blank_text_has_complete_zero_part_coverage() -> None:
    produced = produce_text(_text_surface(" \n\t "), TextChunkRecipe(), max_parts=2)

    assert produced.items == ()
    assert produced.continuation is None
    assert produced.coverage.state == "ready"
    assert produced.coverage.prepared_count == 0


def test_prepared_document_uses_existing_page_extents_without_reconversion() -> None:
    page_one = "alpha"
    page_two = "bravo"
    text = page_one + "\n\n" + page_two
    surface = PreparedDocumentSurface(
        text=_text_surface(text),
        pages=(
            PreparedPageExtent(
                page_number=1,
                version_id=101,
                content_hash=_digest(page_one),
                start=0,
                end=5,
            ),
            PreparedPageExtent(
                page_number=2,
                version_id=102,
                content_hash=_digest(page_two),
                start=7,
                end=12,
            ),
        ),
    )

    produced = produce_prepared_document(
        surface,
        TextChunkRecipe(max_codepoints=8, overlap_codepoints=2),
        max_parts=10,
    )

    assert [item.input.text for item in produced.items] == ["alpha\n\n", "\n\nbravo"]
    assert [item.part.locator.pages for item in produced.items] == [
        PageRange(start=1, end=1),
        PageRange(start=2, end=2),
    ]
    assert all(item.part.part_kind == "prepared_text_chunk" for item in produced.items)


def test_prepared_page_hash_and_extent_must_match_the_resolved_surface() -> None:
    text = _text_surface("page")

    with pytest.raises(ValueError, match="page content_hash"):
        PreparedDocumentSurface(
            text=text,
            pages=(
                PreparedPageExtent(
                    page_number=1,
                    version_id=1,
                    content_hash=_digest("other"),
                    start=0,
                    end=4,
                ),
            ),
        )


def _transcript() -> TranscriptSurface:
    return TranscriptSurface(
        source=_source(),
        segments=(
            TranscriptSegment(
                source_span_id="span:1",
                start_ms=1000,
                end_ms=2000,
                text="one",
                segment_index=1,
            ),
            TranscriptSegment(
                source_span_id="span:2",
                start_ms=2100,
                end_ms=3000,
                text="two",
                segment_index=2,
            ),
            TranscriptSegment(
                source_span_id="span:3",
                start_ms=3200,
                end_ms=4500,
                text="three",
                segment_index=3,
            ),
        ),
    )


def test_transcript_groups_whole_segments_with_ordered_spans_and_overlap() -> None:
    produced = produce_transcript(
        _transcript(),
        TranscriptChunkRecipe(max_codepoints=7, overlap_segments=1),
        max_parts=10,
    )

    assert [item.input.text for item in produced.items] == ["one\ntwo", "two", "three"]
    assert [item.part.locator.source_span_ids for item in produced.items] == [
        ("span:1", "span:2"),
        ("span:2",),
        ("span:3",),
    ]
    assert [item.part.locator.time for item in produced.items] == [
        TimeRange(start_seconds=1.0, end_seconds=3.0),
        TimeRange(start_seconds=2.1, end_seconds=3.0),
        TimeRange(start_seconds=3.2, end_seconds=4.5),
    ]
    assert all(item.part.locator.text is None for item in produced.items)


def test_oversized_transcript_segment_is_bounded_with_source_unit_location() -> None:
    surface = TranscriptSurface(
        source=_source(),
        segments=(
            TranscriptSegment(
                source_span_id="span:long",
                start_ms=250,
                end_ms=9250,
                text="abcdefghijk",
                segment_index=4,
            ),
        ),
    )
    recipe = TranscriptChunkRecipe(max_codepoints=4, overlap_segments=1)

    first = produce_transcript(surface, recipe, max_parts=2)
    assert first.continuation is not None
    second = produce_transcript(surface, recipe, max_parts=2, cursor=first.continuation)
    items = first.items + second.items

    assert [item.input.text for item in items] == ["abcd", "efgh", "ijk"]
    assert all(len(item.input.text) <= 4 for item in items)
    assert all(item.part.locator.time == TimeRange(0.25, 9.25) for item in items)
    assert all(item.part.locator.source_span_ids == ("span:long",) for item in items)
    assert len({item.part.part_id for item in items}) == 3
    assert second.continuation is None


def test_repeated_oversized_transcript_fragments_keep_distinct_part_ids() -> None:
    surface = TranscriptSurface(
        source=_source(),
        segments=(
            TranscriptSegment(
                source_span_id="span:repeat",
                start_ms=0,
                end_ms=1000,
                text="aaaaaaaa",
                segment_index=1,
            ),
        ),
    )

    produced = produce_transcript(
        surface,
        TranscriptChunkRecipe(max_codepoints=4),
        max_parts=10,
    )

    assert [item.input.text for item in produced.items] == ["aaaa", "aaaa"]
    assert len({item.part.part_id for item in produced.items}) == 2


def test_image_bytes_and_existing_source_spans_pass_through_unchanged() -> None:
    image = b"not-decoded-at-the-producer-boundary"
    surface = ImageSurface(
        source=_source(),
        image_bytes=image,
        content_hash=_digest(image),
        source_span_ids=("span:image",),
    )

    produced = produce_image(surface)

    assert len(produced.items) == 1
    item = produced.items[0]
    assert item.input == ImageEncodingInput(image_bytes=image)
    assert item.part.locator.source_span_ids == ("span:image",)
    assert produced.coverage.state == "ready"


def test_surface_hashes_are_verified_once_at_construction() -> None:
    with pytest.raises(ValueError, match="content_hash"):
        TextSurface(
            source=_source(),
            text_surface_id="surface:bad",
            content_hash=_digest("different"),
            text="actual",
        )
    with pytest.raises(ValueError, match="content_hash"):
        ImageSurface(
            source=_source(),
            image_bytes=b"actual",
            content_hash=_digest(b"different"),
        )


def test_production_honors_cancellation_between_parts() -> None:
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(asyncio.CancelledError):
        produce_text(
            _text_surface("abcdefgh"),
            TextChunkRecipe(max_codepoints=2, overlap_codepoints=0),
            max_parts=10,
            cancelled=cancelled,
        )


@pytest.mark.parametrize("max_parts", [0, -1, True])
def test_batch_bound_must_be_a_positive_integer(max_parts: int) -> None:
    with pytest.raises((TypeError, ValueError), match="max_parts"):
        produce_text(_text_surface("abc"), TextChunkRecipe(), max_parts=max_parts)
