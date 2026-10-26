"""Formula's managed column publication is all or nothing."""

from __future__ import annotations

import asyncio
import pickle
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
    row_ids: list[int] | None = None,
):
    request = ActionRequest(
        action_id="map.formula",
        scope=SheetRows(sheet_id=sheet_id, row_ids=row_ids),
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
        assert plan.program._spool.closed
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


def test_formula_selected_replacement_preserves_other_manual_cell(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "selected.frisket", name="selected")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(
            sheet, [{"amount": 2}, {"amount": 3}], {"amount": source}
        )
        first = _run(project, sheet, "={{amount}} + 1")
        assert first.status == "completed", first.errors
        column = first.outputs[0].column_id
        project.apply_edits([{"row_id": rows[0], "column_id": column, "value": 99}])
        replacement = _run(
            project,
            sheet,
            "={{amount}} * 2",
            key="selected-replace",
            replace_existing=True,
            row_ids=[rows[1]],
        )
        assert replacement.status == "completed", replacement.errors
        assert project.get_values(sheet, column, row_ids=rows) == {
            rows[0]: 99,
            rows[1]: 6,
        }
    finally:
        project.close()


def test_formula_append_after_staging_does_not_enter_prepared_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action

    project = Project.create(tmp_path / "append.frisket", name="append")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        original = inferred_batch_action.stage_plan

        def append_after_stage(*args, **kwargs):
            staged = original(*args, **kwargs)
            project.add_rows(sheet, [{"amount": 3}], {"amount": source})
            return staged

        monkeypatch.setattr(inferred_batch_action, "stage_plan", append_after_stage)
        result = _run(project, sheet, "={{amount}} + 1")
        assert result.status == "completed", result.errors
        column = result.outputs[0].column_id
        values = project.get_values(sheet, column)
        assert values[rows[0]] == 3
        assert len([value for value in values.values() if value is not None]) == 1
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM runs WHERE status='running'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_formula_queued_target_refusal_keeps_terminal_receipt(tmp_path: Path) -> None:
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "refusal.frisket", name="refusal")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        assert _run(project, sheet, "={{amount}} + 1").status == "completed"
        request = ActionRequest(
            action_id="map.formula",
            scope=SheetRows(sheet_id=sheet),
            params={"formula": {"text": "={{amount}} * 2"}},
            output_names={"result": "computed"},
            idempotency_key="queued-refusal",
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.formula"), request
        )
        envelope = reserve_typed_action_job(project, "p", bound)
        result = run_action_run_job(project, {"action_job": envelope.to_json()})
        assert result.status == "failed"
        assert result.errors[0].code == "output_column_exists"
        stored = ReceiptStore(project).find_by_id(envelope.receipt_id)
        assert stored is not None and stored.status == "failed"
        replay = run_action_run_job(project, {"action_job": envelope.to_json()})
        assert replay.receipt_id == stored.id
    finally:
        project.close()


def test_formula_queued_postrun_exception_closes_attached_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "postrun.frisket", name="postrun")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        request = ActionRequest(
            action_id="map.formula",
            scope=SheetRows(sheet_id=sheet),
            params={"formula": {"text": "={{amount}} + 1"}},
            output_names={"result": "computed"},
            idempotency_key="postrun-failure",
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.formula"), request
        )
        envelope = reserve_typed_action_job(project, "p", bound)

        def fail_publication(*_args, **_kwargs):
            raise RuntimeError("injected publication failure")

        monkeypatch.setattr(inferred_batch_action, "_publish", fail_publication)
        result = run_action_run_job(project, {"action_job": envelope.to_json()})
        assert result.status == "failed", result.errors
        receipt = ReceiptStore(project).find_by_id(envelope.receipt_id).parsed()
        assert receipt.status == "failed" and receipt.run_id is not None
        assert (
            project.db.execute(
                "SELECT status FROM runs WHERE id=?", (receipt.run_id,)
            ).fetchone()[0]
            == "failed"
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM run_output_generations WHERE state='staged'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_formula_cancel_after_prepare_closes_run_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "cancel-late.frisket", name="cancel-late")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        request = ActionRequest(
            action_id="map.formula",
            scope=SheetRows(sheet_id=sheet),
            params={"formula": {"text": "={{amount}} + 1"}},
            output_names={"result": "computed"},
            idempotency_key="cancel-late",
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.formula"), request
        )
        envelope = reserve_typed_action_job(project, "p", bound)
        cancelled = False
        original = inferred_batch_action._publish

        def cancel_before_publish(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return original(*args, **kwargs)

        monkeypatch.setattr(inferred_batch_action, "_publish", cancel_before_publish)
        result = run_action_run_job(
            project, {"action_job": envelope.to_json()}, should_cancel=lambda: cancelled
        )
        assert result.status == "cancelled", result.errors
        receipt = ReceiptStore(project).find_by_id(envelope.receipt_id).parsed()
        assert receipt.status == "cancelled" and receipt.run_id is not None
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE name='computed' AND hidden=0"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_formula_staging_io_error_closes_queued_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "staging-io.frisket", name="staging-io")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        request = ActionRequest(
            action_id="map.formula",
            scope=SheetRows(sheet_id=sheet),
            params={"formula": {"text": "={{amount}} + 1"}},
            output_names={"result": "computed"},
            idempotency_key="staging-io",
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.formula"), request
        )
        envelope = reserve_typed_action_job(project, "p", bound)

        def disk_full(*_args, **_kwargs):
            raise OSError("No space left on device")

        monkeypatch.setattr(inferred_batch_action.pickle, "dump", disk_full)
        result = run_action_run_job(project, {"action_job": envelope.to_json()})
        assert (
            result.status == "failed"
            and result.errors[0].code == "project_write_failed"
        )
        assert ReceiptStore(project).find_by_id(envelope.receipt_id).status == "failed"
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


def test_formula_rand_replay_keeps_exact_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action

    project = Project.create(tmp_path / "rand.frisket", name="rand")
    try:
        sheet = project.add_sheet("source")
        rows = project.add_rows(sheet, [{}, {}], {})
        first = _run(project, sheet, "=RAND()")
        assert first.status == "completed", first.errors
        column = first.outputs[0].column_id
        values = project.get_values(sheet, column, row_ids=rows)

        def rerun_forbidden(*_args, **_kwargs):
            raise AssertionError("replay evaluated Formula again")

        monkeypatch.setattr(inferred_batch_action, "stage_plan", rerun_forbidden)
        replay = _run(project, sheet, "=RAND()")
        assert replay.receipt_id == first.receipt_id
        assert project.get_values(sheet, column, row_ids=rows) == values
    finally:
        project.close()


def test_formula_expired_worker_lease_closes_attached_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action
    from frisket.engine.executor.action_jobs import (
        launch_queued_action_job,
        reconcile_exhausted_action_run_jobs,
    )
    from frisket.engine.jobs.queue import ACTION_RUN_KIND, jobs_table, open_queue
    from frisket.engine.store.receipts import ReceiptStore

    root = tmp_path / "workspace"
    root.mkdir()
    project = Project.create(root / "p.frisket", name="lease")
    queue = open_queue(workspace=root)
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        request = ActionRequest(
            action_id="map.formula",
            scope=SheetRows(sheet_id=sheet),
            params={"formula": {"text": "={{amount}} + 1"}},
            output_names={"result": "computed"},
            idempotency_key="lease-formula",
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.formula"), request
        )
        envelope = reserve_typed_action_job(project, "p", bound)
        launched = launch_queued_action_job(
            envelope=envelope, queue=queue, job_kind=ACTION_RUN_KIND, project=project
        )

        def crash_after_run(*_args, **_kwargs):
            raise KeyboardInterrupt("simulated worker loss")

        monkeypatch.setattr(inferred_batch_action, "_publish", crash_after_run)
        with pytest.raises(KeyboardInterrupt):
            run_action_run_job(
                project,
                {"action_job": envelope.to_json(), "job_id": launched.job_id},
            )
        receipt = ReceiptStore(project).find_by_id(envelope.receipt_id).parsed()
        assert receipt.run_id is not None and receipt.status == "running"
        with queue.engine.begin() as connection:
            connection.execute(
                jobs_table.update()
                .where(jobs_table.c.id == launched.job_id)
                .values(
                    status="failed", error="worker_lease_expired: simulated worker loss"
                )
            )
        project.close()
        assert reconcile_exhausted_action_run_jobs(queue, workspace_root=root) == 1
        reopened = Project(root / "p.frisket")
        try:
            receipt = ReceiptStore(reopened).find_by_id(envelope.receipt_id).parsed()
            assert receipt.status == "failed" and receipt.run_id is not None
            assert (
                reopened.db.execute(
                    "SELECT status FROM runs WHERE id=?", (receipt.run_id,)
                ).fetchone()[0]
                == "failed"
            )
            assert (
                reopened.db.execute(
                    "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
                ).fetchone()[0]
                == 0
            )
            assert (
                reopened.db.execute(
                    "SELECT COUNT(*) FROM run_output_generations WHERE state='staged'"
                ).fetchone()[0]
                == 0
            )
        finally:
            reopened.close()
    finally:
        queue.close()


def test_formula_late_staged_scope_loss_never_publishes_earlier_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action
    from frisket.engine.store.receipts import ReceiptStore

    project = Project.create(tmp_path / "truncated.frisket", name="truncated")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        project.add_rows(sheet, [{"amount": 2}] * 1001, {"amount": source})
        request = ActionRequest(
            action_id="map.formula",
            scope=SheetRows(sheet_id=sheet),
            params={"formula": {"text": "={{amount}} + 1"}},
            output_names={"result": "computed"},
            idempotency_key="truncated-stage",
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("map.formula"), request
        )
        envelope = reserve_typed_action_job(project, "p", bound)
        original = inferred_batch_action.stage_plan

        def truncate_after_stage(*args, **kwargs):
            staged = original(*args, **kwargs)
            spool = args[2]
            for _ in range(1000):
                pickle.load(spool)
            spool.truncate()
            spool.seek(0)
            return staged

        monkeypatch.setattr(inferred_batch_action, "stage_plan", truncate_after_stage)
        result = run_action_run_job(project, {"action_job": envelope.to_json()})
        assert result.status == "failed", result.errors
        receipt = ReceiptStore(project).find_by_id(envelope.receipt_id).parsed()
        assert receipt.status == "failed" and receipt.run_id is not None
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE name='computed' AND hidden=0"
            ).fetchone()[0]
            == 0
        )
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE status='active'"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_formula_invalid_row_scope_keeps_typed_refusal(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "bad-scope.frisket", name="bad-scope")
    try:
        sheet = project.add_sheet("source")
        result = _run(project, sheet, "=1", row_ids=[987654321])
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert result.errors[0].field == "scope.row_ids"
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        project.close()


def test_formula_source_edit_before_publication_refuses_all_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action

    project = Project.create(tmp_path / "stale-source.frisket", name="stale-source")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        original = inferred_batch_action._publish

        def edit_before_publish(*args, **kwargs):
            project.apply_edits([{"row_id": rows[0], "column_id": source, "value": 99}])
            return original(*args, **kwargs)

        monkeypatch.setattr(inferred_batch_action, "_publish", edit_before_publish)
        result = _run(project, sheet, "={{amount}} + 1")
        assert result.status == "failed", result.errors
        assert result.errors[0].code == "stale_input"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM columns WHERE name='computed' AND hidden=0"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def test_formula_whole_scope_retype_refuses_row_appended_after_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action

    project = Project.create(tmp_path / "retype-append.frisket", name="retype-append")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        first = _run(project, sheet, "={{amount}} + 1")
        assert first.status == "completed", first.errors
        column = first.outputs[0].column_id
        original = inferred_batch_action.stage_plan
        appended = []

        def append_after_stage(*args, **kwargs):
            staged = original(*args, **kwargs)
            appended.extend(
                project.add_rows(sheet, [{"amount": 3}], {"amount": source})
            )
            project.apply_edits(
                [{"row_id": appended[0], "column_id": column, "value": 4}]
            )
            return staged

        monkeypatch.setattr(inferred_batch_action, "stage_plan", append_after_stage)
        replacement = _run(
            project,
            sheet,
            '=IF({{computed}} > 0, "positive", "negative")',
            key="retype-after-append",
            replace_existing=True,
        )
        assert replacement.status == "failed", replacement.errors
        assert replacement.errors[0].code == "stale_input"
        assert project.get_column(column)["type"] == "number"
        assert project.get_values(sheet, column, row_ids=rows + appended) == {
            rows[0]: 3,
            appended[0]: 4,
        }
    finally:
        project.close()


def test_formula_whole_scope_retype_refuses_row_appended_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.engine.executor import inferred_batch_action

    project = Project.create(tmp_path / "retype-publish.frisket", name="retype-publish")
    try:
        sheet = project.add_sheet("source")
        source = project.add_column(sheet, "amount", type="number")
        rows = project.add_rows(sheet, [{"amount": 2}], {"amount": source})
        first = _run(project, sheet, "={{amount}} + 1")
        assert first.status == "completed", first.errors
        column = first.outputs[0].column_id
        original = inferred_batch_action._publish
        appended = []

        def append_before_publish(*args, **kwargs):
            appended.extend(
                project.add_rows(sheet, [{"amount": 3}], {"amount": source})
            )
            project.apply_edits(
                [{"row_id": appended[0], "column_id": column, "value": 4}]
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(inferred_batch_action, "_publish", append_before_publish)
        replacement = _run(
            project,
            sheet,
            '=IF({{computed}} > 0, "positive", "negative")',
            key="retype-before-publish",
            replace_existing=True,
        )
        assert replacement.status == "failed", replacement.errors
        assert replacement.errors[0].code == "stale_input"
        assert project.get_column(column)["type"] == "number"
        assert project.get_values(sheet, column, row_ids=rows + appended) == {
            rows[0]: 3,
            appended[0]: 4,
        }
    finally:
        project.close()
