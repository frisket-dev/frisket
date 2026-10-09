from __future__ import annotations

import math

import pytest

from frisket.pdf_packets import PhraseRule, match_packet_pages


ALL_BITS = (1 << 256) - 1


def changed_bits(count: int) -> int:
    return (1 << count) - 1


def test_feedback_groups_start_kinds_and_vetoes_when_a_rejection_is_closer():
    signatures = {
        1: 0,
        2: changed_bits(5),
        3: changed_bits(6),
        4: ALL_BITS,
        5: ALL_BITS ^ changed_bits(2),
    }

    learned = match_packet_pages(
        page_count=5,
        signatures=signatures,
        confirmed={1, 4},
        rejected={2},
        threshold=90,
    )

    assert [kind.confirmed_pages for kind in learned.kinds] == [(1,), (4,)]
    assert learned.pages[2].visual_score == 97.7
    assert 3 not in learned.suggested_pages
    assert 3 in learned.unsure_pages
    assert 5 in learned.suggested_pages
    assert learned.pages[4].visual_score == 99.2

    regrouped = match_packet_pages(
        page_count=5,
        signatures=signatures,
        confirmed={1, 2, 4},
        threshold=90,
    )
    assert [kind.confirmed_pages for kind in regrouped.kinds] == [(1, 2), (4,)]
    assert regrouped.pages[1].kind_id == regrouped.pages[0].kind_id


def test_equal_positive_and_negative_match_stays_unsure_above_threshold():
    result = match_packet_pages(
        page_count=3,
        signatures={1: 0, 2: changed_bits(2), 3: changed_bits(1)},
        confirmed={1},
        rejected={2},
        threshold=90,
    )

    assert result.pages[2].visual_score == 99.6
    assert result.pages[2].suggested is False
    assert result.unsure_pages == (3,)


def test_negative_example_does_not_fill_unsure_queue_with_unrelated_pages():
    result = match_packet_pages(
        page_count=4,
        signatures={1: 0, 2: ALL_BITS, 3: ALL_BITS, 4: ALL_BITS},
        confirmed={1},
        rejected={2},
        threshold=80,
        ocr_text={4: "Memorandum for:"},
        phrases=(PhraseRule("Memorandum for:"),),
    )

    assert result.suggested_pages == ()
    assert result.unsure_pages == (4,)


def test_unsure_band_edges_labels_and_phrase_hits():
    result = match_packet_pages(
        page_count=7,
        signatures={
            1: 0,
            2: changed_bits(50),
            3: changed_bits(51),
            4: changed_bits(77),
            5: changed_bits(102),
            6: changed_bits(102) << 154,
            7: changed_bits(103),
        },
        confirmed={1},
        rejected={6},
        threshold=80.2,
        ocr_text={3: "Notice of determination"},
        phrases=(PhraseRule("notice of determination"),),
    )

    assert result.pages[4].visual_score == 60.2
    assert result.suggested_pages == (2, 3)
    assert result.unsure_pages == (4, 5)


def test_unsure_pages_cover_the_whole_packet_in_page_order():
    signatures = {1: 0}
    signatures.update(
        {page: changed_bits(bits) for page, bits in zip(range(2, 8), range(27, 33))}
    )

    result = match_packet_pages(
        page_count=7,
        signatures=signatures,
        confirmed={1},
        threshold=90,
    )

    assert result.unsure_pages == (2, 3, 4, 5, 6, 7)


def test_text_phrases_converge_with_visual_feedback_and_rejections_win_conflicts():
    result = match_packet_pages(
        page_count=6,
        signatures={
            1: 0,
            2: changed_bits(20) << 100,
            4: changed_bits(6),
            5: changed_bits(7),
        },
        confirmed={1},
        rejected={4},
        threshold=90,
        ocr_text={
            2: "NOTICE OF DETERMLNATION",
            3: "This is the final report.",
            4: "Notice of determination",
            5: "Notice of determination",
            6: "Agxncy memxrandxm",
        },
        phrases=(
            PhraseRule("notice of determination", fuzzy=True),
            PhraseRule("final report"),
            PhraseRule("agency memorandum", fuzzy=True),
            PhraseRule("disabled phrase", enabled=False),
        ),
    )

    assert result.suggested_pages == (2, 3)
    assert result.pages[1].visual_score == 92.2
    assert result.pages[1].matched_phrases == ("notice of determination",)
    assert result.pages[2].visual_score is None
    assert result.pages[2].matched_phrases == ("final report",)
    assert result.pages[3].matched_phrases == ("notice of determination",)
    assert result.pages[3].suggested is False
    assert result.pages[4].matched_phrases == ("notice of determination",)
    assert result.pages[4].suggested is False
    assert 5 in result.unsure_pages
    assert result.pages[5].matched_phrases == ()


def test_suggestions_never_become_training_examples_without_confirmation():
    inputs = dict(
        page_count=3,
        signatures={1: 0, 2: changed_bits(5), 3: changed_bits(23)},
        confirmed={1},
        threshold=90,
    )

    first = match_packet_pages(**inputs)
    repeated = match_packet_pages(**inputs)

    assert first == repeated
    assert first.kinds[0].confirmed_pages == (1,)
    assert first.suggested_pages == (2, 3)

    accepted = match_packet_pages(**{**inputs, "confirmed": {1, 2}})
    assert accepted.kinds[0].confirmed_pages == (1, 2)
    assert 2 not in accepted.suggested_pages


def test_visual_scores_use_native_hamming_and_zero_to_100_thresholds():
    result = match_packet_pages(
        page_count=3,
        signatures={1: 0, 2: changed_bits(51), 3: ALL_BITS},
        confirmed={1},
        threshold=80,
    )

    assert result.pages[1].visual_score == 80.1
    assert result.pages[1].suggested is True
    assert result.pages[2].visual_score == 0.0
    assert all(
        page.visual_score is None or 0 <= page.visual_score <= 100
        for page in result.pages
    )


def test_identical_empty_signatures_are_valid_and_confirmed_only_is_stable():
    result = match_packet_pages(
        page_count=2,
        signatures={1: 0, 2: 0},
        confirmed={1, 2},
        threshold=80,
    )

    assert result.kinds[0].confirmed_pages == (1, 2)
    assert result.pages[0].visual_score == 100.0
    assert result.pages[1].visual_score == 100.0
    assert result.suggested_pages == ()
    assert result.unsure_pages == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"signatures": {1: True}}, "256-bit"),
        ({"signatures": {1: -1}}, "256-bit"),
        ({"signatures": {1: 1 << 256}}, "256-bit"),
        ({"rejected": {4}}, "page"),
        ({"confirmed": {1, 2}, "rejected": {2}}, "disjoint"),
        ({"threshold": math.inf}, "threshold"),
    ],
)
def test_invalid_match_inputs_are_rejected(overrides, message):
    inputs = {
        "page_count": 3,
        "signatures": {1: 0},
        "confirmed": {1},
        "rejected": set(),
        "threshold": 80,
    }
    inputs.update(overrides)

    with pytest.raises(ValueError, match=message):
        match_packet_pages(**inputs)
