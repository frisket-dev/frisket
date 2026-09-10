from __future__ import annotations

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project


@pytest.mark.parametrize(
    ("kind", "params"),
    [
        (
            "temporal.extract_range",
            {
                "source": "video",
                "selection": {"kind": "column", "column": "ranges"},
            },
        ),
        (
            "derive.temporal_segments",
            {
                "source": "video",
                "selection": {"kind": "column", "column": "cuts"},
            },
        ),
    ],
)
def test_temporal_actions_accept_explicit_scopes_above_runner_page_size(
    kind: str,
    params: dict[str, object],
) -> None:
    row_ids = list(range(1, 502))

    result = validate_root_action(
        {
            "action_id": kind,
            "scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": row_ids},
            "params": params,
            **({"sheet_name": "Segments"} if kind.startswith("derive.") else {}),
            "idempotency_key": f"{kind}@sha256:over-500-row-scope",
        }
    )

    assert result.ok, result.error
    assert result.action is not None
    bound = BoundTypedActionRequest.bind(ACTION_REGISTRY.get(kind), result.action)
    assert list(bound.request.scope.row_ids) == row_ids


def test_typed_transcript_binds_scope_above_runner_page_size() -> None:
    row_ids = list(range(1, 502))
    result = validate_root_action(
        {
            "action_id": "derive.transcript_segments",
            "scope": {"kind": "sheet_rows", "sheet_id": 1, "row_ids": row_ids},
            "params": {
                "source": "transcript",
                "selection": {"kind": "column", "column": "ranges"},
            },
            "sheet_name": "Transcript segments",
            "idempotency_key": "transcript-over-500-row-scope",
        }
    )
    assert result.ok, result.error
    assert result.action is not None
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get("derive.transcript_segments"), result.action
    )
    assert list(bound.request.scope.row_ids) == row_ids


@pytest.mark.parametrize(
    "action_id,source_type,key",
    [
        ("map.find_visual_cuts", "video", "cuts"),
        ("map.find_topic_sections", "timestamped_transcript", "sections"),
    ],
)
def test_typed_finder_binds_explicit_scope_above_runner_page_size(
    tmp_path, action_id, source_type, key
) -> None:
    project = Project.create(tmp_path / "visual-scope.frisket")
    try:
        sheet_id = project.add_sheet("Videos")
        column_id = project.add_column(sheet_id, "video", source_type)
        row_ids = project.add_rows(
            sheet_id, [{"video": None} for _ in range(501)], {"video": column_id}
        )
        result = validate_root_action(
            {
                "action_id": action_id,
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": row_ids,
                },
                "params": {"source": "video"},
                "output_names": {key: "Analysis"},
                "idempotency_key": "visual-cuts-over-500-row-scope",
            }
        )
        assert result.ok, result.error
        assert result.action is not None
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get(action_id), result.action
        )
        plan = build_typed_map_rows_plan(project, bound)
        assert list(bound.request.scope.row_ids) == row_ids
        assert plan.spec_dict()["row_ids"] == row_ids
        assert plan.source_columns == ("video",)
    finally:
        project.close()
