from __future__ import annotations

import math

import pytest

from frisket.pdf_packets import PhraseRule, match_packet_pages


def test_feedback_groups_start_kinds_and_only_demotes_pages_near_a_rejection():
    vectors = {
        1: [1.0, 0.0],
        2: [0.98, 0.20],
        3: [0.97, 0.24],
        4: [0.0, 1.0],
        5: [0.10, 0.99],
    }

    learned = match_packet_pages(
        page_count=5,
        vectors=vectors,
        confirmed={1, 4},
        rejected={2},
        threshold=90,
    )

    assert [kind.confirmed_pages for kind in learned.kinds] == [(1,), (4,)]
    assert 3 not in learned.suggested_pages
    assert 5 in learned.suggested_pages
    assert learned.pages[2].visual_score < 90
    assert learned.pages[4].visual_score > 99

    regrouped = match_packet_pages(
        page_count=5,
        vectors=vectors,
        confirmed={1, 2, 4},
        threshold=90,
    )
    assert [kind.confirmed_pages for kind in regrouped.kinds] == [(1, 2), (4,)]
    assert regrouped.pages[2].kind_id == regrouped.pages[0].kind_id


def test_questions_cover_distinct_start_kinds_before_near_duplicates():
    a_score = 0.89
    b_score = 0.87
    vectors = {
        1: [1.0, 0.0, 0.0],
        2: [0.0, 1.0, 0.0],
        3: [a_score, math.sqrt(1 - a_score**2), 0.0],
        4: [0.88, math.sqrt(1 - 0.88**2), 0.0],
        5: [math.sqrt(1 - b_score**2), b_score, 0.0],
    }

    result = match_packet_pages(
        page_count=5,
        vectors=vectors,
        confirmed={1, 2},
        threshold=90,
    )

    first_two_kinds = {
        result.pages[page - 1].kind_id for page in result.question_pages[:2]
    }
    assert first_two_kinds == {1, 2}
    assert result.question_pages[0] == 3
    assert len(result.question_pages) == 3


def test_text_phrases_are_or_matches_with_bounded_ocr_error_tolerance():
    result = match_packet_pages(
        page_count=5,
        vectors={1: [1.0, 0.0]},
        confirmed={1},
        rejected={4},
        threshold=90,
        ocr_text={
            2: "NOTICE OF DETERMLNATION",
            3: "This is the final report.",
            4: "Notice of determination",
            5: "Agxncy memxrandxm",
        },
        phrases=(
            PhraseRule("notice of determination", fuzzy=True),
            PhraseRule("final report"),
            PhraseRule("agency memorandum", fuzzy=True),
            PhraseRule("disabled phrase", enabled=False),
        ),
    )

    assert result.suggested_pages == (2, 3)
    assert result.pages[1].visual_score is None
    assert result.pages[1].matched_phrases == ("notice of determination",)
    assert result.pages[2].matched_phrases == ("final report",)
    assert result.pages[3].matched_phrases == ("notice of determination",)
    assert result.pages[3].suggested is False
    assert result.pages[4].matched_phrases == ()


def test_suggestions_never_become_training_examples_without_confirmation():
    inputs = dict(
        page_count=3,
        vectors={1: [1.0, 0.0], 2: [0.98, 0.2], 3: [0.91, 0.41]},
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


def test_visual_scores_normalize_vectors_and_use_zero_to_100_thresholds():
    result = match_packet_pages(
        page_count=3,
        vectors={1: [10.0, 0.0], 2: [8.0, 6.0], 3: [-10.0, 0.0]},
        confirmed={1},
        threshold=80,
    )

    assert result.pages[1].visual_score == 80.0
    assert result.pages[1].suggested is True
    assert result.pages[2].visual_score == 0.0
    assert all(
        page.visual_score is None or 0 <= page.visual_score <= 100
        for page in result.pages
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"vectors": {1: [1.0, math.inf]}}, "finite"),
        ({"vectors": {1: [1.0, 0.0], 2: [1.0]}}, "dimension"),
        ({"rejected": {4}}, "page"),
        ({"confirmed": {1, 2}, "rejected": {2}}, "disjoint"),
        ({"threshold": 101}, "threshold"),
    ],
)
def test_invalid_match_inputs_are_rejected(overrides, message):
    inputs = {
        "page_count": 3,
        "vectors": {1: [1.0, 0.0]},
        "confirmed": {1},
        "rejected": set(),
        "threshold": 80,
    }
    inputs.update(overrides)

    with pytest.raises(ValueError, match=message):
        match_packet_pages(**inputs)
