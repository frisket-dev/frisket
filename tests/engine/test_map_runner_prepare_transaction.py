"""STORE-01's fixed matrix for fresh mode-B preparation.

Case 1 remains covered by
test_extract_metadata_atomic_output_family_semantics_remain_non_routed:
the existing mode-A atomic-family regression. Case 5 is N/A in the current
registry because MapRunner._prepare routes every fresh consumes_resolution
recipe through mode A, regardless of its atomic_output_columns value. The
tests here cover cases 2, 3, 4, and 6, plus the card's explicit accidental-
nesting guard.

The orphan regression (case 3) is intentionally checked through a second
SQLite connection. Before STORE-01, add_column committed independently, so
that connection observed agency_clean after the injected append_op failure
even though no op or run existed. The assertion expecting no durable state
therefore failed before the production cutover and passes only when the whole
preparation tuple rolls back.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from frisket.ai.llm import ModelRouter
from frisket.ops.base import Recipe
from frisket.engine.runner import MapRunner
from frisket.engine.runner.validation import _ValidatedSpec
from frisket.engine.store import Project
from frisket.engine.store.result_generations import GenerationDeclarationConflict
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


@dataclass
class _ReuseRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"

    name: str = "test.store01_reuse"
    llm: bool = False

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": "result",
                "column_type": "text",
                "schema": {"type": "string"},
                "description": "",
            }
        ]


@dataclass
class _FreshPrepareRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"

    name: str = "test.store01_fresh_prepare"
    llm: bool = False

    def source_columns(self, spec: dict[str, Any]) -> list[str]:
        del spec
        return ["agency"]

    def output_fields(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        del spec
        return [
            {
                "name": "agency_clean",
                "column_type": "text",
                "schema": {"type": "string"},
                "description": "",
            }
        ]


@pytest.fixture
def project(tmp_path: Path):
    value = Project.create(tmp_path / "store01.frisket", name="store01")
    yield value
    value.close()


def _seed(project: Project) -> tuple[int, list[int]]:
    sheet_id = project.add_sheet("data")
    source_id = project.add_column(sheet_id, "agency")
    row_ids = project.add_rows(
        sheet_id,
        [{"agency": "  CITY HALL  "}],
        {"agency": source_id},
    )
    return sheet_id, row_ids


def _runner(project: Project) -> MapRunner:
    return MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )


def _clean_spec(sheet_id: int) -> dict[str, Any]:
    return {
        "action_kind": "test.store01_fresh_prepare",
        "sheet_id": sheet_id,
        "input_columns": ["agency"],
    }


def _durable_state(project: Project, *, output_name: str) -> dict[str, int]:
    with sqlite3.connect(project.db_path) as db:
        return {
            "output_columns": int(
                db.execute(
                    "SELECT COUNT(*) FROM columns WHERE name=?", (output_name,)
                ).fetchone()[0]
            ),
            "ops": int(db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]),
            "runs": int(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]),
        }


def test_fresh_mode_b_prepare_commits_column_op_run_and_pointer(project: Project):
    sheet_id, _ = _seed(project)
    before = _durable_state(project, output_name="agency_clean")

    prepared = _runner(project)._prepare(
        _clean_spec(sheet_id),
        program=_FreshPrepareRecipe(),
        confirmed=False,
        resume_run_id=None,
    )

    output_id = prepared.out_cols["agency_clean"]
    assert project.get_column(output_id)["current_run_id"] == prepared.run_id
    assert (
        project.db.execute(
            "SELECT op_id FROM runs WHERE id=?", (prepared.run_id,)
        ).fetchone()["op_id"]
        == prepared.op_id
    )
    assert _durable_state(project, output_name="agency_clean") == {
        "output_columns": before["output_columns"] + 1,
        "ops": before["ops"] + 1,
        "runs": before["runs"] + 1,
    }
    assert project.db.in_transaction is False


def test_append_op_failure_rolls_back_new_output_column(
    project: Project, monkeypatch: pytest.MonkeyPatch
):
    """Pre-fix red: a second connection saw 1 durable orphan output column."""

    sheet_id, _ = _seed(project)
    before = _durable_state(project, output_name="agency_clean")

    def fail_before_append(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("injected append_op failure")

    monkeypatch.setattr(Project, "append_op", fail_before_append)

    with pytest.raises(RuntimeError, match="injected append_op failure"):
        _runner(project)._prepare(
            _clean_spec(sheet_id),
            program=_FreshPrepareRecipe(),
            confirmed=False,
            resume_run_id=None,
        )

    assert _durable_state(project, output_name="agency_clean") == before
    assert project.db.in_transaction is False


def test_pointer_failure_rolls_back_column_op_and_started_run(
    project: Project, monkeypatch: pytest.MonkeyPatch
):
    sheet_id, _ = _seed(project)
    before = _durable_state(project, output_name="agency_clean")
    observed_started_run = False

    def fail_after_start_run(
        store: RunResultStore,
        _op_id: int,
        _column_id: int,
        run_id: int,
        *,
        commit: bool = True,
    ) -> None:
        nonlocal observed_started_run
        observed_started_run = store.get_run(run_id) is not None
        raise RuntimeError("injected point_column_at_run failure")

    monkeypatch.setattr(RunResultStore, "point_column_at_run", fail_after_start_run)

    with pytest.raises(RuntimeError, match="injected point_column_at_run failure"):
        _runner(project)._prepare(
            _clean_spec(sheet_id),
            program=_FreshPrepareRecipe(),
            confirmed=False,
            resume_run_id=None,
        )

    assert observed_started_run is True
    assert _durable_state(project, output_name="agency_clean") == before
    assert project.db.in_transaction is False


def test_reused_ai_column_converges_inside_mode_b_transaction(project: Project):
    sheet_id, row_ids = _seed(project)
    output_id = project.add_column(
        sheet_id,
        "result",
        type="number",
        ai_generated=True,
        format="json",
        semantic_type="entity_mentions",
    )
    recipe = _ReuseRecipe()
    spec = {"action_kind": recipe.name, "sheet_id": sheet_id}
    columns = project.columns(sheet_id)
    validated = _ValidatedSpec(
        recipe=recipe,
        sheet_id=sheet_id,
        columns=columns,
        col_map={str(column["name"]): int(column["id"]) for column in columns},
        row_ids=row_ids,
        output_fields=recipe.output_fields(spec),
        est=None,
        has_stored_run_scope=False,
        stored_run_scope_count=None,
        explicit_resume_scope=None,
    )

    before = tuple(project.get_column(output_id))
    durable_before = _durable_state(project, output_name="result")
    with pytest.raises(GenerationDeclarationConflict, match="no generation history"):
        _runner(project)._prepare(
            spec,
            confirmed=False,
            resume_run_id=None,
            _validated=validated,
        )

    reused = project.get_column(output_id)
    assert tuple(reused) == before
    assert (reused["type"], reused["format"], reused["semantic_type"]) == (
        "number",
        "json",
        "entity_mentions",
    )
    assert reused["current_run_id"] is None
    assert (
        project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        == durable_before["ops"]
    )
    assert (
        project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        == durable_before["runs"]
    )
    assert (
        project.db.execute("SELECT COUNT(*) FROM run_output_generations").fetchone()[0]
        == 0
    )
    assert project.db.in_transaction is False


def test_fresh_mode_b_prepare_refuses_an_existing_caller_transaction(
    project: Project,
):
    sheet_id, _ = _seed(project)
    before = _durable_state(project, output_name="agency_clean")
    project.db.execute("BEGIN IMMEDIATE")

    with pytest.raises(
        RuntimeError,
        match="mode-B prepare requires no existing project transaction",
    ):
        _runner(project)._prepare(
            _clean_spec(sheet_id),
            program=_FreshPrepareRecipe(),
            confirmed=False,
            resume_run_id=None,
        )

    assert project.db.in_transaction is True
    project.db.rollback()
    assert _durable_state(project, output_name="agency_clean") == before
