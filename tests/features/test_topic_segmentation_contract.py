from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from frisket.features.topic_segmentation import (
    ENGINES,
    ENGINE_BY_ID,
    BetweenUnits,
    BoundaryCandidate,
    DialogueSegmenter,
    DialogueUnit,
    ExactTime,
    SegmentationSettingsError,
    SegmentationSnapshot,
    UnknownDialogueEngineError,
    WithinUnit,
    engine_catalog,
    get_segmenter,
    validate_detail_settings,
)


def test_source_locator_contract_is_small_typed_and_immutable() -> None:
    between = BetweenUnits(left_unit_id="u1", right_unit_id="u2")
    within = WithinUnit(unit_id="u2")
    exact = ExactTime(at_ms=1_250)

    assert between.kind == "between_units"
    assert within.kind == "within_unit"
    assert exact.kind == "exact_time"
    assert BoundaryCandidate(id="b1", locator=between).locator is between

    with pytest.raises(FrozenInstanceError):
        between.left_unit_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="different units"):
        BetweenUnits(left_unit_id="same", right_unit_id="same")
    with pytest.raises(ValueError, match="non-negative"):
        ExactTime(at_ms=-1)


def test_snapshot_enforces_order_identity_and_timing_mode() -> None:
    timed_units = (
        DialogueUnit("u1", 0, "one", start_ms=0, end_ms=1_000),
        DialogueUnit("u2", 1, "two", start_ms=900, end_ms=2_000),
    )
    snapshot = SegmentationSnapshot(
        snapshot_hash="sha256:test",
        source_kind="timestamped_transcript",
        language="en",
        units=timed_units,
    )
    assert snapshot.units == timed_units

    with pytest.raises(ValueError, match="all carry timing"):
        SegmentationSnapshot(
            snapshot_hash="sha256:test",
            source_kind="timestamped_transcript",
            language=None,
            units=(DialogueUnit("u1", 0, "untimed"),),
        )
    with pytest.raises(ValueError, match="duplicate"):
        SegmentationSnapshot(
            snapshot_hash="sha256:test",
            source_kind="untimed_transcript",
            language=None,
            units=(
                DialogueUnit("u1", 0, "one"),
                DialogueUnit("u1", 1, "two"),
            ),
        )


def test_one_closed_detail_setting_has_a_materialized_default() -> None:
    assert validate_detail_settings(None) == {"detail": "balanced"}
    assert validate_detail_settings({"detail": "fewer"}) == {"detail": "fewer"}

    with pytest.raises(SegmentationSettingsError, match="unknown"):
        validate_detail_settings({"window": 10})
    with pytest.raises(SegmentationSettingsError, match="detail must"):
        validate_detail_settings({"detail": "maximum"})


def test_engine_roster_and_catalog_have_one_immutable_source() -> None:
    ids = tuple(engine.definition.id for engine in ENGINES)

    assert ids == ("deep_tiling", "texttiling")
    assert tuple(ENGINE_BY_ID) == ids
    assert tuple(option["id"] for option in engine_catalog()) == ids
    # Both dialogue
    # segmentation engines run in-process, never the old local:bool + source
    # string pair.
    assert all(option["tier"] == "local" for option in engine_catalog())
    assert all(
        "local" not in option and "source" not in option for option in engine_catalog()
    )
    assert all(isinstance(engine, DialogueSegmenter) for engine in ENGINES)
    assert get_segmenter("texttiling") is ENGINE_BY_ID["texttiling"]

    with pytest.raises(TypeError):
        ENGINE_BY_ID["fake"] = ENGINES[0]  # type: ignore[index]
    with pytest.raises(UnknownDialogueEngineError, match="unknown"):
        get_segmenter("not-an-engine")
