"""Empty-output terminal-row contract: a hosted engine returning an EMPTY result over
non-empty source fails the row honestly with the TERMINAL outcome
``empty_output`` — automatic backfill never revisits it, because
whether a retry would fix it is not our judgment call. A deliberate
user-initiated retry creates a fresh generation for exactly the named rows.
Every other hosted-engine code keeps ``outcome="model_error"`` and stays
eligible for automatic backfill.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from frisket.ops.integrations.translate_common import TranslateEngineError
from frisket.ai.llm import ModelRouter
from frisket.ops.base import Recipe
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from runner_test_helpers import run_with_output_claim


@pytest.fixture
def project(tmp_path: Path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


class _FakeHostedRecipe(Recipe):
    """Non-LLM recipe whose per-row behavior is keyed on the source text:
    ``"empty"`` raises the hosted empty_output error, ``"quota"`` raises a
    transient hosted error, anything else succeeds. ``heal=True`` makes every
    row succeed (the second attempt of a retry scenario). ``executed`` logs
    the source text of every row that actually reached execute()."""

    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)

    def __init__(self) -> None:
        super().__init__(name="fake_hosted", llm=False)
        self.heal = False
        self.executed: list[str] = []

    async def execute(self, values: dict, spec: dict, ctx: Any) -> dict:
        text = str(values.get("text") or "")
        self.executed.append(text)
        if not self.heal:
            if text == "empty":
                raise TranslateEngineError(
                    code="empty_output",
                    message="engine returned an empty translation",
                    retryable=False,
                )
            if text == "quota":
                raise TranslateEngineError(
                    code="quota",
                    message="quota exceeded",
                    retryable=True,
                )
        return {"out": f"t:{text}"}


def _install(monkeypatch: pytest.MonkeyPatch) -> _FakeHostedRecipe:
    import frisket.engine.runner.validation as validation_module

    recipe = _FakeHostedRecipe()
    recipe.source_columns = lambda spec: ["text"]  # type: ignore[method-assign]
    recipe.output_fields = lambda spec: [  # type: ignore[method-assign]
        {"name": "out", "column_type": "text"}
    ]
    monkeypatch.setattr(validation_module, "get_recipe", lambda name: recipe)
    return recipe


def _seed(project: Project, texts: list[str]) -> tuple[int, list[int]]:
    sheet = project.add_sheet("data")
    cols = {"text": project.add_column(sheet, "text")}
    project.add_rows(sheet, [{"text": t} for t in texts], cols)
    row_ids = [
        int(r["id"])
        for r in project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position", (sheet,)
        )
    ]
    return sheet, row_ids


def _spec(sheet: int) -> dict[str, Any]:
    return {
        "action_kind": "test.fake_hosted",
        "sheet_id": sheet,
        "input_columns": ["text"],
    }


def _runner(project: Project) -> MapRunner:
    return MapRunner(
        project,
        ModelRouter(keys={}, cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )


def _results(project: Project, run_id: int) -> dict[int, dict[str, Any]]:
    return {
        int(r["row_id"]): dict(r)
        for r in project.db.execute(
            "SELECT row_id, outcome, error, error_code, value "
            "FROM results WHERE run_id=?",
            (run_id,),
        )
    }


def test_empty_output_fails_row_terminally_and_other_codes_stay_model_error(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch)
    sheet, row_ids = _seed(project, ["fine", "empty", "quota"])
    progress = asyncio.run(run_with_output_claim(_runner(project), _spec(sheet)))
    assert progress.failed == 2

    rows = _results(project, progress.run_id)
    ok_row, empty_row, quota_row = row_ids
    assert rows[ok_row]["outcome"] == "ok"
    # empty result over non-empty source: TERMINAL failure — honest error,
    # the empty_output outcome, never a silent green cell.
    assert rows[empty_row]["outcome"] == "empty_output"
    assert rows[empty_row]["error"]
    assert rows[empty_row]["error_code"] == "empty_output"
    assert rows[empty_row]["value"] is None
    # any other hosted code keeps the auto-retryable classification; the
    # error_code still preserves the taxonomy code for diagnosis.
    assert rows[quota_row]["outcome"] == "model_error"
    assert rows[quota_row]["error_code"] == "quota"

    # both count as honest failures on the run.
    run = project.db.execute(
        "SELECT failed_rows FROM runs WHERE id=?", (progress.run_id,)
    ).fetchone()
    assert int(run["failed_rows"]) == 2


def test_fresh_automatic_scope_retries_model_error_but_never_empty_output(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _install(monkeypatch)
    sheet, row_ids = _seed(project, ["fine", "empty", "quota"])
    first = asyncio.run(run_with_output_claim(_runner(project), _spec(sheet)))
    ok_row, empty_row, quota_row = row_ids

    # Automatic recovery selects only the retryable head, then runs that scope
    # as a fresh generation.
    recipe.heal = True
    recipe.executed.clear()
    retry_spec = {**_spec(sheet), "row_ids": [quota_row], "overwrite": True}
    retry = asyncio.run(
        run_with_output_claim(
            _runner(project),
            retry_spec,
        )
    )

    assert recipe.executed == ["quota"]
    assert _results(project, first.run_id)[quota_row]["outcome"] == "model_error"
    heads = ResultGenerationStore(project).read_cell_heads(
        next(c["id"] for c in project.columns(sheet) if c["name"] == "out"), row_ids
    )
    assert heads[quota_row].run_id == retry.run_id
    assert heads[quota_row].outcome == "ok"
    assert heads[empty_row].run_id == first.run_id
    assert heads[empty_row].outcome == "empty_output"
    assert heads[ok_row].run_id == first.run_id


def test_failed_row_states_scope_deliberate_fresh_retry_without_repeating_success(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _install(monkeypatch)
    sheet, row_ids = _seed(project, ["fine", "empty", "quota"])
    runner = _runner(project)
    first = asyncio.run(run_with_output_claim(runner, _spec(sheet)))
    ok_row, empty_row, quota_row = row_ids

    failure_states = runner.run_store.result_row_failure_states(
        first.run_id, set(row_ids)
    )
    retry_row_ids = [row_id for row_id in row_ids if failure_states.get(row_id, False)]
    assert retry_row_ids == [empty_row, quota_row]

    recipe.heal = True
    recipe.executed.clear()
    retry = asyncio.run(
        run_with_output_claim(
            _runner(project),
            {**_spec(sheet), "row_ids": retry_row_ids, "overwrite": True},
        )
    )

    assert recipe.executed == ["empty", "quota"]
    heads = ResultGenerationStore(project).read_cell_heads(
        next(c["id"] for c in project.columns(sheet) if c["name"] == "out"), row_ids
    )
    assert heads[ok_row].run_id == first.run_id
    assert heads[empty_row].run_id == retry.run_id
    assert heads[quota_row].run_id == retry.run_id


def test_deliberate_fresh_retry_reruns_exactly_the_named_rows(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _install(monkeypatch)
    sheet, row_ids = _seed(project, ["fine", "empty", "quota"])
    first = asyncio.run(run_with_output_claim(_runner(project), _spec(sheet)))
    ok_row, empty_row, quota_row = row_ids
    assert _results(project, first.run_id)[empty_row]["outcome"] == "empty_output"

    # A deliberate retry creates a fresh generation for ONLY the named row.
    recipe.heal = True
    recipe.executed.clear()
    retry_spec = _spec(sheet)
    retry_spec["row_ids"] = [empty_row]
    retry_spec["overwrite"] = True
    retry = asyncio.run(
        run_with_output_claim(
            _runner(project),
            retry_spec,
        )
    )

    assert recipe.executed == ["empty"]
    assert _results(project, first.run_id)[empty_row]["outcome"] == "empty_output"
    rows = _results(project, retry.run_id)
    assert rows[empty_row]["outcome"] == "ok"
    assert rows[empty_row]["value"] is not None
    assert rows[empty_row]["error"] is None
    heads = ResultGenerationStore(project).read_cell_heads(
        next(c["id"] for c in project.columns(sheet) if c["name"] == "out"), row_ids
    )
    assert heads[empty_row].run_id == retry.run_id
    assert heads[ok_row].run_id == first.run_id
    assert heads[quota_row].run_id == first.run_id


def test_deliberate_retry_that_fails_again_stays_an_honest_terminal_failure(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "who is to say retry will fix it": a deliberate retry that comes back
    # empty AGAIN re-records the terminal failure — never a silent success,
    # and the failure count does not inflate across attempts.
    recipe = _install(monkeypatch)
    sheet, row_ids = _seed(project, ["fine", "empty"])
    first = asyncio.run(run_with_output_claim(_runner(project), _spec(sheet)))
    _ok_row, empty_row = row_ids

    recipe.executed.clear()
    retry_spec = _spec(sheet)
    retry_spec["row_ids"] = [empty_row]
    retry_spec["overwrite"] = True
    retry = asyncio.run(
        run_with_output_claim(
            _runner(project),
            retry_spec,
        )
    )

    assert recipe.executed == ["empty"]
    assert _results(project, first.run_id)[empty_row]["outcome"] == "empty_output"
    rows = _results(project, retry.run_id)
    assert rows[empty_row]["outcome"] == "empty_output"
    assert rows[empty_row]["error_code"] == "empty_output"
    run = project.db.execute(
        "SELECT failed_rows, completed_rows FROM runs WHERE id=?", (first.run_id,)
    ).fetchone()
    assert int(run["failed_rows"]) == 1
    assert int(run["completed_rows"]) == 2
    retry_run = project.db.execute(
        "SELECT failed_rows, completed_rows FROM runs WHERE id=?", (retry.run_id,)
    ).fetchone()
    assert int(retry_run["failed_rows"]) == 1
    assert int(retry_run["completed_rows"]) == 1
