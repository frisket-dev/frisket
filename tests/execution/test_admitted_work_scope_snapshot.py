import json
from types import SimpleNamespace

import pytest

from frisket.engine.runner.row_inputs import _expected_source_cell, row_values
from frisket.engine.store import Project
from frisket.execution.scope_identity import (
    AdmittedWorkScopeSnapshot,
    canonical_work_scope_binding,
)
from frisket.ops.base import RecipeInvocationHalt


def test_expected_cells_are_indexed_once_without_changing_the_snapshot():
    class CountingCells(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    first = {"row_id": 1, "value_hash": "first"}
    second = {"row_id": 2, "value_hash": "second"}
    cells = CountingCells([first, second, {"row_id": 1, "value_hash": "duplicate"}])
    payload = {"sources": [{"name": "text", "column_id": 4, "cells": cells}]}
    snapshot = AdmittedWorkScopeSnapshot(payload)
    assert cells.iterations == 1
    for _ in range(10):
        for row_id, expected in ((1, first), (2, second)):
            assert (
                _expected_source_cell(
                    snapshot, source_index=0, name="text", column_id=4, row_id=row_id
                )
                is expected
            )
    assert cells.iterations == 1
    assert json.dumps(snapshot) == json.dumps(payload)


@pytest.mark.parametrize("mutation", ["row_id", "column_id", "name", "cells"])
def test_changed_snapshot_identity_cannot_reuse_cached_expected_facts(mutation):
    source = {"name": "text", "column_id": 4, "cells": [{"row_id": 1}]}
    snapshot = AdmittedWorkScopeSnapshot({"sources": [source]})
    if mutation == "row_id":
        source["cells"][0]["row_id"] = 2
    elif mutation == "cells":
        source["cells"] = [{"row_id": 1}]
    else:
        source[mutation] = "changed"
    with pytest.raises(RecipeInvocationHalt) as exc:
        _expected_source_cell(
            snapshot, source_index=0, name="text", column_id=4, row_id=1
        )
    assert exc.value.code == "promise_violation"


@pytest.mark.parametrize("replacement", ["changed", "original"])
def test_indexed_admitted_read_still_checks_live_value_and_revision(
    tmp_path, replacement
):
    project = Project.create(tmp_path / "source.frisket")
    try:
        sheet = project.add_sheet("Source")
        column = project.add_column(sheet, "text")
        project.add_rows(sheet, [{"text": "original"}], {"text": column})
        row_id = project.visible_row_ids(sheet)[0]
        recipe = SimpleNamespace(source_columns=lambda _spec: ["text"])
        spec = {"sheet_id": sheet}
        payload = {}
        canonical_work_scope_binding(project, recipe, spec, [row_id], snapshot=payload)
        snapshot = AdmittedWorkScopeSnapshot(payload)
        assert row_values(
            project,
            recipe,
            spec,
            {"text": column},
            row_id,
            for_model=False,
            column_types={"text": "text"},
            admitted_work_scope=snapshot,
        ) == {"text": "original"}
        project.apply_edits(
            [{"row_id": row_id, "column_id": column, "value": replacement}]
        )
        with pytest.raises(RecipeInvocationHalt) as exc:
            row_values(
                project,
                recipe,
                spec,
                {"text": column},
                row_id,
                for_model=False,
                column_types={"text": "text"},
                admitted_work_scope=snapshot,
            )
        assert exc.value.code == "promise_violation"
    finally:
        project.close()
