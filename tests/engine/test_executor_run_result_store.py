from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.engine.store import Project
from frisket.engine.store.result_generations import GenerationSealedError
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results


def _seed_project(
    tmp_path: Path,
) -> tuple[Project, int, dict[str, int], list[int], int]:
    project = Project.create(tmp_path / "runs.frisket", name="Runs")
    sheet_id = project.add_sheet("Rows")
    columns = {
        "text": project.add_column(sheet_id, "text", type="text"),
        "score": project.add_column(
            sheet_id, "score", type="number", ai_generated=True
        ),
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
    op_id = project.append_op("map", {"recipe": "score"}, label="score rows")
    return project, sheet_id, columns, row_ids, op_id


def test_run_result_store_preserves_scopes_results_model_calls_and_pointers(
    tmp_path: Path,
) -> None:
    project, sheet_id, columns, row_ids, op_id = _seed_project(tmp_path)
    store = RunResultStore(project)

    run_id = store.start_run(
        op_id,
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

    store.finish_run(run_id)
    store.point_column_at_run(op_id, columns["score"], run_id)
    assert project.get_column(columns["score"])["current_run_id"] == run_id
    project.undo()
    assert project.get_column(columns["score"])["current_run_id"] is None
