"""Public reader integration coverage for the current-cell projection."""

from __future__ import annotations

import json
from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.querysets import resolve_sheet_filter_rows
from frisket.server.services.sheet_grid import _sheet_data_payload
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
)


def test_current_reads_and_querysets_follow_candidate_beneath_edit_undo_redo(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    _generations, run = _publish_initial_generation(
        project, sheet_id, column_id, row_ids
    )
    try:
        values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids)
        assert values == {
            row_id: f"first:{index}" for index, row_id in enumerate(row_ids)
        }
        assert {ref["kind"] for ref in refs.values()} == {"run_result"}
        assert {ref["run_id"] for ref in refs.values()} == {run.run_id}

        edit_op_id = project.apply_edits(
            [{"row_id": row_ids[0], "column_id": column_id, "value": "manual"}]
        )
        live, live_refs = project.get_values_with_refs(
            sheet_id, column_id, [row_ids[0]]
        )
        candidate, candidate_refs = project.get_values_with_refs(
            sheet_id, column_id, [row_ids[0]], apply_edits=False
        )
        assert live[row_ids[0]] == "manual"
        assert live_refs[row_ids[0]]["kind"] == "manual_edit"
        assert candidate[row_ids[0]] == "first:0"
        assert candidate_refs[row_ids[0]] == {
            "kind": "run_result",
            "op_id": run.op_id,
            "row_id": row_ids[0],
            "column_id": column_id,
            "run_id": run.run_id,
        }

        manual = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=json.dumps({"generated": {"eq": "manual"}}),
        )
        sorted_rows = resolve_sheet_filter_rows(
            project,
            sheet_id,
            sort=json.dumps([{"column": "generated", "dir": "asc"}]),
        )
        assert manual.row_ids == [row_ids[0]]
        assert sorted_rows.row_ids == [*row_ids[1:], row_ids[0]]

        assert project.undo() == edit_op_id
        assert project.get_values(sheet_id, column_id, [row_ids[0]]) == {
            row_ids[0]: "first:0"
        }
        assert project.redo() == edit_op_id
        assert project.get_values(sheet_id, column_id, [row_ids[0]]) == {
            row_ids[0]: "manual"
        }
    finally:
        project.close()


def test_failed_and_withheld_heads_refresh_to_explicit_null_current_values(
    tmp_path: Path,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    generations = ResultGenerationStore(project)
    run = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=row_ids,
        label="open generation for terminal values",
    )
    try:
        results = RunResultStore(project)
        _declare(generations, run, column_id, write_mode="create")
        results.write_results(
            run.run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": f"candidate:{index}",
                    "publication_effect": "publish_value",
                }
                for index, row_id in enumerate(row_ids)
            ],
            project_heads=False,
            **run.authority.kwargs(),
        )
        # Unsealed facts have no current head: source cells retain their
        # original visibility and the output remains missing until seal.
        source_column_id = next(
            int(column["id"])
            for column in project.columns(sheet_id)
            if column["name"] == "source"
        )
        source_values, source_refs = project.get_values_with_refs(
            sheet_id, source_column_id, row_ids[:2]
        )
        assert source_values == {row_ids[0]: "alpha", row_ids[1]: "beta"}
        assert [source_refs[row_id]["kind"] for row_id in row_ids[:2]] == [
            "source_cell",
            "source_cell",
        ]
        staged_values, staged_refs = project.get_values_with_refs(
            sheet_id, column_id, row_ids[:2]
        )
        assert staged_values == {row_ids[0]: None, row_ids[1]: None}
        assert [staged_refs[row_id]["kind"] for row_id in row_ids[:2]] == [
            "missing",
            "missing",
        ]
        results.fail_result_value(run.run_id, row_ids[0], column_id, "model failed")
        results.withhold_result_value(run.run_id, row_ids[1], column_id, "no citation")

        _seal(generations, run, column_id)

        values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids[:2])
        assert values == {row_ids[0]: None, row_ids[1]: None}
        assert [refs[row_id]["kind"] for row_id in row_ids[:2]] == [
            "run_result",
            "run_result",
        ]
        assert [refs[row_id]["run_id"] for row_id in row_ids[:2]] == [
            run.run_id,
            run.run_id,
        ]
        failed = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=json.dumps({"generated": {"failed": "any"}}),
        )
        assert failed.row_ids == [row_ids[0]]
    finally:
        _release(project, run)
        project.close()


def test_current_cell_grid_payload_handles_a_page_larger_than_sqlite_bind_chunk(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "large-page.frisket")
    try:
        sheet_id = project.add_sheet("Rows")
        column_id = project.add_column(sheet_id, "number", type="integer")
        row_ids = project.add_rows(
            sheet_id,
            [{"number": index} for index in range(901)],
            {"number": column_id},
        )
        payload = _sheet_data_payload(
            project,
            sheet_id,
            project.columns(sheet_id),
            row_ids,
            total=len(row_ids),
        )
        assert payload["total"] == 901
        assert len(payload["rows"]) == 901
        assert payload["rows"][0]["cells"][str(column_id)] == 0
        assert payload["rows"][-1]["cells"][str(column_id)] == 900
        assert payload["rows"][-1]["meta"][str(column_id)]["current_value_ref"] == {
            "kind": "source_cell",
            "op_id": None,
            "row_id": row_ids[-1],
            "column_id": column_id,
            "run_id": None,
        }
    finally:
        project.close()
