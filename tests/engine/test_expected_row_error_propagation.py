from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    Outcome,
    Row,
    RowError,
    RowResult,
    SheetRows,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.action_families.runs import _resolve_backfill_target
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import (
    EXPECTED_ROW_ERROR,
    FAILURE_OUTCOMES,
    RunResultStore,
)
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.server.run_payloads import (
    _batch_history_op_run_payloads,
    action_run_rows_payload,
)
from frisket.server.services.project_debug import ProjectDebugService


class Params(ActionParams):
    source: ColumnRef[str]


class Output(BaseModel):
    first: Outcome[str]
    second: Outcome[str]


def classify_failure(params: Params, row: Row) -> RowResult[Output]:
    value = params.source.read(row)
    if value == "expected":
        raise RowError("invalid_source", "The source row is invalid")
    if value == "unexpected":
        raise RuntimeError("Unexpected implementation failure")
    if value == "outcome":
        return RowResult(
            output=Output(
                first=Outcome.failed("lookup_failed", "Lookup failed"),
                second=Outcome.failed("lookup_failed", "Lookup failed"),
            )
        )
    return RowResult(output=Output(first=Outcome.ok(value), second=Outcome.ok(value)))


REGISTRY = ActionRegistry(
    [
        ActionNamespace(
            "map",
            actions=[
                action(
                    name="row_error_fact_test",
                    title="Row error fact test",
                    description="Exercise shared row failure policy without capabilities.",
                    category=ActionCategory.CLEANUP,
                    run=map_rows(classify_failure),
                )
            ],
        )
    ]
)


@pytest.fixture
def project(tmp_path):
    store = Project.create(tmp_path / "row-errors.frisket", name="Row errors")
    try:
        yield store
    finally:
        store.close()


def _run(project, values, *, threshold=3):
    sheet = project.add_sheet("rows")
    source = project.add_column(sheet, "source")
    row_ids = project.add_rows(
        sheet, [{"source": value} for value in values], {"source": source}
    )
    request = ActionRequest(
        action_id="map.row_error_fact_test",
        scope=SheetRows(sheet_id=sheet, row_ids=tuple(row_ids)),
        params={"source": "source"},
        output_names={"first": "Diagnostic", "second": "Other diagnostic"},
        idempotency_key="row-errors@1",
    )
    bound = BoundTypedActionRequest.bind(REGISTRY.get(request.action_id), request)

    def runner(store, router):
        return MapRunner(
            store,
            router,
            concurrency=1,
            halt_after_consecutive_failures=threshold,
            authority=UnroutedOnlyAuthority(store),
        )

    result = run_typed_map_rows_action(
        project,
        "project-1",
        bound,
        ModelRouter(cache=None, cache_mode="off", use_env_keys=False),
        runner,
    )
    return sheet, row_ids, bound, result


def test_expected_row_errors_keep_failed_counts_receipt_http_and_retry_facts(project):
    sheet, row_ids, bound, result = _run(project, ["expected"] * 16)
    assert result.status == "failed", result
    assert result.run_id is not None
    store = RunResultStore(project)
    run = store.get_run(result.run_id)
    assert run["completed_rows"] == run["failed_rows"] == run["total_rows"] == 16
    assert run["halted_reason"] is None
    assert EXPECTED_ROW_ERROR in FAILURE_OUTCOMES
    assert store.result_row_failure_states(result.run_id, set(row_ids)) == {
        row_id: True for row_id in row_ids
    }
    columns = project.columns(sheet)
    assert [column["name"] for column in columns] == [
        "source",
        "Diagnostic",
        "Other diagnostic",
    ]
    cells = project.db.execute(
        "SELECT outcome, error_code, error, publication_effect FROM results WHERE run_id=?",
        (result.run_id,),
    ).fetchall()
    assert len(cells) == 32
    assert all(
        cell["outcome"] == EXPECTED_ROW_ERROR
        and cell["error_code"] == "invalid_source"
        and cell["error"] == "The source row is invalid"
        and cell["publication_effect"] == "publish_error"
        for cell in cells
    )
    # Automatic backfill classifies the exact published heads by this same
    # taxonomy; expected failures must not be mistaken for successful heads.
    heads = ResultGenerationStore(project).read_cell_heads(columns[1]["id"])
    assert [
        row_id for row_id in row_ids if heads[row_id].outcome in FAILURE_OUTCOMES
    ] == row_ids
    retry = _resolve_backfill_target(project, sheet_id=sheet, column="Diagnostic")
    assert isinstance(retry, dict), retry
    assert retry["unrun_row_ids"] == row_ids
    assert retry["runner_spec"]["row_ids"] == row_ids

    stored = ReceiptStore(project).find_by_idempotency_key(
        bound.request.idempotency_key
    )
    receipt = stored.parsed()
    counts = next(
        item.ref
        for item in receipt.evidence
        if item.ref["kind"] == "map_rows_run_counts"
    )
    assert counts["failed_row_ids"] == row_ids
    assert counts["completed_rows"] == counts["failed_rows"] == 16
    assert len(receipt.outputs) == 2

    page = action_run_rows_payload(project, result.run_id, status="error")
    assert page["total"] == 16
    assert [row["row_id"] for row in page["rows"]] == row_ids
    assert all(row["status"] == "error" for row in page["rows"])
    payload = _batch_history_op_run_payloads(project, [run["op_id"]])[run["op_id"]]
    assert payload["row_errors"]["total_failed_rows"] == 16
    assert payload["row_errors"]["groups"] == [
        {
            "message": "The source row is invalid",
            "count": 16,
            "code": "invalid_source",
            "outcome": EXPECTED_ROW_ERROR,
            "terminal": False,
            "row_ids": row_ids[:5],
        }
    ]
    workspace = SimpleNamespace(
        get=lambda _project_id: project, active_runs={}, _router=None
    )
    debug = ProjectDebugService(workspace).debug("project-1")
    assert debug["results"][0]["errors"] == 32

    def forbidden_runner(*_args):
        raise AssertionError("replay must not dispatch rows")

    replay = run_typed_map_rows_action(
        project, "project-1", bound, None, forbidden_runner
    )
    assert replay.status == result.status
    assert replay.receipt_id == result.receipt_id


def test_expected_row_error_resets_the_unexpected_source_order_streak(project):
    values = ["unexpected", "unexpected", "expected"] * 4 + ["ok"]
    _sheet, row_ids, _bound, result = _run(project, values)
    assert result.status == "partial", result
    run = RunResultStore(project).get_run(result.run_id)
    assert run["completed_rows"] == len(row_ids)
    assert run["failed_rows"] == len(row_ids) - 1
    assert run["halted_reason"] is None


@pytest.mark.parametrize("failure", ["unexpected", "outcome"])
def test_unexpected_exceptions_and_failed_outcomes_still_trip_the_breaker(
    project, failure
):
    _sheet, row_ids, _bound, result = _run(project, [failure] * 9)
    assert result.status == "cancelled", result
    run = RunResultStore(project).get_run(result.run_id)
    assert 3 <= run["failed_rows"] == run["completed_rows"] < len(row_ids)
    assert "3 consecutive" in run["halted_reason"]


@pytest.mark.parametrize("failure", ["unexpected", "outcome"])
def test_unexpected_zero_success_columns_still_hide(project, failure):
    sheet, _row_ids, _bound, result = _run(project, [failure] * 3, threshold=None)
    assert result.status == "failed", result
    assert [column["name"] for column in project.columns(sheet)] == ["source"]
