from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from frisket.ai.llm import ModelRouter
from frisket.engine.runner import MapRunner
from frisket.server.run_payloads import action_run_rows_payload
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.engine.store.result_generations import GenerationSealedError
from frisket.ops.base import Recipe
from helpers import run_writer_authority_fixture, write_claimed_test_results
from runner_test_helpers import run_with_output_claim


ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _EmptyScopeRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"
    name: str = "test.empty_scope"
    llm: bool = False

    def source_columns(self, spec: dict[str, Any]) -> list[str]:
        del spec
        return ["text"]

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [{"name": "late_match", "column_type": "text"}]


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} must contain a YAML mapping"
    return loaded


def _seed_project(tmp_path: Path) -> tuple[Project, int, int, int, list[int]]:
    project = Project.create(tmp_path / "long-lived.frisket")
    sheet_id = project.add_sheet("events")
    text_col = project.add_column(sheet_id, "text")
    row_ids = project.add_rows(
        sheet_id,
        [{"text": "alpha"}, {"text": "beta"}, {"text": "gamma"}],
        {"text": text_col},
    )
    output_col = project.add_column(sheet_id, "topic", ai_generated=True)
    return project, sheet_id, text_col, output_col, row_ids


def _seed_completed_full_sheet_run(
    project: Project,
    *,
    sheet_id: int,
    output_col: int,
    row_ids: list[int],
) -> int:
    op_id = project.append_op("map", {"action_kind": "map.classify"}, label="classify")
    run_id = RunResultStore(project).start_run(
        op_id,
        sheet_id,
        "map.classify",
        params={
            "action_kind": "map.classify",
            "sheet_id": sheet_id,
            "input_columns": ["text"],
            "fields": [{"name": "topic", "type": "text"}],
        },
        total_rows=len(row_ids),
        row_ids=row_ids,
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_col,
                "value": f"topic-{index}",
                "tokens_in": 1,
                "tokens_out": 1,
                "cost": 0.001,
            }
            for index, row_id in enumerate(row_ids)
        ],
    )
    RunResultStore(project).finish_run(run_id)
    RunResultStore(project).point_column_at_run(op_id, output_col, run_id)
    return run_id


def test_run_rows_uses_immutable_run_scope_after_sheet_history_changes(
    tmp_path: Path,
) -> None:
    project, sheet_id, text_col, output_col, original_row_ids = _seed_project(tmp_path)
    try:
        run_id = _seed_completed_full_sheet_run(
            project,
            sheet_id=sheet_id,
            output_col=output_col,
            row_ids=original_row_ids,
        )

        project.add_rows(
            sheet_id,
            [{"text": "delta"}, {"text": "epsilon"}],
            {"text": text_col},
        )
        project.db.execute(
            "UPDATE rows SET hidden=1 WHERE id=?", (original_row_ids[1],)
        )
        project.db.commit()

        payload = action_run_rows_payload(project, run_id, offset=0, limit=10)

        assert payload["run"]["total_rows"] == 3
        assert payload["total"] == 3
        assert [row["row_id"] for row in payload["rows"]] == original_row_ids
    finally:
        project.close()


def test_result_writes_do_not_overcount_duplicate_row_upserts(
    tmp_path: Path,
) -> None:
    project, sheet_id, _text_col, output_col, row_ids = _seed_project(tmp_path)
    try:
        op_id = project.append_op(
            "map", {"action_kind": "map.classify"}, label="classify"
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            total_rows=1,
            row_ids=[row_ids[0]],
        )
        batch = [
            {
                "row_id": row_ids[0],
                "column_id": output_col,
                "value": "first",
                "tokens_in": 3,
                "tokens_out": 1,
                "cost": 0.01,
                # runs.cost_actual is projected from the durable facts; the
                # stable call id is what keeps the duplicate upsert free.
                "model_calls": [
                    {
                        "id": "call-upsert-1",
                        "fact_version": "frisket.model-call-fact.v1",
                        "capability": "classify",
                        "engine": "fixture",
                        "provider": "fixture",
                        "provider_kind": "test",
                        "credential_source": "platform_key",
                        "provider_cost_usd": 0.01,
                    }
                ],
            }
        ]

        authority = run_writer_authority_fixture(
            project,
            run_id,
            output_column_ids={output_col},
        )
        RunResultStore(project).write_results(
            run_id,
            batch,
            **authority.kwargs(),
        )
        RunResultStore(project).write_results(
            run_id,
            batch,
            **authority.kwargs(),
        )

        run = project.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        result_count = project.db.execute(
            "SELECT COUNT(*) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        assert result_count == 1
        assert run["completed_rows"] == 1
        assert run["failed_rows"] == 0
        assert run["cost_actual"] == 0.01
    finally:
        project.close()


def test_result_writes_update_failed_state_by_batch_delta(tmp_path: Path) -> None:
    project, sheet_id, _text_col, output_col, row_ids = _seed_project(tmp_path)
    try:
        op_id = project.append_op(
            "map", {"action_kind": "map.classify"}, label="classify"
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            total_rows=1,
            row_ids=[row_ids[0]],
        )

        write_claimed_test_results(
            project,
            run_id,
            [
                {
                    "row_id": row_ids[0],
                    "column_id": output_col,
                    "error": "temporary failure",
                }
            ],
        )
        failed = project.db.execute(
            "SELECT completed_rows, failed_rows FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        assert failed["completed_rows"] == 1
        assert failed["failed_rows"] == 1

        with pytest.raises(GenerationSealedError):
            write_claimed_test_results(
                project,
                run_id,
                [
                    {
                        "row_id": row_ids[0],
                        "column_id": output_col,
                        "value": "recovered",
                        "error": None,
                    }
                ],
            )

        recovered = project.db.execute(
            "SELECT completed_rows, failed_rows FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        assert recovered["completed_rows"] == 1
        assert recovered["failed_rows"] == 1
    finally:
        project.close()


def test_run_scope_records_large_scope_in_chunks(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "large-scope.frisket")
    try:
        sheet_id = project.add_sheet("events")
        text_col = project.add_column(sheet_id, "text")
        row_ids = project.add_rows(
            sheet_id,
            [{"text": f"row-{index}"} for index in range(1005)],
            {"text": text_col},
        )
        op_id = project.append_op("map", {"action_kind": "map.classify"}, label="large")

        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            total_rows=len(row_ids),
            row_ids=row_ids,
        )

        assert RunResultStore(project).run_row_scope(run_id) == row_ids
        row_count = project.db.execute(
            "SELECT row_count FROM run_scopes WHERE run_id=?", (run_id,)
        ).fetchone()
        assert row_count["row_count"] == len(row_ids)
    finally:
        project.close()


def test_run_scope_write_failure_rolls_back_run_and_marker(tmp_path: Path) -> None:
    project, sheet_id, _text_col, _output_col, _row_ids = _seed_project(tmp_path)
    try:
        op_id = project.append_op(
            "map", {"action_kind": "map.classify"}, label="bad scope"
        )

        with pytest.raises(sqlite3.IntegrityError):
            RunResultStore(project).start_run(
                op_id,
                sheet_id,
                "map.classify",
                total_rows=1,
                row_ids=[999_999],
            )

        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM runs WHERE op_id=?", (op_id,)
            ).fetchone()[0]
            == 0
        )
        assert project.db.execute("SELECT COUNT(*) FROM run_scopes").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM run_rows").fetchone()[0] == 0
    finally:
        project.close()


def test_empty_run_scope_stays_empty_after_rows_are_added(tmp_path: Path) -> None:
    project, sheet_id, text_col, _output_col, _row_ids = _seed_project(tmp_path)
    try:
        op_id = project.append_op(
            "map", {"action_kind": "map.classify"}, label="empty run"
        )
        run_id = RunResultStore(project).start_run(
            op_id,
            sheet_id,
            "map.classify",
            params={"action_kind": "map.classify", "sheet_id": sheet_id},
            total_rows=0,
            row_ids=[],
        )

        project.add_rows(sheet_id, [{"text": "late"}], {"text": text_col})

        payload = action_run_rows_payload(project, run_id, offset=0, limit=10)
        assert payload["run"]["total_rows"] == 0
        assert payload["total"] == 0
        assert payload["rows"] == []
    finally:
        project.close()


def test_resuming_empty_run_scope_does_not_pick_up_late_rows(tmp_path: Path) -> None:
    project, sheet_id, text_col, _output_col, _row_ids = _seed_project(tmp_path)
    program = _EmptyScopeRecipe()
    spec = {
        "action_kind": program.name,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "row_ids": [],
    }
    try:
        # regex_extract is generation-managed, so this direct-root contract
        # test runs through the canonical output-claim helper (a claimless
        # managed run is refused by design).
        initial = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project, ModelRouter(), authority=UnroutedOnlyAuthority(project)
                ),
                spec,
                program=program,
                confirmed=True,
            )
        )
        assert RunResultStore(project).has_run_row_scope(initial.run_id)
        assert RunResultStore(project).run_row_scope(initial.run_id) == []

        project.add_rows(sheet_id, [{"text": "late arrival"}], {"text": text_col})
        resume_spec = {key: value for key, value in spec.items() if key != "row_ids"}

        # regex_extract is generation-managed: the completed run's generation
        # is sealed, and resuming a sealed managed run REFUSES rather than
        # re-executing. The pinned property — a resume never widens onto rows
        # added after the run's immutable scope was recorded — now holds by
        # refusal instead of by an empty re-run; a deliberate continuation
        # must create a fresh run/generation.
        with pytest.raises(GenerationSealedError):
            asyncio.run(
                run_with_output_claim(
                    MapRunner(
                        project, ModelRouter(), authority=UnroutedOnlyAuthority(project)
                    ),
                    resume_spec,
                    program=program,
                    confirmed=True,
                    resume_run_id=initial.run_id,
                )
            )

        payload = action_run_rows_payload(project, initial.run_id, offset=0, limit=10)
        assert payload["total"] == 0
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?",
                (initial.run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()
