from __future__ import annotations

from pathlib import Path

import pytest

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.runner.preparation import _validate_managed_resume
from frisket.engine.store.result_generations import (
    GenerationStateError,
    ResultGenerationStore,
)
from frisket.engine.store.runs import RunResultStore
from test_result_generation_store import (
    _declare,
    _release,
    _seed_project,
    _start_claimed_run,
)
from test_sheet_refresh import _build, _refresh


def _mutation_snapshot(project) -> tuple[object, ...]:  # noqa: ANN001
    return tuple(
        int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "ops",
            "runs",
            "run_rows",
            "run_scopes",
            "results",
            "model_calls",
            "receipts",
            "output_column_claims",
        )
    )


def test_managed_resume_only_accepts_exact_open_recovery(tmp_path: Path) -> None:
    project, sheet_id, column_id, row_ids = _seed_project(tmp_path)
    claimed_run = _start_claimed_run(
        project,
        sheet_id=sheet_id,
        output_column_id=column_id,
        row_ids=row_ids,
        label="managed recovery",
    )
    generations = ResultGenerationStore(project)
    _declare(generations, claimed_run, column_id, write_mode="create")
    store = RunResultStore(project)
    fields = [{"name": "generated", "column_type": "text"}]
    bindings = generations.bindings_for_run(claimed_run.run_id)
    try:
        _validate_managed_resume(
            project,
            store,
            run_id=claimed_run.run_id,
            sheet_id=sheet_id,
            fields=fields,
            bindings=bindings,
            explicit_resume_scope=row_ids,
        )
        with pytest.raises(GenerationStateError, match="change its row scope"):
            _validate_managed_resume(
                project,
                store,
                run_id=claimed_run.run_id,
                sheet_id=sheet_id,
                fields=fields,
                bindings=bindings,
                explicit_resume_scope=row_ids[:-1],
            )
        with pytest.raises(GenerationStateError, match="output descriptors"):
            _validate_managed_resume(
                project,
                store,
                run_id=claimed_run.run_id,
                sheet_id=sheet_id,
                fields=[{"name": "generated", "column_type": "number"}],
                bindings=bindings,
                explicit_resume_scope=None,
            )
    finally:
        _release(project, claimed_run)
        project.close()


def test_managed_sheet_refresh_refuses_before_rows_or_receipts_change(
    tmp_path: Path,
) -> None:
    project, _parent_id, _source_column_id, child_id, _parent_row_ids = _build(tmp_path)
    column_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? ORDER BY position LIMIT 1",
            (child_id,),
        ).fetchone()[0]
    )
    row_ids = [
        int(row[0])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (child_id,)
        ).fetchall()
    ]
    claimed_run = _start_claimed_run(
        project,
        sheet_id=child_id,
        output_column_id=column_id,
        row_ids=row_ids,
        label="refresh gate generation",
    )
    _declare(
        ResultGenerationStore(project),
        claimed_run,
        column_id,
        write_mode="create",
    )
    try:
        before = (
            _mutation_snapshot(project),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT id,position,hidden FROM rows WHERE sheet_id=? "
                    "ORDER BY position",
                    (child_id,),
                ).fetchall()
            ),
            project.op_cursor,
        )
        result = run_action_spec(
            project,
            _refresh(sheet_id=child_id, key="managed-refresh@sha256:test"),
            project_id="p-ref",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "refresh_unsupported"
        assert result.errors[0].details == {
            "sheet_id": child_id,
            "column_id": column_id,
            "reason": "generation_managed_rows",
        }
        after = (
            _mutation_snapshot(project),
            tuple(
                tuple(row)
                for row in project.db.execute(
                    "SELECT id,position,hidden FROM rows WHERE sheet_id=? "
                    "ORDER BY position",
                    (child_id,),
                ).fetchall()
            ),
            project.op_cursor,
        )
        assert after == before
    finally:
        _release(project, claimed_run)
        project.close()
