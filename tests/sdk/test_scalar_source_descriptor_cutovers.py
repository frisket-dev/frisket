from __future__ import annotations


import pytest

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import (
    TypedMapRowsPlanError,
    build_typed_map_rows_plan,
)
from frisket.engine.store import Project


@pytest.mark.parametrize(
    "action_id,output_key,accepted_types,source_type",
    [
        ("media.extract_metadata", "details", ["image", "audio", "video", "file"], kind)
        for kind in ("image", "audio", "video", "file")
    ]
    + [
        ("map.find_visual_cuts", "cuts", ["video"], "video"),
        (
            "map.find_topic_sections",
            "sections",
            ["timestamped_transcript"],
            "timestamped_transcript",
        ),
    ],
)
def test_typed_source_enforces_its_declared_media_types(
    tmp_path,
    action_id: str,
    output_key: str,
    accepted_types: list[str],
    source_type: str,
) -> None:
    project = Project.create(tmp_path / "metadata.frisket", name="Metadata source")
    try:
        sheet_id = project.add_sheet("Inputs")
        column_id = project.add_column(sheet_id, "accepted", type=source_type)
        project.add_column(sheet_id, "rejected", type="geo_point")

        def request(source: str):
            return typed_action_for_request(
                {
                    "action_id": action_id,
                    "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                    "params": {"source": source},
                    "output_names": {output_key: "Analysis"},
                    "idempotency_key": "metadata-source-types",
                }
            )

        accepted = build_typed_map_rows_plan(project, request("accepted"))
        assert accepted.source_columns == ("accepted",)
        assert accepted.source_column_ids == {"accepted": column_id}
        assert accepted.source_column_types == {"accepted": source_type}
        with pytest.raises(TypedMapRowsPlanError) as refused:
            build_typed_map_rows_plan(project, request("rejected"))
        assert refused.value.code == "invalid_input_ref"
        assert refused.value.details["columns"] == [
            {
                "name": "rejected",
                "actual_type": "geo_point",
                "accepted_column_types": accepted_types,
            }
        ]
    finally:
        project.close()
