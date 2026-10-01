from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

import pytest

from frisket.server.route_errors import RouteError
from frisket.server.services.action_runs import ActionRunResponse
from frisket.server.services.project_qa_child_runs import (
    ChildRunDrainTimeout,
    ChildRunDispatch,
    ProjectQAChildRunService,
)


@dataclass
class _Project:
    db: Any


class _Db:
    def __init__(self, cost: float | None = None):
        self.cost = cost

    def execute(self, _sql: str, _params: tuple[int]) -> "_Db":
        return self

    def fetchone(self) -> dict[str, float | None]:
        return {"cost_actual": self.cost}


class _ActionRuns:
    def __init__(self, jobs: list[dict[str, Any]], receipt: dict[str, Any]):
        self.jobs = list(jobs)
        self.receipt = receipt
        self.cancelled_jobs: list[tuple[str, int]] = []

    def receipt_lookup(self, _project_id: str, _receipt_id: str) -> dict[str, Any]:
        return self.receipt

    def job_detail(self, _project_id: str, _job_id: int) -> dict[str, Any]:
        return self.jobs.pop(0)

    def cancel_job(self, project_id: str, job_id: int) -> dict[str, Any]:
        self.cancelled_jobs.append((project_id, job_id))
        return {"status": "cancelled"}


class _RunCancel:
    def __init__(self, *, conflict: bool = False):
        self.calls: list[tuple[str, int]] = []
        self.conflict = conflict

    def cancel_run(self, project_id: str, run_id: int) -> dict[str, Any]:
        self.calls.append((project_id, run_id))
        if self.conflict:
            raise RouteError(409, {"status": "cancel_pending"})
        return {"status": "cancelled"}


def _response(*, run_id: int | None = 7) -> ActionRunResponse:
    return ActionRunResponse(
        status_code=200,
        payload={
            "schema_version": "frisket.action_result.v1",
            "action": {"kind": "map.translate", "action_id": "act_1"},
            "status": "queued",
            "project_id": "project-1",
            "run_id": run_id,
            "job_id": 9,
            "op_ids": [31],
            "outputs": [],
            "receipt_id": "rcpt_1",
            "warnings": [],
            "errors": [],
        },
    )


def _receipt(*, cost: float | None = 0.125) -> dict[str, Any]:
    return {
        "schema_version": "frisket.receipt.v1",
        "receipt_id": "rcpt_1",
        "project_id": "project-1",
        "action_id": "act_1",
        "action_kind": "map.translate",
        "run_id": 7,
        "op_ids": [31],
        "idempotency_key": "research:turn:operation",
        "status": "completed",
        "inputs": [],
        "outputs": [
            {
                "name": "Translation",
                "kind": "column",
                "ref": {"kind": "column", "sheet_id": 2, "column_id": 8},
            }
        ],
        "provider_use": [{"provider": "openai", "cost_actual": cost}],
        "evidence": [],
        "exports": [],
        "warnings": [],
        "errors": [],
    }


def test_records_response_dispatch_before_receipt_enrichment_and_returns_terminal_receipt() -> None:
    events: list[str] = []
    recorded: list[ChildRunDispatch] = []
    action_runs = _ActionRuns(
        [
            {"job_id": 9, "status": "running", "receipt_id": "rcpt_1"},
            {"job_id": 9, "status": "done", "receipt_id": "rcpt_1"},
        ],
        _receipt(),
    )

    def record(dispatch: ChildRunDispatch) -> None:
        events.append("record")
        recorded.append(dispatch)
        assert (dispatch.job_id, dispatch.run_id, dispatch.receipt_id) == (
            9,
            7,
            "rcpt_1",
        )

    async def sleep(_delay: float) -> None:
        events.append("sleep")

    service = ProjectQAChildRunService(
        object(),
        action_runs=action_runs,  # type: ignore[arg-type]
        action_run_cancel=_RunCancel(),  # type: ignore[arg-type]
        sleep=sleep,
    )
    outcome = asyncio.run(
        service.wait(
            project=_Project(_Db()),  # type: ignore[arg-type]
            project_id="project-1",
            response=_response(),
            record_dispatch=record,
            rate_actual_cost=lambda usd: round(usd * 1_000_000),
        )
    )

    assert events == ["record", "record", "sleep"]
    assert [dispatch.idempotency_key for dispatch in recorded] == [
        None,
        "research:turn:operation",
    ]
    assert outcome.status == "completed"
    assert outcome.outputs == (
        {
            "name": "Translation",
            "kind": "column",
            "ref": {"kind": "column", "sheet_id": 2, "column_id": 8},
        },
    )
    assert outcome.provider_cost_usd == 0.125
    assert outcome.billed_cost_micros == 125_000


def test_stop_cancels_run_once_and_drains_cancel_pending() -> None:
    action_runs = _ActionRuns(
        [
            {"job_id": 9, "status": "running", "receipt_id": "rcpt_1"},
            {"job_id": 9, "status": "cancelled", "receipt_id": "rcpt_1"},
        ],
        {**_receipt(cost=0.02), "status": "cancelled"},
    )
    cancel = _RunCancel(conflict=True)
    service = ProjectQAChildRunService(
        object(),
        action_runs=action_runs,  # type: ignore[arg-type]
        action_run_cancel=cancel,  # type: ignore[arg-type]
        sleep=lambda _delay: asyncio.sleep(0),
    )
    outcome = asyncio.run(
        service.wait(
            project=_Project(_Db()),  # type: ignore[arg-type]
            project_id="project-1",
            response=_response(),
            record_dispatch=lambda _dispatch: None,
            stop_requested=lambda: True,
        )
    )

    assert cancel.calls == [("project-1", 7)]
    assert action_runs.cancelled_jobs == []
    assert outcome.status == "cancelled"
    assert outcome.provider_cost_usd == 0.02


def test_runless_stop_uses_job_cancel_and_unknown_cost_stays_unknown() -> None:
    receipt = {**_receipt(cost=None), "run_id": None, "status": "cancelled"}
    action_runs = _ActionRuns(
        [{"job_id": 9, "status": "cancelled", "receipt_id": "rcpt_1"}],
        receipt,
    )
    cancel = _RunCancel()
    rated: list[float] = []
    service = ProjectQAChildRunService(
        object(),
        action_runs=action_runs,  # type: ignore[arg-type]
        action_run_cancel=cancel,  # type: ignore[arg-type]
    )
    outcome = asyncio.run(
        service.wait(
            project=_Project(_Db()),  # type: ignore[arg-type]
            project_id="project-1",
            response=_response(run_id=None),
            record_dispatch=lambda _dispatch: None,
            stop_requested=lambda: True,
            rate_actual_cost=lambda cost: rated.append(cost) or 0,
        )
    )

    assert action_runs.cancelled_jobs == [("project-1", 9)]
    assert cancel.calls == []
    assert outcome.provider_cost_usd is None
    assert outcome.billed_cost_micros is None
    assert rated == []


def test_task_cancellation_cancels_and_bounds_child_drain() -> None:
    class _NeverTerminal(_ActionRuns):
        def job_detail(self, _project_id: str, _job_id: int) -> dict[str, Any]:
            return {"job_id": 9, "status": "running", "receipt_id": "rcpt_1"}

    async def scenario() -> tuple[list[tuple[str, int]], int]:
        sleeps = 0

        async def sleep(_delay: float) -> None:
            nonlocal sleeps
            sleeps += 1
            await asyncio.sleep(0)

        action_runs = _NeverTerminal([], _receipt())
        cancel = _RunCancel()
        service = ProjectQAChildRunService(
            object(),
            action_runs=action_runs,  # type: ignore[arg-type]
            action_run_cancel=cancel,  # type: ignore[arg-type]
            cancellation_drain_timeout_seconds=0.001,
            sleep=sleep,
        )
        task = asyncio.create_task(
            service.wait(
                project=_Project(_Db()),  # type: ignore[arg-type]
                project_id="project-1",
                response=_response(),
                record_dispatch=lambda _dispatch: None,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:  # pragma: no cover - assertion message is clearer than raises check
            raise AssertionError("child waiter swallowed task cancellation")
        return cancel.calls, sleeps

    calls, sleeps = asyncio.run(scenario())
    assert calls == [("project-1", 7)]
    assert sleeps > 0


def test_stop_timeout_preserves_dispatch_and_raises_typed_unresolved_child() -> None:
    class _NeverTerminal(_ActionRuns):
        def job_detail(self, _project_id: str, _job_id: int) -> dict[str, Any]:
            return {"job_id": 9, "status": "running", "receipt_id": "rcpt_1"}

    async def sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    records: list[ChildRunDispatch] = []
    cancel = _RunCancel(conflict=True)
    service = ProjectQAChildRunService(
        object(),
        action_runs=_NeverTerminal([], _receipt()),  # type: ignore[arg-type]
        action_run_cancel=cancel,  # type: ignore[arg-type]
        cancellation_drain_timeout_seconds=0.001,
        sleep=sleep,
    )

    with pytest.raises(ChildRunDrainTimeout) as raised:
        asyncio.run(
            service.wait(
                project=_Project(_Db()),  # type: ignore[arg-type]
                project_id="project-1",
                response=_response(),
                record_dispatch=records.append,
                stop_requested=lambda: True,
            )
        )

    assert cancel.calls == [("project-1", 7)]
    assert records[0].idempotency_key is None
    assert raised.value.dispatch == records[-1]


def test_terminal_receipt_without_provider_operations_costs_zero() -> None:
    receipt = {**_receipt(), "provider_use": []}
    action_runs = _ActionRuns([], receipt)
    response = ActionRunResponse(
        status_code=200,
        payload={
            **_response().payload,
            "status": "completed",
            "job_id": None,
        },
    )
    service = ProjectQAChildRunService(
        object(),
        action_runs=action_runs,  # type: ignore[arg-type]
        action_run_cancel=_RunCancel(),  # type: ignore[arg-type]
    )
    outcome = asyncio.run(
        service.wait(
            project=_Project(_Db()),  # type: ignore[arg-type]
            project_id="project-1",
            response=response,
            record_dispatch=lambda _dispatch: None,
            rate_actual_cost=lambda usd: round(usd * 1_000_000),
        )
    )
    assert outcome.provider_cost_usd == 0.0
    assert outcome.billed_cost_micros == 0


def test_child_store_calls_run_off_the_event_loop_thread() -> None:
    calls: list[tuple[str, int]] = []

    class TrackingActionRuns(_ActionRuns):
        def receipt_lookup(self, project_id: str, receipt_id: str) -> dict[str, Any]:
            calls.append(("receipt_lookup", threading.get_ident()))
            return super().receipt_lookup(project_id, receipt_id)

        def job_detail(self, project_id: str, job_id: int) -> dict[str, Any]:
            calls.append(("job_detail", threading.get_ident()))
            return super().job_detail(project_id, job_id)

    class TrackingRunCancel(_RunCancel):
        def cancel_run(self, project_id: str, run_id: int) -> dict[str, Any]:
            calls.append(("cancel_run", threading.get_ident()))
            return super().cancel_run(project_id, run_id)

    async def scenario() -> int:
        loop_thread = threading.get_ident()
        service = ProjectQAChildRunService(
            object(),
            action_runs=TrackingActionRuns(
                [{"job_id": 9, "status": "cancelled", "receipt_id": "rcpt_1"}],
                {**_receipt(), "status": "cancelled"},
            ),  # type: ignore[arg-type]
            action_run_cancel=TrackingRunCancel(),  # type: ignore[arg-type]
        )
        await service.wait(
            project=_Project(_Db()),  # type: ignore[arg-type]
            project_id="project-1",
            response=_response(),
            record_dispatch=lambda _dispatch: calls.append(
                ("record_dispatch", threading.get_ident())
            ),
            stop_requested=lambda: True,
            rate_actual_cost=lambda _cost: (
                calls.append(("rate_actual_cost", threading.get_ident())) or 0
            ),
        )
        return loop_thread

    loop_thread = asyncio.run(scenario())
    assert [name for name, _thread in calls] == [
        "record_dispatch",
        "receipt_lookup",
        "record_dispatch",
        "cancel_run",
        "job_detail",
        "receipt_lookup",
        "rate_actual_cost",
    ]
    assert all(thread != loop_thread for _name, thread in calls)
