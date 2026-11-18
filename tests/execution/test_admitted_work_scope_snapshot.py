"""Admission fixes selection; execution reads current values within that selection."""

from contextlib import closing
from types import SimpleNamespace

import pytest

from frisket.engine.runner.row_inputs import row_values
from frisket.engine.store import Project
from frisket.execution.scope_identity import (
    AdmittedWorkScopeSnapshot,
    canonical_work_scope_binding,
)


@pytest.mark.parametrize("replacement", ["changed", "original"])
def test_admitted_selection_allows_live_edits_but_not_new_rows(tmp_path, replacement):
    with closing(Project.create(tmp_path / "source.frisket")) as project:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "text")
        row_id = project.add_rows(sheet, [{"text": "original"}], {"text": column})[0]
        program = SimpleNamespace(source_columns=lambda _spec: ["text"])
        spec = {"sheet_id": sheet}
        payload = {}
        binding = canonical_work_scope_binding(
            project, program, spec, [row_id], snapshot=payload
        )
        snapshot = AdmittedWorkScopeSnapshot(payload)
        assert payload["row_ids"] == [row_id]
        assert payload["sources"] == [{"name": "text", "column_id": column}]

        def read(selected_row):
            return row_values(
                project,
                program,
                spec,
                {"text": column},
                selected_row,
                for_model=False,
                column_types={"text": "text"},
                admitted_work_scope=snapshot,
            )

        before = read(row_id)
        assert before == {"text": "original"}
        project.apply_edits(
            [{"row_id": row_id, "column_id": column, "value": replacement}]
        )
        added = project.add_rows(sheet, [{"text": "new row"}], {"text": column})[0]
        after = read(row_id)
        assert after == {"text": replacement}
        assert after.source_cells != before.source_cells
        # Input edits do not invalidate the approved selection or require a new quote.
        assert canonical_work_scope_binding(project, program, spec, [row_id]) == binding
        with pytest.raises(ValueError, match="outside the admitted selection"):
            read(added)
        assert snapshot["row_ids"] == [row_id]
