from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


from frisket.ai.llm import ModelRouter
from frisket.authoring.plugin_registry import (
    register_recipe,
    unregister_recipe,
)
from frisket.ops.base import OpContext, Recipe
from frisket.engine.runner import MapRunner
from frisket.engine.runner.publication import PUBLISH_VALUE
from frisket.engine.runner.result_generations import declare_bound_run_outputs
from frisket.engine.store import Project
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.runs import RunResultStore
from helpers import write_claimed_test_results
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from runner_test_helpers import run_with_output_claim


ROOT = Path(__file__).resolve().parents[2]


class _WindowProbeRecipe(Recipe):
    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)
    RECIPE_NAME = "test.window_probe"

    def source_columns(self, spec: dict) -> list[str]:
        return ["text"]

    def output_fields(self, spec: dict) -> list[dict[str, Any]]:
        return [
            {
                "name": "out",
                "column_type": "text",
                "schema": {"type": "string"},
                "description": "",
            }
        ]

    async def execute(
        self, row_values: dict[str, Any], spec: dict, ctx: OpContext
    ) -> dict[str, Any]:
        state = _WINDOW_PROBE_STATE
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0)
            state["seen"].append(str(row_values["text"]))
            return {"out": row_values["text"]}
        finally:
            state["active"] -= 1


_WINDOW_PROBE_STATE: dict[str, Any] = {"active": 0, "peak": 0, "seen": []}


class _BatchProbeRecipe(Recipe):
    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)
    RECIPE_NAME = "test.batch_probe"

    def source_columns(self, spec: dict) -> list[str]:
        return ["text"]

    def output_fields(self, spec: dict) -> list[dict[str, Any]]:
        return [
            {
                "name": "batch_out",
                "column_type": "text",
                "schema": {"type": "string"},
                "description": "",
            }
        ]

    async def execute_batch(
        self,
        values_by_row: dict[int, dict[str, Any]],
        spec: dict,
        ctx: OpContext,
    ) -> dict[int, dict[str, Any]]:
        _BATCH_PROBE_STATE["seen"] = [
            str(values["text"]) for values in values_by_row.values()
        ]
        return {
            row_id: {"batch_out": values["text"]}
            for row_id, values in values_by_row.items()
        }


_BATCH_PROBE_STATE: dict[str, Any] = {"seen": []}


def _bind_resume_claim(
    project: Project,
    *,
    sheet_id: int,
    run_id: int,
    output_name: str,
    action_kind: str,
) -> str:
    claim_token = f"output-claim:test:paged-resume:{run_id}:{output_name}"
    claims, conflict = OutputColumnClaimStore(project).acquire(
        sheet_id=sheet_id,
        output_names=[output_name],
        action_kind=action_kind,
        claim_token=claim_token,
        lease_seconds=6 * 60 * 60,
    )
    assert conflict is None
    assert len(claims) == 1
    assert (
        OutputColumnClaimStore(project).bind_to_run(
            claim_token=claim_token,
            run_id=run_id,
            expected_output_names=[output_name],
        )
        == 1
    )
    declare_bound_run_outputs(
        project,
        run_id=run_id,
        output_fields=[
            {
                "name": output_name,
                "column_type": "text",
                "schema": {"type": "string"},
                "description": "",
            }
        ],
        claim_token=claim_token,
    )
    return claim_token


def _install_probe_recipe() -> None:
    register_recipe(
        _WindowProbeRecipe(
            name=_WindowProbeRecipe.RECIPE_NAME,
            llm=False,
            description="test-only row-local recipe for bounded executor checks",
        ),
        action_kind=_WindowProbeRecipe.RECIPE_NAME,
        plugin="test",
        replace=True,
    )
    _WINDOW_PROBE_STATE.update({"active": 0, "peak": 0, "seen": []})


def _install_batch_probe_recipe() -> None:
    register_recipe(
        _BatchProbeRecipe(
            name=_BatchProbeRecipe.RECIPE_NAME,
            llm=False,
            description="test-only batch recipe for bounded executor checks",
        ),
        action_kind=_BatchProbeRecipe.RECIPE_NAME,
        plugin="test",
        replace=True,
    )
    _BATCH_PROBE_STATE.update({"seen": []})


def _restore_recipe(name: str) -> None:
    unregister_recipe(name)


class _ExecuteGuard:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        normalized = " ".join(str(sql).split())
        if (
            "SELECT row_id FROM results" in normalized
            and "GROUP BY row_id" in normalized
        ):
            raise AssertionError("ordinary stored-scope resume must page pending rows")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _project(tmp_path: Path, *, rows: int = 64) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "large-run.frisket", name="large run")
    sheet_id = project.add_sheet("Events")
    text_col = project.add_column(sheet_id, "text", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [{"text": f"row-{index:03d}"} for index in range(rows)],
        {"text": text_col},
    )
    return project, sheet_id, row_ids


def _spec(sheet_id: int) -> dict[str, Any]:
    return {"action_kind": _WindowProbeRecipe.RECIPE_NAME, "sheet_id": sheet_id}


def _batch_spec(sheet_id: int) -> dict[str, Any]:
    return {"action_kind": _BatchProbeRecipe.RECIPE_NAME, "sheet_id": sheet_id}


def test_row_local_runs_keep_scheduled_work_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    _install_probe_recipe()
    project, sheet_id, row_ids = _project(tmp_path, rows=72)
    gather_sizes: list[int] = []
    original_gather = asyncio.gather

    async def counting_gather(*aws, **kwargs):
        gather_sizes.append(len(aws))
        return await original_gather(*aws, **kwargs)

    monkeypatch.setattr(
        "frisket.engine.runner.map_runner.asyncio.gather", counting_gather
    )
    try:
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            concurrency=5,
            authority=UnroutedOnlyAuthority(project),
        )
        progress = asyncio.run(
            run_with_output_claim(
                runner,
                _spec(sheet_id),
                confirmed=True,
            )
        )

        assert progress.completed == len(row_ids)
        assert progress.failed == 0
        assert _WINDOW_PROBE_STATE["peak"] <= 5
        assert max(gather_sizes) <= 6
        assert len(_WINDOW_PROBE_STATE["seen"]) == len(row_ids)
        assert len(set(_WINDOW_PROBE_STATE["seen"])) == len(row_ids)
        result_count = project.db.execute(
            "SELECT COUNT(*) AS count FROM results WHERE run_id=?", (progress.run_id,)
        ).fetchone()["count"]
        assert result_count == len(row_ids)
    finally:
        project.close()
        _restore_recipe(_WindowProbeRecipe.RECIPE_NAME)


def test_queued_resume_pages_stored_scope_without_full_scope_load(
    tmp_path: Path, monkeypatch
) -> None:
    _install_probe_recipe()
    project, sheet_id, row_ids = _project(tmp_path, rows=40)
    try:
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        )
        claim_token = "output-claim:test:queued-paged-scope"
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet_id,
            output_names=["out"],
            action_kind=_WindowProbeRecipe.RECIPE_NAME,
            claim_token=claim_token,
            lease_seconds=6 * 60 * 60,
        )
        assert conflict is None
        assert len(claims) == 1
        prepared = runner.prepare_run(_spec(sheet_id), confirmed=True)
        assert (
            OutputColumnClaimStore(project).bind_to_run(
                claim_token=claim_token,
                run_id=prepared.run_id,
                expected_output_names=["out"],
            )
            == 1
        )

        # run_row_scope (the full-scope loader) lives on RunResultStore, not
        # Project (moved by the 2026-06-24 executor architecture refactor,
        # 0078b109); MapRunner always talks to it via a fresh
        # RunResultStore(project) instance (self.run_store), so patching the
        # class method intercepts every instance's calls.
        def explode_full_scope(self: RunResultStore, run_id: int) -> list[int]:
            raise AssertionError("worker resume must page run_rows, not load scope")

        monkeypatch.setattr(RunResultStore, "run_row_scope", explode_full_scope)
        progress = asyncio.run(
            MapRunner(
                project,
                ModelRouter(cache=None, cache_mode="off"),
                concurrency=4,
                authority=UnroutedOnlyAuthority(project),
            ).run(
                _spec(sheet_id),
                confirmed=True,
                resume_run_id=prepared.run_id,
                claim_token=claim_token,
            )
        )

        assert progress.run_id == prepared.run_id
        assert progress.completed == len(row_ids)
        output_col = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='out'",
            (sheet_id,),
        ).fetchone()["id"]
        assert (
            RunResultStore(project).pending_run_row_scope_count(
                progress.run_id, [output_col]
            )
            == 0
        )
    finally:
        project.close()
        _restore_recipe(_WindowProbeRecipe.RECIPE_NAME)


def test_partial_row_local_resume_pages_pending_without_done_row_materialization(
    tmp_path: Path, monkeypatch
) -> None:
    _install_probe_recipe()
    project, sheet_id, row_ids = _project(tmp_path, rows=24)
    prior_conn: Any | None = None
    guarded_conn = False
    try:
        prepared = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        ).prepare_run(_spec(sheet_id), confirmed=True)
        output_col = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='out'",
            (sheet_id,),
        ).fetchone()["id"]
        _bind_resume_claim(
            project,
            sheet_id=sheet_id,
            run_id=prepared.run_id,
            output_name="out",
            action_kind=_WindowProbeRecipe.RECIPE_NAME,
        )
        write_claimed_test_results(
            project,
            prepared.run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": output_col,
                    "value": f"done-{index}",
                    "publication_effect": PUBLISH_VALUE,
                }
                for index, row_id in enumerate(row_ids[:18])
            ],
        )

        # The published partial generation is sealed. Recovery is a fresh
        # explicitly scoped successor over the remaining rows.
        def explode_full_scope(self: RunResultStore, run_id: int) -> list[int]:
            raise AssertionError("worker successor must page run_rows, not load scope")

        monkeypatch.setattr(RunResultStore, "run_row_scope", explode_full_scope)
        prior_conn = project.db
        project._local.conn = _ExecuteGuard(prior_conn)  # noqa: SLF001
        guarded_conn = True

        successor_spec = {
            **_spec(sheet_id),
            "row_ids": row_ids[18:],
            "overwrite": True,
        }
        progress = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    ModelRouter(cache=None, cache_mode="off"),
                    concurrency=3,
                    authority=UnroutedOnlyAuthority(project),
                ),
                successor_spec,
                confirmed=True,
            )
        )

        assert progress.run_id != prepared.run_id
        assert progress.completed == 6
        assert _WINDOW_PROBE_STATE["seen"] == [
            f"row-{index:03d}" for index in range(18, 24)
        ]
        assert (
            RunResultStore(project).pending_run_row_scope_count(
                progress.run_id, [output_col]
            )
            == 0
        )
    finally:
        if guarded_conn:
            project._local.conn = prior_conn  # noqa: SLF001
        project.close()
        _restore_recipe(_WindowProbeRecipe.RECIPE_NAME)


def test_batch_resume_uses_pending_scope_without_full_scope_reload(
    tmp_path: Path, monkeypatch
) -> None:
    _install_batch_probe_recipe()
    project, sheet_id, row_ids = _project(tmp_path, rows=16)
    try:
        prepared = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        ).prepare_run(_batch_spec(sheet_id), confirmed=True)
        output_col = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='batch_out'",
            (sheet_id,),
        ).fetchone()["id"]
        _bind_resume_claim(
            project,
            sheet_id=sheet_id,
            run_id=prepared.run_id,
            output_name="batch_out",
            action_kind=_BatchProbeRecipe.RECIPE_NAME,
        )
        write_claimed_test_results(
            project,
            prepared.run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": output_col,
                    "value": f"done-{index}",
                    "publication_effect": PUBLISH_VALUE,
                }
                for index, row_id in enumerate(row_ids[:10])
            ],
        )

        # The sealed partial generation is recovered by a fresh successor.
        def explode_full_scope(self: RunResultStore, run_id: int) -> list[int]:
            raise AssertionError("batch successor must use pending run_rows")

        monkeypatch.setattr(RunResultStore, "run_row_scope", explode_full_scope)
        successor_spec = {
            **_batch_spec(sheet_id),
            "row_ids": row_ids[10:],
            "overwrite": True,
        }
        progress = asyncio.run(
            run_with_output_claim(
                MapRunner(
                    project,
                    ModelRouter(cache=None, cache_mode="off"),
                    concurrency=3,
                    authority=UnroutedOnlyAuthority(project),
                ),
                successor_spec,
                confirmed=True,
            )
        )

        assert progress.run_id != prepared.run_id
        assert progress.completed == 6
        assert _BATCH_PROBE_STATE["seen"] == [
            f"row-{index:03d}" for index in range(10, 16)
        ]
        assert (
            RunResultStore(project).pending_run_row_scope_count(
                progress.run_id, [output_col]
            )
            == 0
        )
    finally:
        project.close()
        _restore_recipe(_BatchProbeRecipe.RECIPE_NAME)
