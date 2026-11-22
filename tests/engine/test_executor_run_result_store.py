from __future__ import annotations

import json

import pytest

from frisket.engine.store import Project
from frisket.engine.store.changes import ChangeStore
from frisket.engine.store.result_generations import GenerationSealedError
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import StaleAttemptWriter
from helpers import run_writer_authority_fixture, write_claimed_test_results
from frisket.engine.store.transactions import write_transaction


def _seed_project(
    make_project,
) -> tuple[Project, int, dict[str, int], list[int], int]:
    project = make_project("Runs")
    sheet_id = project.add_sheet("Rows")
    columns = {
        "text": project.add_column(sheet_id, "text", type="text"),
        "score": project.add_column(sheet_id, "score", type="number", generated=True),
    }
    project.add_rows(
        sheet_id,
        [{"text": "alpha"}, {"text": "beta"}, {"text": "gamma"}],
        {"text": columns["text"]},
    )
    row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        )
    ]
    change_id = project.append_change("map", {"recipe": "score"}, label="score rows")
    return project, sheet_id, columns, row_ids, change_id


def test_run_result_store_preserves_scopes_results_model_calls_and_pointers(
    make_project,
) -> None:
    project, sheet_id, columns, row_ids, change_id = _seed_project(make_project)
    store = RunResultStore(project)

    run_id = store.start_run(
        change_id,
        sheet_id,
        "test.score",
        params={"threshold": 7},
        total_rows=len(row_ids),
        row_ids=row_ids,
    )

    assert store.run_row_scope(run_id) == row_ids
    assert store.run_row_scope_count(run_id) == len(row_ids)
    assert store.run_row_scope_if_present(run_id) == row_ids
    assert store.has_run_row_scope(run_id)
    assert store.pending_run_row_scope_count(run_id, [columns["score"]]) == 3
    assert store.pending_run_row_scope_page_after(
        run_id, [columns["score"]], after_position=0, limit=2
    ) == [(1, row_ids[1]), (2, row_ids[2])]

    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_ids[0],
                "column_id": columns["score"],
                "value": 4,
                "cost": 0.01,
                "model_calls": [
                    {
                        "id": "call-score-1",
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "classify",
                        "engine": "fixture",
                        "provider": "fixture",
                        "provider_kind": "test",
                        "model_ids": ["fixture-model"],
                        "credential_source": "platform_key",
                        "provider_cost_usd": 0.01,
                    }
                ],
            },
            {
                "row_id": row_ids[1],
                "column_id": columns["score"],
                "value": None,
                "error": "boom",
                "cost": 0.02,
                "model_calls": [
                    {
                        "id": "call-score-2",
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "classify",
                        "engine": "fixture",
                        "provider": "fixture",
                        "provider_kind": "test",
                        "model_ids": ["fixture-model"],
                        "credential_source": "platform_key",
                        "provider_cost_usd": 0.02,
                    }
                ],
            },
        ],
    )

    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["completed_rows"] == 2
    assert run["failed_rows"] == 1
    assert run["cost_actual"] == pytest.approx(0.03)
    assert store.pending_run_row_scope_count(run_id, [columns["score"]]) == 1

    # A sealed result cannot be rewritten in place. Recovery uses a fresh
    # explicitly scoped generation and leaves this run's accounting intact.
    with pytest.raises(GenerationSealedError):
        write_claimed_test_results(
            project,
            run_id,
            [
                {
                    "row_id": row_ids[1],
                    "column_id": columns["score"],
                    "value": 8,
                    "cost": 9,
                }
            ],
        )
    run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["completed_rows"] == 2
    assert run["failed_rows"] == 1
    assert run["cost_actual"] == pytest.approx(0.03)

    calls = store.model_calls(run_id)
    assert sorted(call["id"] for call in calls) == ["call-score-1", "call-score-2"]
    assert json.loads(calls[0]["model_ids"]) == ["fixture-model"]

    authority = run_writer_authority_fixture(
        project, run_id, output_column_ids={columns["score"]}
    )
    with pytest.raises(StaleAttemptWriter):
        store.finish_run(run_id)
    assert store.get_run(run_id)["status"] == "running"
    store.finish_run(run_id, fence=authority.fence())
    with write_transaction(store.db):
        store.point_column_at_run(change_id, columns["score"], run_id)
    assert project.get_column(columns["score"])["current_run_id"] == run_id
    project.undo()
    assert project.get_column(columns["score"])["current_run_id"] is None


def test_pre_dispatch_undo_cannot_be_finished_as_completed(make_project) -> None:
    project, sheet_id, _columns, row_ids, change_id = _seed_project(make_project)
    store = RunResultStore(project)
    run_id = store.start_run(
        change_id,
        sheet_id,
        "test.score",
        params={},
        total_rows=len(row_ids),
        row_ids=row_ids,
    )
    project.db.execute("BEGIN IMMEDIATE")
    ChangeStore(project).start(change_id, writing=True)
    project.db.commit()
    assert project.undo() == change_id
    # Cancellation targets this ordered execution, leaving the earlier
    # source-row import visible rather than falling through to undo it.
    assert project.visible_row_ids(sheet_id) == row_ids
    run = store.get_run(run_id)
    assert run["cancel_requested_at"] is not None
    with pytest.raises(StaleAttemptWriter, match="cancellation intent"):
        store.finish_run(run_id, "completed")
    assert store.get_run(run_id)["status"] == "running"
    store.finish_run(run_id, "cancelled")
    assert store.get_run(run_id)["status"] == "cancelled"
