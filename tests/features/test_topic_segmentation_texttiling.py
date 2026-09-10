from __future__ import annotations

import pytest

from frisket.features.topic_segmentation import (
    BetweenUnits,
    DialogueUnit,
    SegmentationCancelled,
    SegmentationContext,
    SegmentationSnapshot,
    TextTilingSegmenter,
    WithinUnit,
)


FRUIT = "apple orchard cider pear harvest fruit "
SPACE = "rocket orbit galaxy astronaut launch planet "


def _snapshot(texts: list[str], *, language: str | None = "en") -> SegmentationSnapshot:
    return SegmentationSnapshot(
        snapshot_hash="sha256:texttiling-fixture",
        source_kind="untimed_transcript",
        language=language,
        units=tuple(
            DialogueUnit(id=f"u{index}", ordinal=index, text=text)
            for index, text in enumerate(texts)
        ),
    )


def test_texttiling_finds_an_obvious_boundary_between_source_units() -> None:
    snapshot = _snapshot(
        [FRUIT * 4, FRUIT * 4, SPACE * 4, SPACE * 4],
        language=None,
    )
    engine = TextTilingSegmenter()

    assert engine.preflight(snapshot, {}).ok
    result = engine.segment(snapshot, {}, SegmentationContext())

    assert result.resolved_settings == {"detail": "balanced"}
    assert len(result.boundaries) == 1
    assert result.boundaries[0].locator == BetweenUnits(
        left_unit_id="u1", right_unit_id="u2"
    )
    assert result.boundaries[0].strength is not None
    assert result.boundaries[0].strength > 0.9


def test_texttiling_preserves_a_native_word_gap_inside_one_unit() -> None:
    snapshot = _snapshot([FRUIT * 5 + SPACE * 5, SPACE * 3])

    result = TextTilingSegmenter().segment(
        snapshot, {"detail": "balanced"}, SegmentationContext()
    )

    assert len(result.boundaries) == 1
    candidate = result.boundaries[0]
    assert candidate.locator == WithinUnit(unit_id="u0")
    assert 24 <= candidate.diagnostics["token_offset_in_right_unit"] <= 36


def test_texttiling_detail_only_changes_the_depth_cutoff() -> None:
    snapshot = _snapshot([FRUIT * 5 + SPACE * 5, SPACE * 3])
    engine = TextTilingSegmenter()

    results = {
        detail: engine.segment(snapshot, {"detail": detail}, SegmentationContext())
        for detail in ("fewer", "balanced", "more")
    }

    assert (
        len(results["fewer"].boundaries)
        <= len(results["balanced"].boundaries)
        <= len(results["more"].boundaries)
    )
    assert results["fewer"].diagnostics["algorithm"] == "texttiling"


def test_texttiling_fails_honestly_without_enough_lexical_signal() -> None:
    engine = TextTilingSegmenter()
    too_short = _snapshot(["hello there", "new subject"])
    no_repetition = _snapshot(
        [
            "one two three four five six seven eight nine ten eleven",
            "twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty",
        ]
    )

    assert engine.preflight(too_short, {}).error_code == "insufficient_text"
    assert (
        engine.preflight(no_repetition, {}).error_code == "insufficient_lexical_signal"
    )


def test_texttiling_observes_cancellation_before_analysis() -> None:
    snapshot = _snapshot([FRUIT * 4, FRUIT * 4, SPACE * 4, SPACE * 4])

    with pytest.raises(SegmentationCancelled):
        TextTilingSegmenter().segment(
            snapshot,
            {},
            SegmentationContext(cancelled=lambda: True),
        )
