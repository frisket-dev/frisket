from __future__ import annotations
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY

import asyncio
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.runner import MapRunner
from frisket.engine.runner.map_runner import ResultEvidenceWriteFailed
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.ops.base import ActionContext, ActionProgram
from runner_test_helpers import run_with_output_claim


class _EvidenceBatchProgram(ActionProgram):
    consumes_resolution = False
    cost_class = "free"

    def __init__(self, *, fail_evidence: bool = False) -> None:
        super().__init__(
            name="test.batch_result_publication",
            llm=False,
            description="test-only batch result publication recipe",
        )
        self.fail_evidence = fail_evidence
        self.evidence_batches: list[list[int]] = []
        self.executed_batches: list[list[int]] = []

    def source_columns(self, spec: dict[str, Any]) -> list[str]:
        del spec
        return ["source"]

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [{"name": "output", "column_type": "text"}]

    async def execute_batch(
        self,
        values_by_row: dict[int, dict[str, Any]],
        spec: dict[str, Any],
        ctx: ActionContext,
    ) -> dict[int, dict[str, Any]]:
        del spec, ctx
        self.executed_batches.append(list(values_by_row))
        return {
            row_id: {"output": f"published:{values['source']}"}
            for row_id, values in values_by_row.items()
        }

    def write_result_evidence(
        self,
        project: Project,
        spec: dict[str, Any],
        *,
        batch: list[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        del spec, kwargs
        row_ids = sorted({int(cell["row_id"]) for cell in batch})
        self.evidence_batches.append(row_ids)
        project.db.executemany(
            "INSERT INTO batch_evidence_probe (row_id) VALUES (?)",
            [(row_id,) for row_id in row_ids],
        )
        if self.fail_evidence:
            raise RuntimeError("injected batch evidence failure")


def _project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "batch-result-publication.frisket")
    project.db.execute("CREATE TABLE batch_evidence_probe (row_id INTEGER NOT NULL)")
    sheet_id = project.add_sheet("Rows")
    source_id = project.add_column(sheet_id, "source", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"source": "alpha"}, {"source": "beta"}],
        {"source": source_id},
    )
    return project, sheet_id, row_ids


def _runner(project: Project) -> MapRunner:
    return MapRunner(
        project,
        ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, keys={}),
        authority=UnroutedOnlyAuthority(project),
    )


def _spec(sheet_id: int) -> dict[str, Any]:
    return {"action_kind": "test.batch_result_publication", "sheet_id": sheet_id}


def test_batch_results_publish_through_recipe_evidence_callback(tmp_path: Path) -> None:
    project, sheet_id, row_ids = _project(tmp_path)
    program = _EvidenceBatchProgram()
    try:
        progress = asyncio.run(
            run_with_output_claim(
                _runner(project),
                _spec(sheet_id),
                program=program,
                confirmed=True,
            )
        )

        assert program.evidence_batches == [row_ids]
        assert [
            int(row["row_id"])
            for row in project.db.execute(
                "SELECT row_id FROM batch_evidence_probe ORDER BY row_id"
            )
        ] == row_ids
        output_id = int(
            project.db.execute(
                "SELECT id FROM columns WHERE sheet_id=? AND name='output'",
                (sheet_id,),
            ).fetchone()[0]
        )
        assert project.get_values(sheet_id, output_id) == {
            row_ids[0]: "published:alpha",
            row_ids[1]: "published:beta",
        }
        assert project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (progress.run_id,)
        ).fetchone()[0] == len(row_ids)
        from frisket.engine.store.consumed_sources import row_input_state

        bundles = project.db.execute(
            "SELECT kind FROM consumed_source_bundles WHERE run_id=?",
            (progress.run_id,),
        ).fetchall()
        assert [bundle["kind"] for bundle in bundles] == ["aggregate"]
        source_id = next(
            column["id"]
            for column in project.columns(sheet_id)
            if column["name"] == "source"
        )
        project.apply_edits(
            [
                {
                    "row_id": row_ids[-1],
                    "column_id": source_id,
                    "value": "changed neighbor",
                }
            ]
        )
        assert row_input_state(project, progress.run_id, row_ids[0]) == "stale"
        project.undo()
        assert row_input_state(project, progress.run_id, row_ids[0]) == "fresh"
    finally:
        project.close()


def test_batch_evidence_failure_rolls_back_result_batch(tmp_path: Path) -> None:
    project, sheet_id, row_ids = _project(tmp_path)
    program = _EvidenceBatchProgram(fail_evidence=True)
    try:
        with pytest.raises(ResultEvidenceWriteFailed):
            asyncio.run(
                run_with_output_claim(
                    _runner(project),
                    _spec(sheet_id),
                    program=program,
                    confirmed=True,
                )
            )

        assert program.evidence_batches == [row_ids]
        assert (
            project.db.execute("SELECT COUNT(*) FROM batch_evidence_probe").fetchone()[
                0
            ]
            == 0
        )
        run = project.db.execute(
            "SELECT id,completed_rows,failed_rows FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert run is not None
        assert (run["completed_rows"], run["failed_rows"]) == (0, 0)
        assert (
            project.db.execute(
                "SELECT count(*) FROM consumed_source_bundles"
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM cell_result_heads WHERE run_id=?", (run["id"],)
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_batch_preview_and_publication_never_dispatch_unavailable_required_rows(
    tmp_path,
):
    project, sheet, rows = _project(tmp_path)
    program = _EvidenceBatchProgram()
    source = next(
        column["id"] for column in project.columns(sheet) if column["name"] == "source"
    )
    project.set_column_type(source, "integer")
    calls = []

    async def never_dispatch(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unavailable inputs reached the batch handler")

    program.execute_batch = never_dispatch
    try:
        preview = asyncio.run(
            _runner(project).preview({**_spec(sheet), "row_ids": rows}, program=program)
        )
        assert all(
            preview.values[row]["output"]["outcome"] == "not_completed" for row in rows
        )
        progress = asyncio.run(
            run_with_output_claim(
                _runner(project), _spec(sheet), program=program, confirmed=True
            )
        )
        assert calls == []
        saved = project.db.execute(
            "SELECT outcome,error_code FROM results WHERE run_id=? ORDER BY row_id",
            (progress.run_id,),
        ).fetchall()
        assert [tuple(row) for row in saved] == [
            ("not_completed", "input_invalid")
        ] * len(rows)
    finally:
        project.close()


@pytest.mark.parametrize("invalid_row", [False, True])
def test_empty_batch_executes_but_unavailable_batch_does_not(make_project, invalid_row):
    project = make_project("empty-batch")
    project.db.execute("CREATE TABLE batch_evidence_probe (row_id INTEGER NOT NULL)")
    sheet = project.add_sheet("Rows")
    source = project.add_column(sheet, "source", type="integer")
    if invalid_row:
        project.add_rows(sheet, [{"source": "not an integer"}], {"source": source})
    program = _EvidenceBatchProgram()
    program.allow_all_empty_input = True
    try:
        progress = asyncio.run(
            run_with_output_claim(
                _runner(project), _spec(sheet), program=program, confirmed=True
            )
        )
        assert program.executed_batches == ([] if invalid_row else [[]])
        outcomes = project.db.execute(
            "SELECT outcome,error_code FROM results WHERE run_id=?",
            (progress.run_id,),
        ).fetchall()
        assert [tuple(row) for row in outcomes] == (
            [("not_completed", "input_invalid")] if invalid_row else []
        )
    finally:
        project.close()
