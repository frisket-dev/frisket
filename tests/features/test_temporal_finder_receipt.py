from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from frisket.engine.executor.topic_sections_read import (
    topic_receipt_evidence,
    _topic_analysis_receipt,
)
from frisket.features.temporal.transcript_boundaries import lock_transcript_boundaries
from frisket.features.temporal_values import canonical_temporal_hash
from frisket.features.topic_segmentation.contracts import (
    BetweenUnits,
    BoundaryCandidate,
    DialogueUnit,
    SegmentationResult,
    SegmentationSnapshot,
    TOPIC_ANALYSIS_SIDECAR_COLUMN,
    TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
)


def _topic_value_and_sidecar() -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = SegmentationSnapshot(
        snapshot_hash="sha256:" + "b" * 64,
        source_kind="timestamped_transcript",
        language="en",
        units=(
            DialogueUnit(
                id="unit-a",
                ordinal=0,
                text="First topic",
                start_ms=0,
                end_ms=1_300,
            ),
            DialogueUnit(
                id="unit-b",
                ordinal=1,
                text="Second topic",
                start_ms=1_000,
                end_ms=2_000,
            ),
        ),
    )
    candidate = BoundaryCandidate(
        id="boundary-1",
        locator=BetweenUnits("unit-a", "unit-b"),
        strength=0.8,
    )
    segmentation = SegmentationResult(
        engine_id="test-engine",
        engine_version="1",
        boundaries=(candidate,),
        resolved_settings={"detail": "balanced"},
        diagnostics={"fixture": True},
    )
    locked = lock_transcript_boundaries(
        snapshot,
        segmentation.boundaries,
        timeline={
            "artifact_stable_id": "source_artifact:video-1",
            "fingerprint": "sha256:" + "a" * 64,
            "duration_ms": 3_000,
        },
    )
    analysis = _topic_analysis_receipt(segmentation, locked)
    analysis["not_receipted"] = "cell-only fixture fact"
    return locked.timeline_ranges.model_dump(mode="json"), {
        "schema_version": TOPIC_ANALYSIS_SIDECAR_SCHEMA_VERSION,
        "transcript_evidence_id": "evidence_link:1",
        "transcript_snapshot_hash": snapshot.snapshot_hash,
        "transcript_run_id": 9,
        "range_column_id": 41,
        "transcript_column_id": 40,
        "transcript_value_ref": {"kind": "run_result", "run_id": 9},
        "transcript_value_hash": "sha256:" + "d" * 64,
        "artifact_stable_id": "source_artifact:video-1",
        "timeline": locked.timeline_ranges.timeline.model_dump(mode="json"),
        "language": "en",
        **analysis,
    }


class _Project:
    def __init__(self, value: dict[str, Any], sidecar: dict[str, Any]) -> None:
        self.value = value
        self.sidecar = sidecar

    def get_values(
        self,
        sheet_id: int,
        column_id: int,
        *,
        row_ids: list[int],
        apply_edits: bool,
    ) -> dict[int, Any]:
        assert sheet_id == 3
        assert column_id in {41, 42}
        assert row_ids == [17]
        assert apply_edits is False
        return {17: self.value if column_id == 41 else self.sidecar}


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_topic_sidecar_binds_exact_value_without_visible_self_reference() -> None:
    value, sidecar = _topic_value_and_sidecar()
    expected_hash = canonical_temporal_hash("timeline_ranges", value)

    assert not _contains_key(value, "analysis_receipt")
    assert not _contains_key(value, "transcript_evidence_id")
    assert sidecar["locking"]["timeline_ranges_hash"] == expected_hash

    facts = SimpleNamespace(
        runner_spec={
            "action_kind": "map.find_topic_sections",
            "input_column": "Transcript",
            "output_name": "Topic sections",
            "engine": "test-engine",
            "settings": {"detail": "balanced"},
        },
        params_hash="sha256:" + "c" * 64,
        run_id=29,
        op_id=31,
        total_rows=1,
        completed_rows=1,
        failed_rows=0,
        failed_row_ids=[],
        sheet_id=3,
        row_ids=[17],
        input_column_ids={"Transcript": 40},
        cost_actual=0.0,
    )
    provenance = topic_receipt_evidence(
        _Project(value, sidecar),
        facts,
        [
            {
                "kind": "map_result_column",
                "name": "Topic sections",
                "type": "timeline_ranges",
                "sheet_id": 3,
                "column_id": 41,
                "run_id": 29,
                "op_id": 31,
                "row_ids": [17],
            },
        ],
        [
            {
                "kind": "map_result_column",
                "name": TOPIC_ANALYSIS_SIDECAR_COLUMN,
                "type": "json",
                "sheet_id": 3,
                "column_id": 42,
                "run_id": 29,
                "op_id": 31,
                "row_ids": [17],
            },
        ],
    )

    analysis_evidence = next(
        evidence.ref
        for evidence in provenance
        if evidence.ref.get("kind") == "topic_segmentation_analysis"
    )
    assert len(analysis_evidence["rows"]) == 1
    receipt_row = analysis_evidence["rows"][0]
    assert receipt_row["row_id"] == 17
    assert receipt_row["transcript_evidence_id"] == "evidence_link:1"
    assert receipt_row["timeline_ranges_hash"] == expected_hash
    assert "timeline_ranges_hash" not in receipt_row["locking"]
    assert "not_receipted" not in receipt_row
    assert expected_hash not in json.dumps(value, sort_keys=True)
    assert analysis_evidence["sidecar_column_id"] == 42
