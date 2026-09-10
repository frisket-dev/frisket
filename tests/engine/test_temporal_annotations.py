from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from pathlib import Path

import pytest

from frisket.engine.executor.temporal_annotations import (
    annotation_intersects_source_range,
    assign_temporal_annotation_output_names,
    project_resolved_temporal_annotation,
    resolve_compatible_temporal_annotations,
    revalidate_temporal_annotation,
)
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import (
    TimelineError,
    ensure_artifact_timeline,
    resolve_timeline,
    write_rate1_timeline_segment,
)
from frisket.engine.store.evidence import record_source_artifact


def _temporal_value(
    schema_version: str,
    timeline: dict,
    key: str,
    item_or_items: object,
) -> dict:
    return {
        "schema_version": schema_version,
        "timeline": timeline,
        key: item_or_items,
    }


def test_resolver_keeps_only_same_row_compatible_typed_cells_and_maps_child_clock(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "annotations.frisket")
    try:
        sheet_id = project.add_sheet("Media")
        video_column = project.add_column(sheet_id, "video", "video")
        point_column = project.add_column(sheet_id, "markers", "timeline_points")
        child_range_column = project.add_column(
            sheet_id, "child notes", "timeline_range"
        )
        selection_column = project.add_column(
            sheet_id, "selected ranges", "timeline_ranges"
        )
        incompatible_column = project.add_column(
            sheet_id, "other timeline", "timeline_point"
        )
        summary_column = project.add_column(sheet_id, "summary", "text")
        blob_hash = project.add_blob(
            b"source media",
            filename="source.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(probe={"duration_seconds": 10.0}),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "video": {
                        "blob": blob_hash,
                        "filename": "source.mp4",
                        "mime": "video/mp4",
                    },
                    "summary": "must never be inherited",
                }
            ],
            {"video": video_column, "summary": summary_column},
        )[0]
        source = resolve_timeline(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=video_column,
        )

        child_blob = project.add_blob(
            b"derived bytes",
            filename="child.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(probe={"duration_seconds": 2.0}),
        )
        child_artifact = record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=child_blob,
            duration_ms=2_000,
        )
        write_rate1_timeline_segment(
            project,
            derived_artifact_id=int(child_artifact["id"]),
            source_artifact_id=source.anchor.artifact_id,
            source_start_ms=4_000,
            source_end_ms=6_000,
            precision="exact",
        )
        child_anchor = ensure_artifact_timeline(project, int(child_artifact["id"]))

        other_blob = project.add_blob(
            b"other root",
            filename="other.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(probe={"duration_seconds": 10.0}),
        )
        other_artifact = record_source_artifact(
            project,
            artifact_kind="av",
            media_type="video/mp4",
            blob_hash=other_blob,
            duration_ms=10_000,
        )
        other_anchor = ensure_artifact_timeline(project, int(other_artifact["id"]))

        project.apply_edits(
            [
                {
                    "row_id": row_id,
                    "column_id": point_column,
                    "value": _temporal_value(
                        "frisket.timeline_points.v1",
                        source.anchor.wire_value(),
                        "items",
                        [
                            {"id": "p1", "at_ms": 3_500},
                            {"id": "p2", "at_ms": 8_000},
                        ],
                    ),
                },
                {
                    "row_id": row_id,
                    "column_id": child_range_column,
                    "value": _temporal_value(
                        "frisket.timeline_range.v1",
                        child_anchor.wire_value(),
                        "item",
                        {
                            "id": "child-local",
                            "start_ms": 0,
                            "end_ms": 500,
                            "label": "Child note",
                            "metadata": {"kept": True},
                        },
                    ),
                },
                {
                    "row_id": row_id,
                    "column_id": selection_column,
                    "value": _temporal_value(
                        "frisket.timeline_ranges.v1",
                        source.anchor.wire_value(),
                        "items",
                        [{"id": "selection", "start_ms": 3_000, "end_ms": 5_000}],
                    ),
                },
                {
                    "row_id": row_id,
                    "column_id": incompatible_column,
                    "value": _temporal_value(
                        "frisket.timeline_point.v1",
                        other_anchor.wire_value(),
                        "item",
                        {"id": "other", "at_ms": 4_000},
                    ),
                },
            ],
            label="seed temporal annotations",
        )

        annotations = resolve_compatible_temporal_annotations(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            source_anchor=source.anchor,
            excluded_column_ids=(selection_column,),
        )
        assert [annotation.column_id for annotation in annotations] == [
            point_column,
            child_range_column,
        ]
        assert annotations[0].source_to_annotation_offset_ms == 0
        assert annotations[1].source_to_annotation_offset_ms == -4_000
        assert annotations[0].input_ref()["value_ref"]["kind"] == "manual_edit"

        assert annotation_intersects_source_range(
            annotations[0], source_start_ms=3_000, source_end_ms=5_000
        )
        assert annotation_intersects_source_range(
            annotations[1], source_start_ms=3_000, source_end_ms=5_000
        )
        derived_timeline = {
            "artifact_stable_id": "source_artifact:output",
            "fingerprint": "sha256:" + ("c" * 64),
            "duration_ms": 2_000,
        }
        child_projection = project_resolved_temporal_annotation(
            annotations[1],
            source_start_ms=3_000,
            source_end_ms=5_000,
            derived_timeline=derived_timeline,
        )
        assert child_projection is not None
        assert child_projection.value["item"] == {
            "id": "child-local",
            "label": "Child note",
            "metadata": {"kept": True},
            "start_ms": 1_000,
            "end_ms": 1_500,
        }

        names = assign_temporal_annotation_output_names(
            annotations,
            used_names={"clip", "clip_markers"},
            prefix="clip_",
        )
        assert names == {
            point_column: "clip_markers 2",
            child_range_column: "clip_child notes",
        }

        for annotation in annotations:
            revalidate_temporal_annotation(project, annotation)
        project.apply_edits(
            [
                {
                    "row_id": row_id,
                    "column_id": point_column,
                    "value": _temporal_value(
                        "frisket.timeline_points.v1",
                        source.anchor.wire_value(),
                        "items",
                        [{"id": "changed", "at_ms": 3_600}],
                    ),
                }
            ]
        )
        with pytest.raises(TimelineError, match="changed during execution"):
            revalidate_temporal_annotation(project, annotations[0])
    finally:
        project.close()
