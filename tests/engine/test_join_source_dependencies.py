import pytest

from frisket.engine.store import Project
from frisket.engine.store.sheet_lifecycle import SheetDeleteBlocked
from frisket.engine.store.staleness import compute_sync_states


@pytest.mark.parametrize(
    "kind,keys",
    [
        ("semantic_join_link_source", ("source_sheet_id", "target_sheet_id")),
        ("joined_tables_source", ("left_sheet_id", "right_sheet_id")),
    ],
)
def test_empty_table_keeps_both_admitted_sheet_dependencies(tmp_path, kind, keys):
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
        assert compute_sync_states(project)[child]["sync_state"] == "synced"
        for source in (left, right):
            assert project.dependent_sheets(source) == [
                {"id": child, "name": "Empty joined"}
            ]
            with pytest.raises(SheetDeleteBlocked):
                project.delete_sheet(source)
        project.apply_edits(
            [{"row_id": row, "column_id": column, "value": 2}],
            label="right source changed",
        )
        assert compute_sync_states(project)[child]["sync_state"] == "stale"
    finally:
        project.close()
