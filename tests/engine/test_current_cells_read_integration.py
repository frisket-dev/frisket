"""Public reader integration coverage for the current-cell projection."""

from __future__ import annotations

import json

from frisket.engine.store.transactions import write_transaction
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore
from frisket.querysets import resolve_sheet_filter_rows
from frisket.server.services.sheet_grid import (
    _column_stats_payload,
    _sheet_data_payload,
)
from test_result_generation_store import (
    _declare,
    _publish_initial_generation,
    _release,
    _seal,
    _seed_project,
    _start_claimed_run,
)


def test_current_reads_and_querysets_follow_candidate_beneath_edit_undo_redo(
    make_project,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(make_project)
    _generations, run = _publish_initial_generation(
        project, sheet_id, column_id, row_ids
    )
    values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids)
    assert values == {row_id: f"first:{index}" for index, row_id in enumerate(row_ids)}
    assert {ref["kind"] for ref in refs.values()} == {"run_result"}
    assert {ref["run_id"] for ref in refs.values()} == {run.run_id}

    edit_op_id = project.apply_edits(
        [{"row_id": row_ids[0], "column_id": column_id, "value": "manual"}]
    )
    live, live_refs = project.get_values_with_refs(sheet_id, column_id, [row_ids[0]])
    from frisket.engine.store.runs import RunResultStore

    saved = RunResultStore(project).decoded_result_rows(
        [(run.run_id, row_ids[0], column_id)]
    )
    assert live[row_ids[0]] == "manual"
    assert live_refs[row_ids[0]]["kind"] == "manual_edit"
    assert saved[(run.run_id, row_ids[0], column_id)]["value"] == "first:0"

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


def test_failed_and_withheld_heads_refresh_to_explicit_null_current_values(
    make_project,
) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(make_project)
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
        # Fixed output scope has an owner immediately, even before a
        # checkpoint is published. Its pending values remain unavailable.
        assert [staged_refs[row_id]["kind"] for row_id in row_ids[:2]] == [
            "run_result",
            "run_result",
        ]
        with write_transaction(results.db):
            results.fail_result_value(run.run_id, row_ids[0], column_id, "model failed")
        with write_transaction(project.db):
            results.withhold_result_value(
                run.run_id, row_ids[1], column_id, "no citation"
            )

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


def test_current_cell_grid_payload_handles_a_page_larger_than_sqlite_bind_chunk(
    make_project,
) -> None:
    project = make_project("large-page")
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
        "producer_id": 1,
        "outcome": "completed",
    }


def test_invalid_typed_cells_are_preserved_visible_and_repairable(
    make_project,
) -> None:
    project = make_project("invalid-cells")
    sheet_id = project.add_sheet("Rows")
    column_id = project.add_column(sheet_id, "amount", type="number")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"amount": 42},
            {"amount": "N/A"},
            {"amount": None},
            {"amount": True},
        ],
        {"amount": column_id},
    )

    assert project.get_values(sheet_id, column_id) == {
        row_ids[0]: 42,
        row_ids[1]: None,
        row_ids[2]: None,
        row_ids[3]: None,
    }
    assert project.get_values(sheet_id, column_id, preserve_invalid=True) == {
        row_ids[0]: 42,
        row_ids[1]: "N/A",
        row_ids[2]: None,
        row_ids[3]: True,
    }
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT row_id,validity FROM current_cells "
            "WHERE column_id=? ORDER BY row_id",
            (column_id,),
        )
    ] == [
        (row_ids[0], "valid"),
        (row_ids[1], "invalid"),
        (row_ids[3], "invalid"),
    ]

    payload = _sheet_data_payload(
        project,
        sheet_id,
        project.columns(sheet_id),
        row_ids,
        total=len(row_ids),
    )
    invalid_cell = payload["rows"][1]
    assert invalid_cell["cells"][str(column_id)] == "N/A"
    assert invalid_cell["meta"][str(column_id)]["invalid"] is True

    filtered = resolve_sheet_filter_rows(
        project,
        sheet_id,
        filter_=json.dumps({"amount": {"gte": "1"}}),
    )
    assert filtered.row_ids == [row_ids[0]]
    stats = _column_stats_payload(project, sheet_id, column_id, force=False)
    assert stats["present"] == 1
    assert stats["missing"] == 1
    assert stats["invalid"] == 2

    edit = project.apply_edits(
        [{"row_id": row_ids[1], "column_id": column_id, "value": 7}]
    )
    assert project.get_values(sheet_id, column_id)[row_ids[1]] == 7
    assert project.undo() == edit
    assert project.get_values(sheet_id, column_id)[row_ids[1]] is None
    assert (
        project.get_values(sheet_id, column_id, preserve_invalid=True)[row_ids[1]]
        == "N/A"
    )


def test_retyping_reclassifies_without_rewriting_source_values(make_project) -> None:
    project = make_project("retype-validity")
    sheet_id = project.add_sheet("Rows")
    column_id = project.add_column(sheet_id, "value", type="json")
    project.add_rows(sheet_id, [{"value": 3}, {"value": "three"}], {"value": column_id})

    with write_transaction(project.db):
        project.set_column_type(column_id, "text")
    assert project.get_values(sheet_id, column_id) == {1: None, 2: "three"}
    assert project.get_values(sheet_id, column_id, preserve_invalid=True) == {
        1: 3,
        2: "three",
    }

    with write_transaction(project.db):
        project.set_column_type(column_id, "number")
    assert project.get_values(sheet_id, column_id) == {1: 3, 2: None}
    assert project.get_values(sheet_id, column_id, preserve_invalid=True) == {
        1: 3,
        2: "three",
    }
