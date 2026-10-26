"""Formula's managed column publication is all or nothing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from frisket.engine.executor.actions import resolve_map_preview
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.executor.action_jobs import (
    reserve_typed_action_job,
    run_action_run_job,
)
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


def _runner(project: Project, router: object) -> MapRunner:
    return MapRunner(
        project,
        router or ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    )


def _run(
    project: Project,
    sheet_id: int,
    formula: str,
    *,
    key: str = "formula-1",
    replace_existing: bool = False,
):
    request = ActionRequest(
        action_id="map.formula",
        scope=SheetRows(sheet_id=sheet_id),
        params={"formula": {"text": formula}},
        output_names={"result": "computed"},
        idempotency_key=key,
        replace_existing=replace_existing,
    )
    return run_typed_map_rows_action(
        project,
        "p",
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get("map.formula"), request),
        None,
        _runner,
    )


def test_formula_publishes_typed_number_once(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "formula.frisket", name="formula")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(
            sheet, [{"amount": 2}, {"amount": 3}], {"amount": source}
        )
        first = _run(project, sheet, "={{amount}} * 2")
        assert first.status == "completed", first.errors
        assert len(first.outputs) == 1
        column = first.outputs[0].column_id
        assert project.get_column(column)["type"] == "number"
        assert project.get_values(sheet, column, row_ids=rows) == {
            rows[0]: 4,
            rows[1]: 6,
        }
        replay = _run(project, sheet, "={{amount}} * 2")
        assert replay.receipt_id == first.receipt_id
    finally:
        project.close()


def test_formula_mixed_later_batch_never_publishes(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "mixed.frisket", name="mixed")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(
            sheet, [{"amount": 1}] * 1000 + [{"amount": -1}], {"amount": source}
        )
        result = _run(project, sheet, '=IF({{amount}} > 0, 1, "negative")')
        assert result.status == "failed", result.errors
        assert "mixed types" in result.errors[0].message
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE name='computed'"
            ).fetchone()[0]
            == 0
        )
        # A direct prepublication refusal leaves no running idempotency reservation.
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE idempotency_key='formula-1'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_formula_queued_job_publishes_and_replays(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "queued.frisket", name="queued")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(
            sheet, [{"amount": 2}, {"amount": 3}], {"amount": source}
        )
        request = ActionRequest(
            action_id="map.formula",
            scope=SheetRows(sheet_id=sheet),
            params={"formula": {"text": "={{amount}} + 1"}},
            output_names={"result": "computed"},
            idempotency_key="queued-formula",
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.formula"), request
        )
        envelope = reserve_typed_action_job(project, "p", bound)
        assert envelope.receipt_id
        result = run_action_run_job(project, {"action_job": envelope.to_json()})
        assert result.status == "completed", result.errors
        column = result.outputs[0].column_id
        assert project.get_values(sheet, column, row_ids=rows) == {
            rows[0]: 3,
            rows[1]: 4,
        }
        replay = run_action_run_job(project, {"action_job": envelope.to_json()})
        assert replay.receipt_id == result.receipt_id
    finally:
        project.close()


def test_formula_cancel_before_publication_has_no_output(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "cancel.frisket", name="cancel")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        request = ActionRequest(
            action_id="map.formula",
            scope=SheetRows(sheet_id=sheet),
            params={"formula": {"text": "={{amount}} + 1"}},
            output_names={"result": "computed"},
            idempotency_key="cancel-formula",
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.formula"), request
        )
        envelope = reserve_typed_action_job(project, "p", bound)
        result = run_action_run_job(
            project, {"action_job": envelope.to_json()}, should_cancel=lambda: True
        )
        assert result.status == "cancelled", result.errors
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE name='computed'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_formula_preview_uses_inferred_type_without_writing(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "preview.frisket", name="preview")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(
            sheet, [{"amount": 2}, {"amount": 3}], {"amount": source}
        )
        columns_before = project.db.execute("SELECT COUNT(*) FROM columns").fetchone()[
            0
        ]
        plan = resolve_map_preview(
            project,
            {
                "action_id": "map.formula",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": rows},
                "params": {"formula": {"text": "={{amount}} + 1"}},
                "output_names": {"result": "computed"},
                "idempotency_key": "preview-formula",
            },
        )
        assert not hasattr(plan, "code"), plan
        result = asyncio.run(
            plan.make_runner().preview(plan.runner_spec, program=plan.program)
        )
        assert result.values[rows[0]]["computed"]["value"] == 3
        assert result.values[rows[1]]["computed"]["value"] == 4
        assert (
            project.db.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
            == columns_before
        )
    finally:
        project.close()


def test_formula_date_source_is_evaluated_as_a_date(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "dates.frisket", name="dates")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "filed", type="date")
        rows = project.add_rows(sheet, [{"filed": "2026-10-26"}], {"filed": source})
        result = _run(project, sheet, "=YEAR({{filed}})")
        assert result.status == "completed", result.errors
        column = result.outputs[0].column_id
        assert project.get_column(column)["type"] == "number"
        assert project.get_values(sheet, column, row_ids=rows) == {rows[0]: 2026}
    finally:
        project.close()


def test_formula_all_errors_refuses_output(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "errors.frisket", name="errors")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(sheet, [{"amount": 1}], {"amount": source})
        result = _run(project, sheet, "={{amount}} / 0")
        assert result.status == "failed", result.errors
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE name='computed'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_formula_replaces_its_own_generated_source_and_undoes(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "replace.frisket", name="replace")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        initial = _run(project, sheet, "={{amount}} + 1")
        assert initial.status == "completed", initial.errors
        column = initial.outputs[0].column_id
        replacement = _run(
            project, sheet, "={{computed}} + 1", key="formula-2", replace_existing=True
        )
        assert replacement.status == "completed", replacement.errors
        assert replacement.outputs[0].column_id == column
        assert project.get_values(sheet, column, row_ids=rows) == {rows[0]: 4}
        project.undo()
        assert project.get_values(sheet, column, row_ids=rows) == {rows[0]: 3}
    finally:
        project.close()


def test_formula_replaces_generated_column_with_new_type(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "replace-type.frisket", name="replace-type")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        initial = _run(project, sheet, "={{amount}} + 1")
        assert initial.status == "completed", initial.errors
        column = initial.outputs[0].column_id
        replacement = _run(
            project,
            sheet,
            '=IF({{computed}} > 0, "positive", "negative")',
            key="formula-text",
            replace_existing=True,
        )
        assert replacement.status == "completed", replacement.errors
        assert project.get_column(column)["type"] == "text"
        assert project.get_values(sheet, column, row_ids=rows) == {rows[0]: "positive"}
        project.undo()
        assert project.get_column(column)["type"] == "number"
        assert project.get_values(sheet, column, row_ids=rows) == {rows[0]: 3}
    finally:
        project.close()


@pytest.mark.parametrize(
    ("formula", "column_type", "expected"),
    [
        ("=RAND()", "number", None),
        ("=NOW()", "datetime", None),
        ('=""', "text", None),
        ("=TRUE()", "boolean", True),
    ],
)
def test_formula_without_column_references_infers_scalar_type(
    tmp_path: Path, formula: str, column_type: str, expected: object
) -> None:
    project = Project.create(tmp_path / "constant.frisket", name="constant")
    try:
        sheet = project.add_sheet("source")
        rows = project.add_rows(sheet, [{}], {})
        result = _run(project, sheet, formula)
        assert result.status == "completed", result.errors
        column = result.outputs[0].column_id
        assert project.get_column(column)["type"] == column_type
        value = project.get_values(sheet, column, row_ids=rows)[rows[0]]
        if expected is not None:
            assert value == expected
        elif formula == "=RAND()":
            assert 0 <= value < 1
        elif formula == "=NOW()":
            assert isinstance(value, str) and value.startswith("20") and "T" in value
        else:
            assert value is None
    finally:
        project.close()
