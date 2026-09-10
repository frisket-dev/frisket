"""Focused unit pins for the quote aligner that the conformance suite does not isolate:

* A temporal match must emit the SOURCE segment indexes the
  tokens carry, not their positions in a (possibly scoped) stream.
* A <=3-char normalized quote returns [] (the strict short-quote
  guard), never a fuzzy/exact-shape hit.
"""

from __future__ import annotations

from frisket.engine.store.grounding_contract import (
    SegmentStream,
    SegmentToken,
    TextTarget,
    WordStream,
    WordToken,
    ground,
)
from frisket.engine.store.quote_align import Token, align_quote_to_tokens


def test_temporal_result_emits_source_segment_indices_not_positions() -> None:
    """A stream whose segments start at index 4 (positions 0,1,2 -> indexes
    4,5,6): a quote covering the last two segments must resolve to source
    indexes [5, 6], not the stream positions [1, 2]."""

    stream = SegmentStream(
        tokens=[
            SegmentToken(text="alpha one", start_ms=4000, end_ms=5000, index=4),
            SegmentToken(text="bravo two", start_ms=5000, end_ms=6000, index=5),
            SegmentToken(text="charlie three", start_ms=6000, end_ms=7000, index=6),
        ]
    )
    matches = ground(TextTarget(text="bravo two charlie three"), stream)
    assert matches, "expected a temporal match"
    span = matches[0].spans[0]
    assert span.span_kind == "temporal"
    assert span.metadata["segment_indices"] == [5, 6]
    assert span.start_ms == 5000
    assert span.end_ms == 7000


def test_temporal_index_defaults_to_position_when_absent() -> None:
    """A raw Token stream with no explicit ``index`` falls back to the stream
    position (backwards-compatible with contiguous streams)."""

    tokens = [
        Token(text="alpha one", start_ms=0, end_ms=1000, source="segment"),
        Token(text="bravo two", start_ms=1000, end_ms=2000, source="segment"),
    ]
    results = align_quote_to_tokens("bravo two", tokens)
    assert results
    assert results[0].segment_indices == [1]


def test_short_quote_guard_returns_empty() -> None:
    """A <=3-char normalized quote is noise to ground -> [] (the
    caller keeps the coarse anchor), never a fuzzy or exact-shape region."""

    tokens = WordStream(
        tokens=[
            WordToken(text="us", box=(0.1, 0.1, 0.2, 0.13), page=1, source="ocr"),
            WordToken(text="is", box=(0.3, 0.1, 0.4, 0.13), page=1, source="ocr"),
        ]
    )
    assert ground(TextTarget(text="us"), tokens) == []
