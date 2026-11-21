from __future__ import annotations

import json

import pytest
from helpers import write_claimed_test_results

from frisket.engine.store import Project
from frisket.engine.store.cell_projection import repair_current_cells
from frisket.engine.store.current_cells import refresh_current_cells_from_key_table
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore


def _production_shaped_project(
    project: Project, row_count: int
) -> tuple[Project, int, list[int]]:
    """A generated column whose every row has a source cell under a run result."""
    sheet = project.add_sheet("Rows")
    column = project.add_column(sheet, "generated", type="integer", ai_generated=True)
    row_ids = project.add_rows(
        sheet,
        [{"generated": -position} for position in range(1, row_count + 1)],
        {"generated": column},
    )
    run = RunResultStore(project).start_run(
        project.append_change("map.test"),
        sheet,
        "map.test",
        row_ids=row_ids,
        total_rows=row_count,
    )
    write_claimed_test_results(
        project,
        run,
        [
            {"row_id": row_id, "column_id": column, "value": position}
            for position, row_id in enumerate(row_ids, 1)
        ],
    )
    while repair_current_cells(project):
        pass
    project.db.execute("ANALYZE")
    return project, column, row_ids


def _sparse_key_table(db, key_table: str, column: int, target_rows: list[int]) -> None:
    db.execute(
        f"CREATE TEMP TABLE {key_table} ("
        "row_id INTEGER NOT NULL,column_id INTEGER NOT NULL,"
        "PRIMARY KEY(row_id,column_id)) WITHOUT ROWID"
    )
    db.executemany(
        f"INSERT INTO {key_table} (row_id,column_id) VALUES (?,?)",
        ((row_id, column) for row_id in target_rows),
    )
    db.commit()


def test_exact_key_refresh_work_is_bounded_by_sparse_target(
    project: Project,
) -> None:
    row_count = 25_000
    project, column, row_ids = _production_shaped_project(project, row_count)
    db = project.db
    key_table = "temp_result_keys_1234abcd"
    target_rows = row_ids[-301:]
    _sparse_key_table(db, key_table, column, target_rows)
    db.execute("BEGIN")

    progress_calls = 0

    def count_vm_work() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return 0

    db.set_progress_handler(count_vm_work, 1_000)
    try:
        assert refresh_current_cells_from_key_table(db, key_table) == len(target_rows)
    finally:
        db.set_progress_handler(None, 0)

    assert progress_calls < 500
    refreshed = db.execute(
        "SELECT COUNT(*) AS cell_count,MIN(origin_kind) AS min_origin,"
        "MAX(origin_kind) AS max_origin FROM current_cells "
        f"WHERE (column_id,row_id) IN (SELECT column_id,row_id FROM {key_table})"
    ).fetchone()
    assert tuple(refreshed) == (len(target_rows), "run_result", "run_result")
    assert db.execute("SELECT COUNT(*) FROM current_cells").fetchone()[0] == row_count
    db.rollback()


def _sparse_head_rebuild_vm_work(project: Project, row_count: int) -> int:
    project, column, row_ids = _production_shaped_project(project, row_count)
    db = project.db
    key_table = "temp_result_keys_5678abcd"
    target_rows = row_ids[-301:]
    _sparse_key_table(db, key_table, column, target_rows)
    db.execute("BEGIN")

    progress_calls = 0

    def count_vm_work() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return 0

    db.set_progress_handler(count_vm_work, 1_000)
    try:
        store = ResultGenerationStore(project)
        assert store.rebuild_heads_from_key_table(key_table, commit=False) == len(
            target_rows
        )
    finally:
        db.set_progress_handler(None, 0)

    assert {
        int(row["row_id"])
        for row in db.execute(
            "SELECT row_id FROM cell_result_heads WHERE column_id=? "
            f"AND row_id IN (SELECT row_id FROM {key_table})",
            (column,),
        )
    } == set(target_rows)
    db.rollback()
    return progress_calls


def test_sparse_head_rebuild_work_does_not_scale_with_unrelated_history(
    make_project,
) -> None:
    small_work = _sparse_head_rebuild_vm_work(make_project("small"), 2_500)
    large_work = _sparse_head_rebuild_vm_work(make_project("large"), 25_000)

    assert large_work <= small_work * 2 + 50


def test_plain_source_and_output_edit_undo_redo_skip_review_recount(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project("Edits")
    sheet_id = project.add_sheet("Rows")
    source_id = project.add_column(sheet_id, "source")
    output_id = project.add_column(sheet_id, "output", type="text", ai_generated=True)
    project.add_rows(sheet_id, [{"source": "before"}], {"source": source_id})
    row_id = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
    )
    edit_op_id = project.apply_edits(
        [
            {"row_id": row_id, "column_id": source_id, "value": "after"},
            {"row_id": row_id, "column_id": output_id, "value": "manual"},
        ]
    )
    refresh_calls = 0

    def count_refresh(_project: Project) -> int:
        nonlocal refresh_calls
        refresh_calls += 1
        return 0

    monkeypatch.setattr(Project, "refresh_pending_review_summary", count_refresh)

    assert project.undo() == edit_op_id
    assert project.get_values(sheet_id, source_id) == {row_id: "before"}
    assert project.get_values(sheet_id, output_id) == {row_id: None}
    assert project.redo() == edit_op_id
    assert project.get_values(sheet_id, source_id) == {row_id: "after"}
    assert project.get_values(sheet_id, output_id) == {row_id: "manual"}
    assert refresh_calls == 0


@pytest.mark.parametrize(
    "transition_metadata",
    [
        {"review_states": {}},
        {"created_rows": []},
        {"column_pointers": {}},
    ],
    ids=["review-state", "visibility", "result-identity"],
)
def test_unknown_edit_transition_metadata_keeps_review_recount(
    make_project,
    monkeypatch: pytest.MonkeyPatch,
    transition_metadata: dict[str, object],
) -> None:
    project = make_project("Guarded edit")
    sheet_id = project.add_sheet("Rows")
    column_id = project.add_column(sheet_id, "source")
    project.add_rows(sheet_id, [{"source": "before"}], {"source": column_id})
    row_id = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
    )
    edit_op_id = project.apply_edits(
        [{"row_id": row_id, "column_id": column_id, "value": "after"}]
    )
    # No edit writes these keys; unrecognised transition metadata must keep
    # the conservative review recount.
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        project.db.execute(
            "UPDATE ops SET undo_info=? WHERE id=?",
            (json.dumps(transition_metadata), edit_op_id),
        )
    refresh_calls = 0

    def count_refresh(_project: Project) -> int:
        nonlocal refresh_calls
        refresh_calls += 1
        return 0

    monkeypatch.setattr(Project, "refresh_pending_review_summary", count_refresh)

    assert project.undo() == edit_op_id
    assert project.redo() == edit_op_id
    assert refresh_calls == 2


def test_edit_operation_with_generation_metadata_keeps_review_recount(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project("Generation")
    sheet_id = project.add_sheet("Rows")
    column_id = project.add_column(sheet_id, "source")
    project.add_rows(sheet_id, [{"source": "before"}], {"source": column_id})
    row_id = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=?", (sheet_id,)
        ).fetchone()[0]
    )
    edit_op_id = project.apply_edits(
        [{"row_id": row_id, "column_id": column_id, "value": "after"}]
    )
    # Edits never own a run; a generation attached to the edit operation is
    # the unexpected metadata that must keep the review recount.
    with project.db:
        project.db.execute("BEGIN IMMEDIATE")
        run_id = int(
            project.db.execute(
                "INSERT INTO runs (op_id,sheet_id,action_kind) VALUES (?,?,?)",
                (edit_op_id, sheet_id, "map.test"),
            ).lastrowid
        )
        project.db.execute(
            "INSERT INTO run_output_generations "
            "(run_id,column_id,output_role,compatibility_key,write_mode,state,claim_token,output_descriptor_json) "
            "VALUES (?,?,'value','text-v1','create','active','claim-test',?)",
            (
                run_id,
                column_id,
                json.dumps(
                    {
                        "column_type": "text",
                        "schema": {},
                        "format": None,
                        "semantic_type": None,
                    }
                ),
            ),
        )
    refresh_calls = 0

    def count_refresh(_project: Project) -> int:
        nonlocal refresh_calls
        refresh_calls += 1
        return 0

    monkeypatch.setattr(Project, "refresh_pending_review_summary", count_refresh)

    assert project.undo() == edit_op_id
    assert project.redo() == edit_op_id
    assert refresh_calls == 2
