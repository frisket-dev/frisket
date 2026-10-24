"""Focused contract tests for invocation-scoped recipe resources."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import frisket.engine.runner.map_runner as map_runner_module
import frisket.engine.runner.validation as validation_module
from frisket.ai.llm import ModelRouter
from frisket.ops.base import (
    OpContext,
    Recipe,
    RecipeInvocationHalt,
    normalize_recipe_invocation_halt,
    persisted_recipe_invocation_halt,
)
from frisket.engine.runner import MapRunner
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.result_generations import (
    GenerationDeclarationConflict,
    GenerationSealedError,
)
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from runner_test_helpers import run_with_output_claim
from typed_model_fixtures import prepare_model_run


def test_exact_target_rows_refuse_membership_lost_after_resolution() -> None:
    class VisibleSheetDb:
        def execute(self, sql: str, params: tuple[int]) -> Any:
            del sql, params
            return self

        def fetchone(self) -> tuple[int]:
            return (1,)

    class ChangingProject:
        calls = 0
        db = VisibleSheetDb()

        def visible_row_ids(self, sheet_id: int, row_ids: list[int]) -> list[int]:
            assert sheet_id == 7
            self.calls += 1
            return row_ids if self.calls == 1 else row_ids[:-1]

    project = ChangingProject()
    spec = {"sheet_id": 7, "row_ids": [11, 12]}
    assert validation_module.target_rows(project, spec) == [11, 12]  # type: ignore[arg-type]

    with pytest.raises(validation_module.InvalidTargetRows) as exc_info:
        validation_module.target_rows(project, spec)  # type: ignore[arg-type]

    assert exc_info.value.missing == [12]


def test_target_rows_refuse_sheet_hidden_after_resolution(tmp_path) -> None:
    project = Project.create(tmp_path / "hidden-sheet.frisket")
    try:
        sheet_id = project.add_sheet("data")
        row_id = project.add_rows(sheet_id, [{}], {})[0]
        assert validation_module.target_rows(
            project,
            {"sheet_id": sheet_id, "row_ids": [row_id]},
        ) == [row_id]
        project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet_id,))

        with pytest.raises(validation_module.InvalidTargetSheet):
            validation_module.target_rows(
                project,
                {"sheet_id": sheet_id, "row_ids": [row_id]},
            )
    finally:
        project.close()


def test_all_empty_probe_pages_large_explicit_scope() -> None:
    class ProjectProbe:
        chunk_sizes: list[int] = []

        def get_values(
            self,
            sheet_id: int,
            column_id: int,
            *,
            row_ids: list[int],
        ) -> dict[int, None]:
            assert sheet_id == 7
            assert column_id == 8
            self.chunk_sizes.append(len(row_ids))
            return {row_id: None for row_id in row_ids}

    project = ProjectProbe()
    assert validation_module.source_values_all_empty(
        project,  # type: ignore[arg-type]
        7,
        {"text": 8},
        ["text"],
        list(range(1, 1_202)),
    )
    assert project.chunk_sizes == [500, 500, 201]


class _ScopeRecipe(Recipe):
    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)

    def __init__(self) -> None:
        super().__init__(name="test.execution_scope", llm=False)
        self.entries = 0
        self.exits = 0
        self.expected_rows: list[int] = []
        self.calls: list[int] = []
        self.halt_on_call: int | None = None
        self.error_on_call: tuple[int, BaseException] | None = None
        self.halt_on_entry: RecipeInvocationHalt | None = None
        self.block = False
        self.started = asyncio.Event()
        self.scope_run_snapshots: list[tuple[str, dict[str, Any]]] = []

    def source_columns(self, spec: dict) -> list[str]:
        return ["text"]

    def output_fields(self, spec: dict) -> list[dict[str, Any]]:
        return [
            {
                "name": str(spec.get("output_name") or "derived"),
                "column_type": "text",
                "schema": {"type": "string"},
            }
        ]

    @asynccontextmanager
    async def execution_scope(
        self,
        spec: dict,
        ctx: OpContext,
        *,
        expected_rows: int,
    ):
        del spec
        self.entries += 1
        self.expected_rows.append(expected_rows)
        row = ctx.project.db.execute(
            "SELECT status, params FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            self.scope_run_snapshots.append(
                (str(row["status"]), json.loads(row["params"] or "{}"))
            )
        if self.halt_on_entry is not None:
            raise self.halt_on_entry
        try:
            yield
        finally:
            self.exits += 1

    async def execute(
        self, row_values: dict[str, Any], spec: dict, ctx: OpContext
    ) -> dict[str, Any]:
        self.calls.append(int(ctx.extras["row_id"]))
        call_number = len(self.calls)
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.halt_on_call == call_number:
            raise RecipeInvocationHalt(
                "local_session_failed",
                "session exited while a row was in flight",
            )
        if self.error_on_call is not None and self.error_on_call[0] == call_number:
            raise self.error_on_call[1]
        return {str(spec.get("output_name") or "derived"): row_values["text"].upper()}


class _BatchScopeRecipe(_ScopeRecipe):
    def __init__(self) -> None:
        super().__init__()
        self.name = "test.batch_execution_scope"
        self.batch_error: BaseException | None = None

    async def execute_batch(
        self,
        values_by_row: dict[int, dict[str, Any]],
        spec: dict,
        ctx: OpContext,
    ) -> dict[int, dict[str, Any]]:
        del ctx
        assert self.entries == self.exits + 1
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.batch_error is not None:
            raise self.batch_error
        output_name = str(spec.get("output_name") or "derived")
        return {
            row_id: {output_name: values["text"].upper()}
            for row_id, values in values_by_row.items()
        }


@pytest.fixture
def scope_recipe(monkeypatch: pytest.MonkeyPatch) -> _ScopeRecipe:
    recipe = _ScopeRecipe()
    original_get_recipe = validation_module.get_recipe
    monkeypatch.setattr(
        validation_module,
        "get_recipe",
        lambda name: recipe if name == recipe.name else original_get_recipe(name),
    )
    return recipe


def _seed_project(tmp_path, *, rows: int = 8) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "execution-scope.frisket")
    sheet_id = project.add_sheet("data")
    columns = {"text": project.add_column(sheet_id, "text")}
    project.add_rows(
        sheet_id,
        [{"text": f"row {index}"} for index in range(rows)],
        columns,
    )
    row_ids = [
        int(row["id"])
        for row in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet_id,)
        )
    ]
    return project, sheet_id, row_ids


def _spec(recipe: Recipe, sheet_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "action_kind": recipe.name,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "output_name": "derived",
        **extra,
    }


@pytest.mark.parametrize("mode", ["immediate", "local", "checkpoint"])
def test_local_publication_batches_keep_row_errors_and_committed_progress(
    tmp_path, scope_recipe, monkeypatch, mode
):
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=7)
    scope_recipe.batch_local_results = mode != "immediate"
    if mode == "checkpoint":
        monkeypatch.setattr(
            scope_recipe, "requires_row_effect_checkpoint", lambda _spec: True
        )
    scope_recipe.error_on_call = (2, ValueError("bad source row"))
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_ROWS", 3)
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_SECONDS", 1000)
    batches = []
    publish = RunResultStore.publish_result_batch

    def record(store, run_id, batch, **kwargs):
        publish(store, run_id, batch, **kwargs)
        batches.append(len({item["row_id"] for item in batch}))

    monkeypatch.setattr(RunResultStore, "publish_result_batch", record)
    reader = sqlite3.connect(project.path / "project.db")

    def progress_committed(progress):
        count = reader.execute(
            "SELECT count(DISTINCT row_id) FROM results WHERE run_id=?",
            (progress.run_id,),
        ).fetchone()[0]
        assert count == progress.completed

    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
            on_progress=progress_committed,
        )
        progress = asyncio.run(
            run_with_output_claim(runner, _spec(scope_recipe, sheet_id))
        )
        assert progress.completed == 7
        assert progress.failed == 1
        assert batches == ([3, 3, 1] if mode == "local" else [1] * 7)
        failed = project.db.execute(
            "SELECT row_id, error FROM results WHERE run_id=? AND error IS NOT NULL",
            (progress.run_id,),
        ).fetchall()
        assert len(failed) == 1 and failed[0]["row_id"] == row_ids[1]
        assert "bad source row" in failed[0]["error"]
    finally:
        reader.close()
        project.close()


def test_local_publication_flushes_on_time_and_reads_each_dispatch_page(
    tmp_path, scope_recipe, monkeypatch
):
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=4)
    scope_recipe.batch_local_results = True
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_ROWS", 3)
    clock = [0.0]
    monkeypatch.setattr(
        map_runner_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    execute = scope_recipe.execute
    column = next(c["id"] for c in project.columns(sheet_id) if c["name"] == "text")

    async def advance(values, spec, ctx):
        result = await execute(values, spec, ctx)
        clock[0] += 0.06
        if len(scope_recipe.calls) == 1:
            project.apply_edits(
                [
                    {"row_id": row_id, "column_id": column, "value": "changed"}
                    for row_id in (row_ids[1], row_ids[3])
                ]
            )
        return result

    monkeypatch.setattr(scope_recipe, "execute", advance)
    committed = []
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
            on_progress=lambda progress: committed.append(progress.completed),
        )
        progress = asyncio.run(
            run_with_output_claim(runner, _spec(scope_recipe, sheet_id))
        )
        assert committed == [0, 2, 4, 4]
        values = project.db.execute(
            "SELECT value FROM results WHERE run_id=? ORDER BY row_id",
            (progress.run_id,),
        ).fetchall()
        # Row 2 was dispatched with the first page; row 4's page was read
        # after the edit. Neither row substitutes a newer source ref later.
        assert [row[0] for row in values] == ["ROW 0", "ROW 1", "ROW 2", "CHANGED"]
    finally:
        project.close()


@pytest.mark.parametrize("stop", ["cancel", "halt"])
def test_local_pending_rows_drain_before_cancel_or_halt_finalization(
    tmp_path, scope_recipe, monkeypatch, stop
):
    project, sheet_id, row_ids = _seed_project(tmp_path)
    scope_recipe.batch_local_results = True
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_SECONDS", 1000)
    if stop == "halt":
        scope_recipe.halt_on_call = 3
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
            should_cancel=(lambda _run_id: len(scope_recipe.calls) >= 3)
            if stop == "cancel"
            else None,
        )
        progress = asyncio.run(
            run_with_output_claim(runner, _spec(scope_recipe, sheet_id))
        )
        assert progress.cancelled and progress.completed == 2
        actual = project.db.execute(
            "SELECT row_id FROM results WHERE run_id=? ORDER BY row_id",
            (progress.run_id,),
        ).fetchall()
        assert [row[0] for row in actual] == row_ids[:2]
        run = project.db.execute(
            "SELECT * FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled" and run["completed_rows"] == 2
        assert scope_recipe.exits == 1
    finally:
        project.close()


def test_local_batch_publication_refusal_keeps_pending_rows_atomic(
    tmp_path, scope_recipe, monkeypatch
):
    project, sheet_id, row_ids = _seed_project(tmp_path)
    scope_recipe.batch_local_results = True
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_ROWS", 3)
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_SECONDS", 1000)
    # Refuse the second member after the store has started writing the first.
    project.db.execute(
        f"CREATE TEMP TRIGGER refuse_result BEFORE INSERT ON results "
        f"WHEN NEW.row_id={row_ids[1]} "
        "BEGIN SELECT RAISE(ABORT, 'publication refused'); END"
    )
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
        )
        with pytest.raises(sqlite3.IntegrityError, match="publication refused"):
            asyncio.run(run_with_output_claim(runner, _spec(scope_recipe, sheet_id)))
        assert project.db.execute("SELECT count(*) FROM results").fetchone()[0] == 0
        assert scope_recipe.exits == 1
    finally:
        project.close()


def test_local_rows_yield_to_external_cancellation_and_drain_pending_results(
    tmp_path, scope_recipe, monkeypatch
):
    project, sheet_id, _row_ids = _seed_project(tmp_path, rows=96)
    scope_recipe.batch_local_results = True
    monkeypatch.setattr(map_runner_module, "LOCAL_RESULT_BATCH_SECONDS", 1000)
    progress_seen = []
    runner = MapRunner(
        project,
        ModelRouter(keys={}),
        concurrency=1,
        authority=UnroutedOnlyAuthority(project),
        on_progress=progress_seen.append,
    )

    async def exercise():
        task = asyncio.create_task(
            run_with_output_claim(runner, _spec(scope_recipe, sheet_id))
        )
        await scope_recipe.started.wait()
        assert 0 < len(scope_recipe.calls) < 96
        progress_seen[0].cancel()
        return await asyncio.wait_for(task, timeout=2)

    try:
        progress = asyncio.run(exercise())
        assert progress.cancelled and 0 < progress.completed < 96
        assert (
            project.db.execute(
                "SELECT count(*) FROM results WHERE run_id=?", (progress.run_id,)
            ).fetchone()[0]
            == progress.completed
        )
        assert scope_recipe.exits == 1
    finally:
        project.close()


@pytest.mark.parametrize("concurrency", [1, 2])
def test_preferred_admitted_row_takes_the_next_free_worker_without_restart(
    tmp_path, scope_recipe, monkeypatch, concurrency
):
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=7)
    initial_rows = set(row_ids[:concurrency])
    preferred_row = row_ids[5]
    hints: list[int] = []
    all_initial_started = asyncio.Event()
    release_first = asyncio.Event()
    release_others = asyncio.Event()
    release_preferred = asyncio.Event()
    preferred_started = asyncio.Event()
    cancelled: list[int] = []
    active = 0
    max_active = 0

    async def execute(values, spec, ctx):
        nonlocal active, max_active
        row_id = int(ctx.extras["row_id"])
        active += 1
        max_active = max(max_active, active)
        try:
            scope_recipe.calls.append(row_id)
            if initial_rows.issubset(scope_recipe.calls):
                all_initial_started.set()
            if row_id == preferred_row:
                preferred_started.set()
                await release_preferred.wait()
            if row_id == row_ids[0]:
                await release_first.wait()
            elif row_id in initial_rows:
                await release_others.wait()
            return {"derived": values["text"].upper()}
        except asyncio.CancelledError:
            cancelled.append(row_id)
            raise
        finally:
            active -= 1

    monkeypatch.setattr(scope_recipe, "execute", execute)
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=concurrency,
            authority=UnroutedOnlyAuthority(project),
            take_preferred_row=lambda: hints.pop(0) if hints else None,
        )

        async def exercise():
            task = asyncio.create_task(
                run_with_output_claim(runner, _spec(scope_recipe, sheet_id))
            )
            await asyncio.wait_for(all_initial_started.wait(), timeout=5)
            hints.append(preferred_row)
            release_first.set()
            await asyncio.wait_for(preferred_started.wait(), timeout=5)
            if concurrency == 2:
                assert active == 2
            release_preferred.set()
            release_others.set()
            return await asyncio.wait_for(task, timeout=5)

        progress = asyncio.run(exercise())
        assert progress.done and progress.completed == len(row_ids)
        assert scope_recipe.calls[:concurrency] == row_ids[:concurrency]
        assert scope_recipe.calls[concurrency] == preferred_row
        assert sorted(scope_recipe.calls) == sorted(row_ids)
        assert len(scope_recipe.calls) == len(set(scope_recipe.calls))
        assert max_active == concurrency
        assert cancelled == []
    finally:
        project.close()


@pytest.mark.parametrize("failed_write", [None, "page", "clear"])
def test_page_progress_is_best_effort_and_clears_before_the_next_row(
    tmp_path, scope_recipe, monkeypatch, caplog, failed_write
):
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=2)
    observed = []
    original_execute = scope_recipe.execute
    original_write = RunResultStore.set_work_progress

    def write(store, run_id, **values):
        if failed_write == "page" and values["files"][0]["completed"] == 1:
            raise sqlite3.OperationalError("injected progress failure")
        return original_write(store, run_id, **values)

    def fail_clear(*args):
        raise sqlite3.OperationalError("injected progress failure")

    async def execute(values, spec, ctx):
        run_id = ctx.extras["run_id"]
        observed.append(
            project.db.execute(
                "SELECT work_progress FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
        )
        if ctx.extras["row_id"] == row_ids[0]:
            for completed in (0, 1, 3):
                ctx.extras["report_work_progress"](
                    kind="ocr_pages", row_id=row_ids[0], completed=completed, total=3
                )
        return await original_execute(values, spec, ctx)

    monkeypatch.setattr(scope_recipe, "execute", execute)
    monkeypatch.setattr(RunResultStore, "set_work_progress", write)
    if failed_write == "clear":
        monkeypatch.setattr(RunResultStore, "clear_work_progress", fail_clear)
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
        )
        progress = asyncio.run(
            run_with_output_claim(runner, _spec(scope_recipe, sheet_id))
        )
        assert progress.done and progress.completed == 2 and progress.failed == 0
        run = runner.run_store.get_run(progress.run_id)
        assert run["status"] == "completed"
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 2
        if failed_write != "clear":
            assert observed == [None, None]
            assert run["work_progress"] is None
        if failed_write is not None:
            assert "work_progress_write_failed" in caplog.text
    finally:
        project.close()


def test_concurrent_page_progress_keeps_each_file_and_removes_only_finished_row(
    tmp_path, scope_recipe, monkeypatch
):
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=2)
    original_execute = scope_recipe.execute
    original_write = RunResultStore.set_work_progress
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    first_updated = asyncio.Event()
    finish_first = asyncio.Event()
    first_removed = asyncio.Event()
    both_seen = False
    clock = [100.0]
    monkeypatch.setattr(
        map_runner_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def write(store, run_id, **values):
        nonlocal both_seen
        original_write(store, run_id, **values)
        rows = {file["row_id"] for file in values["files"]}
        both_seen = both_seen or rows == set(row_ids)
        if both_seen and rows == {row_ids[1]}:
            first_removed.set()

    def files(run_id):
        saved = project.db.execute(
            "SELECT work_progress FROM runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        return {file["row_id"]: file for file in json.loads(saved)["files"]}

    async def execute(values, spec, ctx):
        row_id = ctx.extras["row_id"]
        report = ctx.extras["report_work_progress"]
        total = 196 if row_id == row_ids[0] else 24_909
        filename = "a" * 201 if row_id == row_ids[0] else "second.pdf"
        if row_id == row_ids[1]:
            await first_started.wait()
        report(
            kind="ocr_pages", row_id=row_id, completed=0, total=total, filename=filename
        )
        if row_id == row_ids[0]:
            first_started.set()
            await second_started.wait()
            clock[0] += 0.6
            report(
                kind="ocr_pages",
                row_id=row_id,
                completed=2,
                total=total,
                filename=filename,
            )
            first_updated.set()
            await finish_first.wait()
        else:
            second_started.set()
            await first_updated.wait()
            # Same tick as the sibling: its write must not throttle this file.
            report(
                kind="ocr_pages",
                row_id=row_id,
                completed=2,
                total=total,
                filename=filename,
            )
            current = files(ctx.extras["run_id"])
            assert set(current) == set(row_ids)
            assert [current[row]["completed"] for row in row_ids] == [2, 2]
            assert [current[row]["total"] for row in row_ids] == [196, 24_909]
            assert current[row_ids[0]]["filename"] == "a" * 200
            finish_first.set()
            await first_removed.wait()
            assert files(ctx.extras["run_id"]) == {row_id: current[row_id]}
        return await original_execute(values, spec, ctx)

    monkeypatch.setattr(scope_recipe, "execute", execute)
    monkeypatch.setattr(RunResultStore, "set_work_progress", write)
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=2,
            authority=UnroutedOnlyAuthority(project),
        )

        async def exercise():
            return await asyncio.wait_for(
                run_with_output_claim(runner, _spec(scope_recipe, sheet_id)), timeout=5
            )

        progress = asyncio.run(exercise())
        assert progress.completed == 2 and progress.failed == 0
        assert runner.run_store.get_run(progress.run_id)["work_progress"] is None
    finally:
        project.close()


@pytest.mark.realtime
def test_page_progress_does_not_wait_for_or_commit_another_write(tmp_path):
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=1)
    store = RunResultStore(project)
    op = project.append_op("map.regex_extract", {})
    run_id = store.start_run(op, sheet_id, "map.regex_extract")
    project.db.execute("PRAGMA busy_timeout=2000")
    try:
        with sqlite3.connect(project.db_path) as competing:
            competing.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store.set_work_progress(
                    run_id, files=[{"row_id": row_ids[0], "completed": 1, "total": 3}]
                )
            assert time.monotonic() - started < 0.5
            assert project.db.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
            assert not project.db.in_transaction
        store.set_work_progress(
            run_id, files=[{"row_id": row_ids[0], "completed": 2, "total": 3}]
        )
        project.db.execute("UPDATE runs SET model='uncommitted' WHERE id=?", (run_id,))
        store.clear_work_progress(run_id)
        assert project.db.in_transaction
        project.db.rollback()
        run = store.get_run(run_id)
        assert run["model"] is None
        assert json.loads(run["work_progress"])["files"][0]["completed"] == 2
    finally:
        project.close()


def test_exact_ner_rechecks_fresh_output_during_prepare(tmp_path) -> None:
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=1)
    project.add_column(
        sheet_id,
        "entities",
        type="json",
        ai_generated=True,
    )
    runner = MapRunner(
        project,
        ModelRouter(keys={}),
        authority=UnroutedOnlyAuthority(project),
    )
    spec = {
        "action_kind": "map.ner",
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "labels": ["person"],
        "engine": "gliner",
        "output_name": "entities",
        "row_ids": row_ids,
    }
    try:
        before_ops = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        before = project.get_column(
            int(
                project.db.execute(
                    "SELECT id FROM columns WHERE name='entities'"
                ).fetchone()[0]
            )
        )
        with pytest.raises(
            GenerationDeclarationConflict, match="no generation history"
        ):
            prepare_model_run(runner, spec)

        after = project.get_column(int(before["id"]))
        assert tuple(after) == tuple(before)
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert (
            project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == before_ops
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM run_output_generations"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_exact_scope_rechecks_membership_inside_prepare_transaction(
    tmp_path,
    scope_recipe: _ScopeRecipe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=1)
    runner = MapRunner(
        project,
        ModelRouter(keys={}),
        authority=UnroutedOnlyAuthority(project),
    )
    original_validate = runner._validate_prepare_spec  # noqa: SLF001

    def hide_row_after_validation(*args: Any, **kwargs: Any) -> Any:
        validated = original_validate(*args, **kwargs)
        project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (row_ids[0],))
        project.db.commit()
        return validated

    monkeypatch.setattr(runner, "_validate_prepare_spec", hide_row_after_validation)
    try:
        with pytest.raises(validation_module.InvalidTargetRows) as exc_info:
            runner.prepare_run(
                _spec(scope_recipe, sheet_id, row_ids=row_ids),
            )

        assert exc_info.value.missing == row_ids
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


def _prepare_claimed_run(
    runner: MapRunner,
    spec: dict[str, Any],
) -> tuple[Any, str]:
    """Compose the runner with the output reservation its writers require."""

    recipe = validation_module.recipe_for_spec(spec)
    output_names = [str(field["name"]) for field in recipe.output_fields(spec)]
    claim_token = f"output-claim:test:{recipe.name}:{spec['sheet_id']}:{id(runner)}"
    claims, conflict = OutputColumnClaimStore(runner.project).acquire(
        sheet_id=int(spec["sheet_id"]),
        output_names=output_names,
        action_kind=recipe.name,
        claim_token=claim_token,
        lease_seconds=6 * 60 * 60,
    )
    assert conflict is None
    assert len(claims) == len(output_names)
    prepared = runner._prepare(  # noqa: SLF001 - runner contract probe
        spec,
        confirmed=False,
        resume_run_id=None,
    )
    assert OutputColumnClaimStore(runner.project).bind_to_run(
        claim_token=claim_token,
        run_id=prepared.run_id,
        expected_output_names=output_names,
    ) == len(output_names)
    return prepared, claim_token


async def _wait_for_started_or_task_exit(
    task: asyncio.Task[Any],
    started: asyncio.Event,
    *,
    timeout: float = 2.0,
) -> None:
    """Bound setup waits without hiding an early runner refusal/exception."""

    started_wait = asyncio.create_task(started.wait())
    try:
        done, _pending = await asyncio.wait(
            {task, started_wait},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done, "runner neither started work nor completed within the bound"
        if task in done:
            await task
        assert started_wait in done
    finally:
        if not started_wait.done():
            started_wait.cancel()
            await asyncio.gather(started_wait, return_exceptions=True)


def test_scope_enters_once_for_durable_run_and_preview(
    tmp_path, scope_recipe: _ScopeRecipe
) -> None:
    project, sheet_id, row_ids = _seed_project(tmp_path)
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=3,
            authority=UnroutedOnlyAuthority(project),
        )
        progress = asyncio.run(
            run_with_output_claim(runner, _spec(scope_recipe, sheet_id))
        )

        assert progress.done
        assert progress.completed == len(row_ids)
        assert scope_recipe.entries == 1
        assert scope_recipe.exits == 1
        assert scope_recipe.expected_rows == [len(row_ids)]

        with pytest.raises(GenerationSealedError, match="is sealed"):
            asyncio.run(
                run_with_output_claim(
                    runner,
                    _spec(scope_recipe, sheet_id),
                    resume_run_id=progress.run_id,
                )
            )
        assert scope_recipe.entries == 1
        assert scope_recipe.exits == 1

        scope_recipe.entries = 0
        scope_recipe.exits = 0
        scope_recipe.expected_rows.clear()
        preview = asyncio.run(
            runner.preview(_spec(scope_recipe, sheet_id, row_ids=row_ids[:3]))
        )

        assert preview.sampled == 3
        assert set(preview.values) == set(row_ids[:3])
        assert scope_recipe.entries == 1
        assert scope_recipe.exits == 1
        assert scope_recipe.expected_rows == [3]
    finally:
        project.close()


def test_scope_halt_preserves_committed_prefix_without_prefetch_deadlock(
    tmp_path, scope_recipe: _ScopeRecipe
) -> None:
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=40)
    scope_recipe.halt_on_call = 3
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
        )
        progress = asyncio.run(
            asyncio.wait_for(
                run_with_output_claim(runner, _spec(scope_recipe, sheet_id)),
                timeout=2,
            )
        )

        assert progress.cancelled
        assert progress.halted_code == "local_session_failed"
        assert progress.completed == 2
        assert scope_recipe.entries == 1
        assert scope_recipe.exits == 1
        run = project.db.execute(
            "SELECT * FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled"
        params = json.loads(run["params"])
        assert params["halted_code"] == "local_session_failed"
        assert params["halted_reason"] == "session exited while a row was in flight"
        completed = project.db.execute(
            "SELECT COUNT(DISTINCT row_id) FROM results WHERE run_id=?",
            (progress.run_id,),
        ).fetchone()[0]
        assert completed == 2
        pending = set(row_ids) - {
            int(row["row_id"])
            for row in project.db.execute(
                "SELECT DISTINCT row_id FROM results WHERE run_id=?",
                (progress.run_id,),
            )
        }
        assert pending == set(row_ids[2:])
    finally:
        project.close()


def test_halted_run_recovery_uses_fresh_scoped_generation(
    tmp_path, scope_recipe: _ScopeRecipe
) -> None:
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=5)
    scope_recipe.halt_on_call = 3
    try:
        runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
        )
        first = asyncio.run(
            run_with_output_claim(runner, _spec(scope_recipe, sheet_id))
        )
        assert first.halted_code == "local_session_failed"

        scope_recipe.halt_on_call = None
        scope_recipe.scope_run_snapshots.clear()
        retry_spec = _spec(scope_recipe, sheet_id, row_ids=row_ids[2:])
        retry_spec["overwrite"] = True
        recovered = asyncio.run(
            run_with_output_claim(
                runner,
                retry_spec,
            )
        )

        assert recovered.run_id != first.run_id
        assert recovered.completed == 3
        assert scope_recipe.scope_run_snapshots == [
            (
                "running",
                {
                    "input_columns": ["text"],
                    "output_name": "derived",
                    "action_kind": scope_recipe.name,
                    "sheet_id": sheet_id,
                    "row_ids": row_ids[2:],
                    "overwrite": True,
                },
            )
        ]
        old_run = project.db.execute(
            "SELECT status, params FROM runs WHERE id=?", (first.run_id,)
        ).fetchone()
        assert old_run["status"] == "cancelled"
        assert json.loads(old_run["params"])["halted_code"] == "local_session_failed"
        run = project.db.execute(
            "SELECT status, params FROM runs WHERE id=?", (recovered.run_id,)
        ).fetchone()
        assert run["status"] == "completed"
        params = json.loads(run["params"])
        assert "halted_code" not in params
        assert "halted_reason" not in params
    finally:
        project.close()


def test_queue_style_recovery_uses_fresh_scoped_generation(
    tmp_path, scope_recipe: _ScopeRecipe
) -> None:
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=5)
    scope_recipe.halt_on_call = 3
    try:
        first_runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            authority=UnroutedOnlyAuthority(project),
        )
        first = asyncio.run(
            run_with_output_claim(
                first_runner,
                _spec(scope_recipe, sheet_id),
            )
        )
        assert first.halted_code == "local_session_failed"
        assert (
            project.db.execute(
                "SELECT status FROM runs WHERE id=?", (first.run_id,)
            ).fetchone()[0]
            == "cancelled"
        )

        scope_recipe.halt_on_call = None
        scope_recipe.scope_run_snapshots.clear()
        run_store = MapRunner(
            project, ModelRouter(keys={}), authority=UnroutedOnlyAuthority(project)
        ).run_store

        def durable_cancelled(run_id: int) -> bool:
            row = run_store.get_run(run_id)
            return bool(row and row["status"] == "cancelled")

        queued_runner = MapRunner(
            project,
            ModelRouter(keys={}),
            concurrency=1,
            should_cancel=durable_cancelled,
            authority=UnroutedOnlyAuthority(project),
        )
        retry_spec = _spec(scope_recipe, sheet_id, row_ids=row_ids[2:])
        retry_spec["overwrite"] = True
        recovered = asyncio.run(
            run_with_output_claim(
                queued_runner,
                retry_spec,
            )
        )

        assert recovered.run_id != first.run_id
        assert recovered.completed == 3
        assert recovered.cancelled is False
        assert scope_recipe.scope_run_snapshots == [
            (
                "running",
                {
                    "input_columns": ["text"],
                    "output_name": "derived",
                    "action_kind": scope_recipe.name,
                    "sheet_id": sheet_id,
                    "row_ids": row_ids[2:],
                    "overwrite": True,
                },
            )
        ]
        old_run = run_store.get_run(first.run_id)
        assert old_run["status"] == "cancelled"
        run = run_store.get_run(recovered.run_id)
        assert run["status"] == "completed"
        params = json.loads(run["params"])
        assert "halted_code" not in params
        assert "halted_reason" not in params
    finally:
        project.close()


def test_pre_cancel_and_zero_work_never_enter_scope(
    tmp_path, scope_recipe: _ScopeRecipe
) -> None:
    project, sheet_id, _row_ids = _seed_project(tmp_path, rows=2)
    try:
        cancelled = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    ModelRouter(keys={}),
                    should_cancel=lambda _run_id: True,
                    authority=UnroutedOnlyAuthority(project),
                ),
                _spec(scope_recipe, sheet_id),
            )
        )
        assert cancelled.cancelled
        assert scope_recipe.entries == 0
        run = project.db.execute(
            "SELECT status, params FROM runs WHERE id=?", (cancelled.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled"
        params = json.loads(run["params"])
        assert "halted_code" not in params
        assert "halted_reason" not in params

        empty_project, empty_sheet, _ = _seed_project(tmp_path / "empty", rows=0)
        try:
            empty = asyncio.run(
                run_with_output_claim(
                    MapRunner(
                        empty_project,
                        ModelRouter(keys={}),
                        authority=UnroutedOnlyAuthority(empty_project),
                    ),
                    _spec(scope_recipe, empty_sheet),
                )
            )
            assert empty.total == 0
            assert scope_recipe.entries == 0
        finally:
            empty_project.close()
    finally:
        project.close()


def test_outer_cancellation_joins_workers_before_scope_exit(
    tmp_path, scope_recipe: _ScopeRecipe, monkeypatch
) -> None:
    project, sheet_id, _row_ids = _seed_project(tmp_path, rows=4)
    scope_recipe.block = True
    original_execute = scope_recipe.execute

    async def execute(values, spec, ctx):
        ctx.extras["report_work_progress"](
            kind="ocr_pages", row_id=ctx.extras["row_id"], completed=1, total=196
        )
        return await original_execute(values, spec, ctx)

    monkeypatch.setattr(scope_recipe, "execute", execute)

    runner = MapRunner(
        project,
        ModelRouter(keys={}),
        concurrency=2,
        authority=UnroutedOnlyAuthority(project),
    )
    spec = _spec(scope_recipe, sheet_id)
    prepared, claim_token = _prepare_claimed_run(runner, spec)

    async def exercise() -> None:
        task = asyncio.create_task(
            runner.run(
                spec,
                prepared_run=prepared,
                claim_token=claim_token,
            )
        )
        await _wait_for_started_or_task_exit(task, scope_recipe.started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    try:
        asyncio.run(exercise())
        assert scope_recipe.entries == 1
        assert scope_recipe.exits == 1
        run = project.db.execute(
            "SELECT status, current_attempt_id, work_progress FROM runs WHERE id=?",
            (prepared.run_id,),
        ).fetchone()
        assert run["status"] == "cancelled"
        assert run["work_progress"] is None
        assert isinstance(run["current_attempt_id"], str)
        attempt = project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (run["current_attempt_id"],),
        ).fetchone()
        assert attempt is not None
        assert attempt["state"] == "dispatching"
        assert (
            project.db.execute(
                "SELECT status FROM output_column_claims WHERE claim_token=?",
                (claim_token,),
            ).fetchone()[0]
            == "active"
        )
    finally:
        project.close()


def test_outer_cancellation_fenced_terminalizes_batch_run_before_reraise(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, sheet_id, _row_ids = _seed_project(tmp_path, rows=4)
    recipe = _BatchScopeRecipe()
    recipe.block = True
    original_get_recipe = validation_module.get_recipe
    monkeypatch.setattr(
        validation_module,
        "get_recipe",
        lambda name: recipe if name == recipe.name else original_get_recipe(name),
    )
    runner = MapRunner(
        project,
        ModelRouter(keys={}),
        authority=UnroutedOnlyAuthority(project),
    )
    spec = _spec(recipe, sheet_id)
    prepared, claim_token = _prepare_claimed_run(runner, spec)

    async def exercise() -> None:
        task = asyncio.create_task(
            runner.run(
                spec,
                prepared_run=prepared,
                claim_token=claim_token,
            )
        )
        await _wait_for_started_or_task_exit(task, recipe.started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    try:
        asyncio.run(exercise())
        run = project.db.execute(
            "SELECT status, current_attempt_id FROM runs WHERE id=?",
            (prepared.run_id,),
        ).fetchone()
        assert run["status"] == "cancelled"
        assert isinstance(run["current_attempt_id"], str)
        attempt = project.db.execute(
            "SELECT state FROM execution_attempts WHERE id=?",
            (run["current_attempt_id"],),
        ).fetchone()
        assert attempt is not None
        assert attempt["state"] == "dispatching"
        assert (
            project.db.execute(
                "SELECT status FROM output_column_claims WHERE claim_token=?",
                (claim_token,),
            ).fetchone()[0]
            == "active"
        )
    finally:
        project.close()


def test_persistence_failure_still_joins_workers_and_exits_scope(
    tmp_path, scope_recipe: _ScopeRecipe, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, _row_ids = _seed_project(tmp_path, rows=12)
    runner = MapRunner(
        project,
        ModelRouter(keys={}),
        concurrency=2,
        authority=UnroutedOnlyAuthority(project),
    )

    def fail_write(
        _run_id: int,
        _results: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> None:
        # Signature-tolerant on purpose: the stub fails EVERY write, so it
        # must keep matching the real write_results as its keyword surface
        # grows (the managed-publication cutover added commit=).
        raise RuntimeError("forced persistence failure")

    monkeypatch.setattr(runner.run_store, "write_results", fail_write)
    try:
        with pytest.raises(RuntimeError, match="forced persistence failure"):
            asyncio.run(
                asyncio.wait_for(
                    run_with_output_claim(
                        runner,
                        _spec(scope_recipe, sheet_id),
                    ),
                    timeout=2,
                )
            )
        assert scope_recipe.entries == 1
        assert scope_recipe.exits == 1
    finally:
        project.close()


def test_unprovable_sandbox_teardown_escapes_row_error_boundary(
    tmp_path, scope_recipe: _ScopeRecipe
) -> None:
    project, sheet_id, _row_ids = _seed_project(tmp_path, rows=4)
    scope_recipe.error_on_call = (
        2,
        SandboxTeardownError("owned process tree is still alive"),
    )
    try:
        with pytest.raises(SandboxTeardownError, match="still alive") as caught:
            failing_runner = MapRunner(
                project,
                ModelRouter(keys={}),
                concurrency=1,
                authority=UnroutedOnlyAuthority(project),
            )
            asyncio.run(
                run_with_output_claim(
                    failing_runner,
                    _spec(scope_recipe, sheet_id),
                )
            )
        assert len(scope_recipe.calls) == 2
        assert scope_recipe.entries == 1
        assert scope_recipe.exits == 1
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
        run = project.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert caught.value.run_id == run["id"]
        assert run["status"] == "cancelled"
        params = json.loads(run["params"])
        assert params["halted_code"] == "local_session_failed"
        assert "still alive" not in params["halted_reason"]
        output = next(
            column
            for column in project.columns(sheet_id)
            if column["name"] == "derived"
        )
        assert output["current_run_id"] == run["id"]
    finally:
        project.close()


def test_halt_metadata_status_and_reused_pointer_finalize_atomically(
    tmp_path, scope_recipe: _ScopeRecipe, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, _row_ids = _seed_project(tmp_path, rows=2)
    try:
        first_runner = MapRunner(
            project, ModelRouter(keys={}), authority=UnroutedOnlyAuthority(project)
        )
        first = asyncio.run(
            run_with_output_claim(
                first_runner,
                _spec(scope_recipe, sheet_id),
            )
        )
        output_column = next(
            column
            for column in project.columns(sheet_id)
            if column["name"] == "derived"
        )
        assert output_column["current_run_id"] == first.run_id

        scope_recipe.halt_on_entry = RecipeInvocationHalt(
            "local_engine_busy", "another invocation owns the local engine"
        )
        second_runner = MapRunner(
            project, ModelRouter(keys={}), authority=UnroutedOnlyAuthority(project)
        )

        def fail_finish(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("forced finalize interruption")

        monkeypatch.setattr(second_runner.run_store, "finish_run", fail_finish)
        with pytest.raises(RuntimeError, match="forced finalize interruption"):
            asyncio.run(
                run_with_output_claim(
                    second_runner,
                    _spec(scope_recipe, sheet_id),
                )
            )

        second = project.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert second["id"] != first.run_id
        assert second["status"] == "running"
        second_params = json.loads(second["params"])
        assert "halted_code" not in second_params
        assert "halted_reason" not in second_params
        pointer = project.db.execute(
            "SELECT current_run_id FROM columns WHERE id=?", (output_column["id"],)
        ).fetchone()[0]
        assert pointer == first.run_id
    finally:
        project.close()


def test_batch_run_and_preview_each_own_one_execution_scope(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _BatchScopeRecipe()
    original_get_recipe = validation_module.get_recipe
    monkeypatch.setattr(
        validation_module,
        "get_recipe",
        lambda name: recipe if name == recipe.name else original_get_recipe(name),
    )
    project, sheet_id, row_ids = _seed_project(tmp_path, rows=3)
    runner = MapRunner(
        project, ModelRouter(keys={}), authority=UnroutedOnlyAuthority(project)
    )
    try:
        progress = asyncio.run(run_with_output_claim(runner, _spec(recipe, sheet_id)))
        preview = asyncio.run(runner.preview(_spec(recipe, sheet_id, row_ids=row_ids)))
        assert progress.completed == 3
        assert preview.sampled == 3
        assert recipe.entries == 2
        assert recipe.exits == 2
        assert recipe.expected_rows == [3, 3]
    finally:
        project.close()


def test_batch_recipe_never_flattens_unprovable_teardown(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _BatchScopeRecipe()
    recipe.batch_error = SandboxTeardownError("owned process tree is still alive")
    original_get_recipe = validation_module.get_recipe
    monkeypatch.setattr(
        validation_module,
        "get_recipe",
        lambda name: recipe if name == recipe.name else original_get_recipe(name),
    )
    project, sheet_id, _row_ids = _seed_project(tmp_path, rows=2)
    try:
        with pytest.raises(SandboxTeardownError, match="still alive") as caught:
            runner = MapRunner(
                project,
                ModelRouter(keys={}),
                authority=UnroutedOnlyAuthority(project),
            )
            asyncio.run(
                run_with_output_claim(
                    runner,
                    _spec(recipe, sheet_id),
                )
            )
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
        run = project.db.execute(
            "SELECT id, status, params FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert caught.value.run_id == run["id"]
        assert run["status"] == "cancelled"
        params = json.loads(run["params"])
        assert params["halted_code"] == "local_session_failed"
        output = next(
            column
            for column in project.columns(sheet_id)
            if column["name"] == "derived"
        )
        assert output["current_run_id"] == run["id"]
    finally:
        project.close()


def test_owned_task_join_prioritizes_sibling_teardown_failure() -> None:
    async def fail(error: BaseException) -> None:
        raise error

    with pytest.raises(SandboxTeardownError, match="still alive") as caught:
        asyncio.run(
            map_runner_module._await_owned_tasks(
                fail(ValueError("ordinary sibling failure")),
                fail(SandboxTeardownError("owned process tree is still alive")),
            )
        )
    assert isinstance(caught.value.__cause__, ValueError)


def test_halt_allowlist_fails_closed_and_redacts_detail() -> None:
    code, detail = normalize_recipe_invocation_halt(
        "child_internal_crash",
        "Authorization: Bearer secret-value",
    )
    assert code == "local_session_failed"
    assert "secret-value" not in detail
    assert "parakeet" not in detail.lower()

    code, detail = normalize_recipe_invocation_halt(
        "local_session_failed",
        "the local Parakeet process stopped",
    )
    assert code == "local_session_failed"
    assert detail == "the local Parakeet process stopped"

    code, detail = normalize_recipe_invocation_halt(
        "local_engine_busy",
        "Authorization: Bearer secret-value " + ("x" * 1_000),
    )
    assert code == "local_engine_busy"
    assert "secret-value" not in detail
    assert len(detail) <= 500
    assert persisted_recipe_invocation_halt(
        {"halted_code": code, "halted_reason": detail}
    ) == (code, detail)
    assert persisted_recipe_invocation_halt(
        {"halted_code": "local_artifact_unavailable"}
    ) == (
        "local_artifact_unavailable",
        "The local engine's model artifacts are unavailable; retry after they are ready.",
    )
    assert (
        persisted_recipe_invocation_halt(
            {
                "halted_code": "child_internal_crash",
                "halted_reason": "must not escape",
            }
        )
        is None
    )
