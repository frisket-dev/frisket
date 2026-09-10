"""Recorded-output parser gate for the MOSS transcript grammar.

These run on CPU against recorded ``[start][Sxx]text[end]`` strings — the
fixture-green gate for the adapter.  They cover real bracket ambiguity and
assert that malformed/truncated model output fails closed.  Fixture-green is
not "MOSS done": the live diarized GPU burn remains a shared, later proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from frisket_worker_moss.transcript import (
    MossTranscriptError,
    parse_moss_transcript,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_basic_two_speaker_segments_are_ordered_and_labelled():
    segments = parse_moss_transcript(_load("basic_two_speaker.txt"))

    assert [(s.start, s.end, s.speaker) for s in segments] == [
        (0.48, 1.66, "S01"),
        (12.26, 13.81, "S02"),
        (14.36, 18.76, "S01"),
    ]
    assert segments[0].text == "Welcome everyone"
    assert segments[1].text == "The new transcription pipeline is ready for evaluation"
    for segment in segments:
        assert segment.speaker
        assert 0.0 <= segment.start <= segment.end


def test_overlap_between_speakers_is_preserved_not_flattened():
    segments = parse_moss_transcript(_load("overlap.txt"))

    starts = [s.start for s in segments]
    assert starts == sorted(starts)  # nondecreasing start
    # S02 starts (1.10) before S01's first segment ends (1.40): real overlap.
    assert segments[0].end > segments[1].start
    assert segments[1].speaker == "S02"


def test_inner_numeric_brackets_in_text_are_preserved():
    # The reference's own regression case: numeric brackets inside speech must
    # stay text; only the final [4] is the segment end.
    segments = parse_moss_transcript("[0][S01]第[2024]年，编号[001]继续[4]")

    assert len(segments) == 1
    assert segments[0].start == 0.0
    assert segments[0].end == 4.0
    assert segments[0].text == "第[2024]年，编号[001]继续"


def test_adjacent_numeric_bracket_and_end_is_rejected_as_ambiguous():
    # This is indistinguishable from a decoder cut after the next segment start
    # (`...year [2024]` + `[4]`), so fail rather than choose either meaning.
    with pytest.raises(MossTranscriptError, match="ambiguous adjacent"):
        parse_moss_transcript("[0][S01]year [2024][4]")


def test_numeric_and_acoustic_brackets_adjacent_to_end_are_preserved():
    segments = parse_moss_transcript("[.5][S01]code [7][noise][1.]")

    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.5, 1.0, "code [7][noise]")
    ]


def test_acoustic_event_brackets_in_text_are_kept():
    segments = parse_moss_transcript(_load("acoustic_event.txt"))

    assert segments[0].text == "Welcome [applause] to the show"
    assert segments[0].start == 0.10
    assert segments[0].end == 3.20


def test_single_speaker_transcript():
    segments = parse_moss_transcript(_load("single_speaker.txt"))

    assert len(segments) == 1
    assert segments[0].speaker == "S01"
    assert segments[0].end == 7.42


def test_multi_digit_speakers_and_integer_timestamps():
    segments = parse_moss_transcript("[0][S10]hello[1][1][S02]world[2]")

    assert [(s.start, s.end, s.speaker, s.text) for s in segments] == [
        (0.0, 1.0, "S10", "hello"),
        (1.0, 2.0, "S02", "world"),
    ]


def test_truncated_final_segment_is_a_hard_model_fault():
    # Returning only the prefix would make an incomplete transcription look
    # complete to downstream consumers; a warning cannot restore missing audio.
    with pytest.raises(MossTranscriptError, match="no end timestamp"):
        parse_moss_transcript(_load("truncated_tail.txt"))


def test_midopener_truncation_is_a_hard_model_fault():
    with pytest.raises(MossTranscriptError, match="unparseable content"):
        parse_moss_transcript(_load("truncated_midopener.txt"))


def test_midopener_truncation_minimal_case():
    # Never misreport [2] as the first segment's end or swallow [1] into text.
    with pytest.raises(MossTranscriptError, match="unparseable content"):
        parse_moss_transcript("[0][S01]complete[1][2][S0")


def test_truncation_exactly_after_next_start_is_not_a_corrupted_success():
    with pytest.raises(MossTranscriptError, match="ambiguous adjacent"):
        parse_moss_transcript("[0][S01]first[1][2]")


def test_missing_midstream_end_does_not_fabricate_a_segment():
    with pytest.raises(MossTranscriptError, match="no end timestamp"):
        parse_moss_transcript("[0][S01]missing-end[2][S02]good[3]")


def test_out_of_order_starts_are_not_silently_reordered():
    with pytest.raises(MossTranscriptError, match="before the prior"):
        parse_moss_transcript("[5][S02]later[6][0][S01]earlier[1]")


def test_empty_transcript_is_silence_not_an_error():
    assert parse_moss_transcript("   ") == []


def test_structurally_complete_empty_segment_is_silence():
    assert parse_moss_transcript("[0][S01][1]") == []


def test_no_markers_raises_inference_error():
    with pytest.raises(MossTranscriptError):
        parse_moss_transcript("plain text with no timestamps at all")


def test_prefix_junk_raises_inference_error():
    with pytest.raises(MossTranscriptError, match="before its first segment"):
        parse_moss_transcript("preface [0][S01]hello[1]")


def test_numeric_overflow_raises_inference_error():
    huge = "9" * 400
    with pytest.raises(MossTranscriptError, match="not finite"):
        parse_moss_transcript(f"[{huge}][S01]hello[{huge}]")


def test_end_before_start_yields_no_segment_and_raises():
    # end < start never closes a segment; non-empty output with nothing
    # recoverable is a hard model fault, not a silent empty transcript.
    with pytest.raises(MossTranscriptError):
        parse_moss_transcript("[5.00][S01]backwards[1.00]")
