import pytest

from frisket.engine.store import Project
from frisket.engine.store.staleness import compute_sync_states


@pytest.mark.parametrize(
    "kind,keys",
    [
        ("semantic_join_link_source", ("source_sheet_id", "target_sheet_id")),
        ("joined_tables_source", ("left_sheet_id", "right_sheet_id")),
    ],
)
def test_uncaptured_empty_table_keeps_dependencies_but_freshness_is_unknown(
    tmp_path, kind, keys
):
    project = Project.create(tmp_path / "empty-join.frisket")
    try:
        left = project.add_sheet("Left")
        right = project.add_sheet("Right")
        column = project.add_column(right, "key", type="integer")
        row = project.add_rows(right, [{"key": 1}], {"key": column})[0]
        op = project.append_op(
            "derive.join", {"reads": [{"kind": kind, keys[0]: left, keys[1]: right}]}
        )
        child = project.add_sheet("Empty joined", parent_sheet_id=left, parent_op_id=op)
        assert project.visible_row_ids(child) == []
        assert compute_sync_states(project)[child]["sync_state"] == "unknown"
        for source in (left, right):
            assert project.dependent_sheets(source) == [
                {"id": child, "name": "Empty joined"}
            ]
        project.apply_edits(
            [{"row_id": row, "column_id": column, "value": 2}],
            label="right source changed",
        )
        assert compute_sync_states(project)[child]["sync_state"] == "unknown"
        project.delete_sheet(right)
        assert child in {sheet["id"] for sheet in project.sheets()}
        assert compute_sync_states(project)[child]["sync_state"] == "unknown"
    finally:
        project.close()


@pytest.mark.parametrize("semantic", [False, True])
def test_real_empty_join_captures_both_actual_source_sides(tmp_path, semantic):
    from frisket.engine.executor.actions import run_action_spec
    from frisket.engine.store.op_log import step_operation

    project = Project.create(tmp_path / "actual-empty.frisket")
    try:
        if semantic:
            from test_derive_link_table_executor import _seed, _derive_link_action

            seeded = _seed(project, tmp_path)
            left, right = seeded["source_sheet_id"], seeded["target_sheet_id"]
            match_column = next(
                c["id"]
                for c in project.columns(left)
                if c["name"] == "company_match_row_id"
            )
            project.apply_edits(
                [
                    {"row_id": row, "column_id": match_column, "value": None}
                    for row in seeded["source_row_ids"]
                ]
            )
            request = _derive_link_action(
                seeded["join_receipt_id"], target_sheet_name="Empty joined"
            )
            column = next(
                c["id"] for c in project.columns(right) if c["name"] == "company"
            )
            row = seeded["target_row_ids"][0]
        else:
            left = project.add_sheet("Left")
            project.add_column(left, "key", type="integer")
            right = project.add_sheet("Right")
            column = project.add_column(right, "key", type="integer")
            row = project.add_rows(right, [{"key": 1}], {"key": column})[0]
            request = {
                "action_id": "derive.join",
                "scope": {"kind": "sheet_rows", "sheet_id": left},
                "sheet_name": "Empty joined",
                "params": {
                    "right": {"sheet_id": right},
                    "join_keys": [{"left_column": "key", "right_column": "key"}],
                },
                "idempotency_key": "actual-empty-join",
            }
        result = run_action_spec(project, request, project_id="empty-join")
        assert result.status == "completed", result.errors
        child = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert project.visible_row_ids(child) == []
        assert compute_sync_states(project)[child]["sync_state"] == "synced"
        for source in (left, right):
            assert {"id": child, "name": "Empty joined"} in project.dependent_sheets(
                source
            )
        value = project.get_values(right, column)[row]
        edit = project.apply_edits(
            [{"row_id": row, "column_id": column, "value": value}]
        )
        assert compute_sync_states(project)[child]["sync_state"] == "stale"
        with project.transaction_scope():
            step_operation(project, "revert", op_id=edit)
        assert compute_sync_states(project)[child]["sync_state"] == "synced"
        project.delete_sheet(right)
        assert child in {sheet["id"] for sheet in project.sheets()}
        assert project.visible_row_ids(child) == []
        assert compute_sync_states(project)[child]["sync_state"] == "stale"
    finally:
        project.close()
