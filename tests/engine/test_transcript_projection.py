from __future__ import annotations

from frisket.engine.store.transcript_projection import (
    PARTIAL_SPEECH_MARKER,
    TranscriptSourceSegment,
    TranscriptWord,
    project_transcript_segments,
)


def _segment(
    stable_id: str,
    start_ms: int,
    end_ms: int,
    text: str,
    *,
    index: int = 0,
    words: tuple[TranscriptWord, ...] = (),
    metadata: dict | None = None,
) -> TranscriptSourceSegment:
    return TranscriptSourceSegment(
        span_stable_id=stable_id,
        start_ms=start_ms,
        end_ms=end_ms,
        text=text,
        segment_index=index,
        speaker="S1",
        speaker_confidence="approximate",
        words=words,
        metadata=metadata or {},
    )


def test_full_segment_preserves_text_speaker_words_and_rebases() -> None:
    source = _segment(
        "span-full",
        1500,
        2500,
        "Hello world.",
        index=7,
        words=(
            TranscriptWord("Hello", 1500, 1900),
            TranscriptWord("world", 1900, 2400),
            TranscriptWord(".", 2400, 2500),
        ),
    )
    result = project_transcript_segments(
        [source],
        source_start_ms=1000,
        source_end_ms=3000,
        source_artifact_stable_id="artifact-source",
        transcript_run_id=41,
        evidence_link_stable_id="link-source",
        language="en",
    )

    assert result.text == "Hello world."
    assert result.language == "en"
    (projected,) = result.segments
    assert (projected.start_ms, projected.end_ms) == (500, 1500)
    assert projected.clipping == "full"
    assert projected.partial is False
    assert projected.source_segment_index == 7
    assert projected.speaker == "S1"
    assert projected.speaker_namespace == "media.transcribe:41"
    assert [(word.start_ms, word.end_ms) for word in projected.words] == [
        (500, 900),
        (900, 1400),
        (1400, 1500),
    ]
    cell_value = projected.as_transcript_segment()
    assert (cell_value["start"], cell_value["end"]) == (0.5, 1.5)
    assert cell_value["words"][0] == {
        "word": "Hello",
        "start": 0.5,
        "end": 0.9,
    }
    span_fields = projected.as_source_span_fields()
    assert span_fields["start_ms"] == 500
    assert span_fields["selector"]["source_segment_index"] == 7
    assert span_fields["metadata"]["projection"]["parent_span_stable_id"] == (
        "span-full"
    )


def test_partial_segment_with_words_rebuilds_only_intersecting_text() -> None:
    source = _segment(
        "span-word-partial",
        0,
        2000,
        "Hello, world later",
        words=(
            TranscriptWord("Hello", 0, 600),
            TranscriptWord(",", 600, 700),
            TranscriptWord("world", 700, 1500),
            TranscriptWord("later", 1500, 2000),
        ),
    )
    result = project_transcript_segments(
        [source],
        source_start_ms=500,
        source_end_ms=1300,
        source_artifact_stable_id="artifact-source",
    )

    (projected,) = result.segments
    assert result.text == "Hello, world"
    assert projected.text == "Hello, world"
    assert projected.clipping == "both"
    assert projected.partial is True
    assert projected.text_precision == "word_timestamps"
    assert projected.text_may_extend_outside_clip is False
    assert [(word.text, word.start_ms, word.end_ms) for word in projected.words] == [
        ("Hello", 0, 100),
        (",", 100, 200),
        ("world", 200, 800),
    ]
    assert projected.words[0].clipped_left is True
    assert projected.words[-1].clipped_right is True
    assert projected.lineage()["source_quote"] == "Hello, world later"


def test_partial_segment_without_words_uses_honest_marker() -> None:
    source = _segment("span-coarse", 0, 2000, "The entire source utterance")
    result = project_transcript_segments(
        [source],
        source_start_ms=500,
        source_end_ms=1500,
        source_artifact_stable_id="artifact-source",
    )

    assert result.text == PARTIAL_SPEECH_MARKER
    (projected,) = result.segments
    assert projected.text == PARTIAL_SPEECH_MARKER
    assert projected.text_precision == "source_segment"
    assert projected.text_may_extend_outside_clip is True
    assert "The entire source utterance" not in result.text
    assert projected.lineage()["source_quote"] == "The entire source utterance"


def test_half_open_boundaries_exclude_touching_segments_and_dense_indices() -> None:
    sources = [
        _segment("before", 0, 1000, "before", index=0),
        _segment("one", 1000, 1500, "one", index=1),
        _segment("two", 1500, 2000, "two", index=2),
        _segment("after", 2000, 2500, "after", index=3),
    ]
    result = project_transcript_segments(
        reversed(sources),
        source_start_ms=1000,
        source_end_ms=2000,
        source_artifact_stable_id="artifact-source",
    )

    assert [segment.text for segment in result.segments] == ["one", "two"]
    assert [segment.segment_index for segment in result.segments] == [0, 1]
    assert [(segment.start_ms, segment.end_ms) for segment in result.segments] == [
        (0, 500),
        (500, 1000),
    ]


def test_source_ordinal_wins_when_overlapping_segments_start_together() -> None:
    result = project_transcript_segments(
        [
            _segment("first", 100, 900, "first", index=0),
            _segment("second", 100, 500, "second", index=1),
        ],
        source_start_ms=0,
        source_end_ms=1_000,
        source_artifact_stable_id="artifact-source",
    )

    assert result.text == "first second"
    assert [segment.source_segment_index for segment in result.segments] == [0, 1]


def test_clip_of_clip_propagates_root_span_and_coordinates() -> None:
    source = _segment(
        "immediate-parent-span",
        100,
        900,
        "parent projected speech",
        metadata={
            "projection": {
                "root_span_stable_id": "original-root-span",
                "root_start_ms": 5100,
                "root_end_ms": 5900,
            }
        },
    )
    result = project_transcript_segments(
        [source],
        source_start_ms=300,
        source_end_ms=700,
        source_artifact_stable_id="parent-clip-artifact",
    )

    (projected,) = result.segments
    lineage = projected.lineage()
    assert lineage["parent_span_stable_id"] == "immediate-parent-span"
    assert lineage["root_span_stable_id"] == "original-root-span"
    assert (lineage["root_start_ms"], lineage["root_end_ms"]) == (5300, 5700)


def test_clip_of_clip_does_not_upgrade_inherited_partial_text_to_exact() -> None:
    source = TranscriptSourceSegment(
        span_stable_id="parent-partial",
        start_ms=100,
        end_ms=900,
        text=PARTIAL_SPEECH_MARKER,
        partial=True,
        text_precision="source_segment",
        text_may_extend_outside_clip=True,
        source_quote="original complete utterance",
        metadata={
            "projection": {
                "root_span_stable_id": "root-partial",
                "root_start_ms": 5100,
                "root_end_ms": 5900,
                "source_quote": "original complete utterance",
            }
        },
    )
    result = project_transcript_segments(
        [source],
        source_start_ms=0,
        source_end_ms=1000,
        source_artifact_stable_id="parent-clip-artifact",
    )

    (projected,) = result.segments
    assert projected.partial is True
    assert projected.text == PARTIAL_SPEECH_MARKER
    assert projected.text_may_extend_outside_clip is True
    assert projected.lineage()["source_quote"] == "original complete utterance"


def test_mapping_input_accepts_viewer_selector_and_warns_on_bad_word() -> None:
    result = project_transcript_segments(
        [
            {
                "stable_id": "span-mapping",
                "start_ms": 100,
                "end_ms": 900,
                "quote": "mapped",
                "selector": {
                    "kind": "temporal",
                    "temporal": {
                        "segment_index": 3,
                        "speaker": "S2",
                        "words": [
                            {"word": "mapped", "start_ms": 100, "end_ms": 900},
                            {"word": "bad", "start_ms": 500, "end_ms": 400},
                        ],
                    },
                },
            }
        ],
        source_start_ms=0,
        source_end_ms=1000,
        source_artifact_stable_id="artifact-source",
    )

    assert result.text == "mapped"
    assert result.warnings == ("invalid_transcript_word:span-mapping",)
    assert result.segments[0].speaker == "S2"
    assert len(result.segments[0].words) == 1
