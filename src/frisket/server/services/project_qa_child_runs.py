"""Wait for Project Ask child actions through the ordinary action services.

This adapter deliberately owns no execution or queue state.  It records the
identifiers returned by :class:`ActionRunService` before yielding control,
waits on that service's public job projection, and uses the canonical receipt
as the terminal result.  The research ledger remains the owner of admission,
commitments, and settlement.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from frisket.ai.models.metadata import provider_cost_total, provider_cost_value
from frisket.contracts.action import ActionResult as V1ActionResult
from frisket.engine.store import Project
from frisket.server.route_errors import RouteError
from frisket.server.services.action_run_cancel import ActionRunCancelService
from frisket.server.services.action_runs import ActionRunResponse, ActionRunService
from frisket.server.thread_worker import await_thread_worker
from frisket.server.workspace import Workspace


_TERMINAL_ACTION_STATUSES = frozenset(
    {"needs_confirmation", "completed", "partial", "failed", "cancelled"}
)
_TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled"})


@dataclass(frozen=True)
class ChildRunDispatch:
    """Stable correlation facts persisted by the research operation ledger."""

    project_id: str
    action_kind: str
    action_id: str
    idempotency_key: str | None
    job_id: int | None
    run_id: int | None
    receipt_id: str | None
    op_ids: tuple[int, ...]


@dataclass(frozen=True)
class ChildRunOutcome:
    """Bounded terminal projection returned to the research host."""

    dispatch: ChildRunDispatch
    status: str
    outputs: tuple[dict[str, Any], ...]
    errors: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    provider_cost_usd: float | None
    billed_cost_micros: int | None
    public_job_status: dict[str, Any] | None


class ChildRunDrainTimeout(TimeoutError):
    """A Stop request did not reach a public terminal child state in time."""

    def __init__(self, dispatch: ChildRunDispatch) -> None:
        self.dispatch = dispatch
        super().__init__(
            "child action did not reach a terminal state after its Stop request"
        )


class RecordChildDispatch(Protocol):
    def __call__(self, dispatch: ChildRunDispatch, /) -> None: ...


class RateActualCost(Protocol):
    def __call__(self, provider_cost_usd: float, /) -> int | None: ...


Sleep = Callable[[float], Awaitable[None]]
StopRequested = Callable[[], bool | Awaitable[bool]]


class ProjectQAChildRunService:
    """Host-owned waiter and Stop bridge for one already-launched child action."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        poll_interval_seconds: float = 0.25,
        cancellation_drain_timeout_seconds: float = 5.0,
        sleep: Sleep = asyncio.sleep,
        action_runs: ActionRunService | None = None,
        action_run_cancel: ActionRunCancelService | None = None,
    ) -> None:
        if poll_interval_seconds <= 0 or not math.isfinite(poll_interval_seconds):
            raise ValueError("poll_interval_seconds must be finite and positive")
        if cancellation_drain_timeout_seconds <= 0 or not math.isfinite(
            cancellation_drain_timeout_seconds
        ):
            raise ValueError(
                "cancellation_drain_timeout_seconds must be finite and positive"
            )
        self._action_runs = action_runs or ActionRunService(workspace)
        self._action_run_cancel = action_run_cancel or ActionRunCancelService(workspace)
        self._poll_interval_seconds = poll_interval_seconds
        self._cancellation_drain_timeout_seconds = cancellation_drain_timeout_seconds
        self._sleep = sleep

    async def wait(
        self,
        *,
        project: Project,
        project_id: str,
        response: ActionRunResponse,
        record_dispatch: RecordChildDispatch,
        stop_requested: StopRequested = lambda: False,
        rate_actual_cost: RateActualCost | None = None,
    ) -> ChildRunOutcome:
        """Record one launch, await it, and return its bounded durable result.

        ``record_dispatch`` first persists response-derived correlation facts,
        then receives a second idempotent update if the receipt supplies an
        idempotency key. This narrows the launch-to-wait process-death window
        without adding another scheduler; it cannot make a crashed process
        recover work that was never persisted.
        """

        initial = V1ActionResult.model_validate(response.payload)
        dispatch = ChildRunDispatch(
            project_id=project_id,
            action_kind=initial.action.kind,
            action_id=initial.action.action_id,
            idempotency_key=None,
            job_id=initial.job_id,
            run_id=initial.run_id,
            receipt_id=initial.receipt_id,
            op_ids=tuple(initial.op_ids),
        )
        initial_receipt: dict[str, Any] | None = None
        dispatch_recorded = False
        cancel_requested = False
        try:
            # Persist response-derived correlation before receipt I/O or a job
            # poll, but offload it: ledger writes must not block this event loop.
            # The callback is an idempotent upsert keyed by the action response.
            await await_thread_worker(record_dispatch, dispatch)
            dispatch_recorded = True

            initial_receipt = await self._receipt_if_present(
                project_id, initial.receipt_id
            )
            enriched_dispatch = replace(
                dispatch,
                idempotency_key=_optional_string(
                    initial_receipt.get("idempotency_key") if initial_receipt else None
                ),
            )
            if enriched_dispatch != dispatch:
                await await_thread_worker(record_dispatch, enriched_dispatch)
                dispatch = enriched_dispatch

            if initial.status in _TERMINAL_ACTION_STATUSES:
                return await self._outcome(
                    project=project,
                    dispatch=dispatch,
                    fallback=initial.model_dump(mode="json"),
                    receipt=initial_receipt,
                    public_job_status=None,
                    rate_actual_cost=rate_actual_cost,
                )
            if initial.job_id is None:
                raise RuntimeError(
                    f"child action is {initial.status!r} but has no durable job id"
                )

            public_job_status: dict[str, Any] | None = None
            while True:
                if not cancel_requested and await _requested(stop_requested):
                    cancel_requested = True
                    await self._request_cancel(dispatch)
                    try:
                        async with asyncio.timeout(
                            self._cancellation_drain_timeout_seconds
                        ):
                            public_job_status = await self._drain_job(
                                project_id, initial.job_id
                            )
                    except TimeoutError as exc:
                        # A 409 from cancellation can mean a writer is
                        # reconciling. Do not turn that into an endless poll or
                        # a fabricated terminal outcome; durable dispatch facts
                        # let the research host reconcile this typed result.
                        raise ChildRunDrainTimeout(dispatch) from exc
                    break

                public_job_status = await await_thread_worker(
                    self._action_runs.job_detail, project_id, initial.job_id
                )
                if str(public_job_status.get("status")) in _TERMINAL_JOB_STATUSES:
                    break
                await self._sleep(self._poll_interval_seconds)

            receipt_id = _optional_string(public_job_status.get("receipt_id"))
            if receipt_id is None:
                receipt_id = dispatch.receipt_id
            receipt = await self._receipt_if_present(project_id, receipt_id)
            fallback = _job_fallback(initial, public_job_status)
            return await self._outcome(
                project=project,
                dispatch=dispatch,
                fallback=fallback,
                receipt=receipt,
                public_job_status=public_job_status,
                rate_actual_cost=rate_actual_cost,
            )
        except asyncio.CancelledError:
            # A cancellation can land during the initial receipt read, before
            # its idempotency fact is available. Persist the action response's
            # durable correlation fields before asking the worker to stop.
            if not dispatch_recorded:
                await await_thread_worker(record_dispatch, dispatch)
            # Server shutdown/caller cancellation must not strand owned work.
            # Bound the drain so shutdown cannot wait forever on a live writer;
            # the persisted dispatch facts let later reconciliation find it.
            if not cancel_requested:
                await self._request_cancel(dispatch)
            if initial.job_id is not None:
                try:
                    async with asyncio.timeout(
                        self._cancellation_drain_timeout_seconds
                    ):
                        await self._drain_job(project_id, initial.job_id)
                except TimeoutError:
                    pass
            raise

    async def _drain_job(self, project_id: str, job_id: int) -> dict[str, Any]:
        while True:
            payload = await await_thread_worker(
                self._action_runs.job_detail, project_id, job_id
            )
            if str(payload.get("status")) in _TERMINAL_JOB_STATUSES:
                return payload
            await self._sleep(self._poll_interval_seconds)

    async def _receipt_if_present(
        self, project_id: str, receipt_id: str | None
    ) -> dict[str, Any] | None:
        if receipt_id is None:
            return None
        return await await_thread_worker(
            self._action_runs.receipt_lookup, project_id, receipt_id
        )

    async def _request_cancel(self, dispatch: ChildRunDispatch) -> None:
        try:
            if dispatch.run_id is not None:
                await await_thread_worker(
                    self._action_run_cancel.cancel_run,
                    dispatch.project_id,
                    dispatch.run_id,
                )
            elif dispatch.job_id is not None:
                await await_thread_worker(
                    self._action_runs.cancel_job, dispatch.project_id, dispatch.job_id
                )
        except RouteError as exc:
            # Canonical cancellation uses 409 for races such as a writer that
            # must terminalize or a job that finished between status and Stop.
            # The child still has to be drained so its final receipt/cost can
            # settle the research commitment exactly once.
            if exc.status_code != 409:
                raise

    async def _outcome(
        self,
        *,
        project: Project,
        dispatch: ChildRunDispatch,
        fallback: Mapping[str, Any],
        receipt: Mapping[str, Any] | None,
        public_job_status: dict[str, Any] | None,
        rate_actual_cost: RateActualCost | None,
    ) -> ChildRunOutcome:
        source = receipt or fallback
        provider_cost = _provider_cost(receipt)
        if receipt is None and provider_cost is None and dispatch.run_id is not None:
            provider_cost = await await_thread_worker(
                _run_provider_cost, project, dispatch.run_id
            )
        billed_cost = (
            await await_thread_worker(rate_actual_cost, provider_cost)
            if rate_actual_cost is not None and provider_cost is not None
            else None
        )
        if billed_cost is not None and (
            not isinstance(billed_cost, int)
            or isinstance(billed_cost, bool)
            or billed_cost < 0
        ):
            raise ValueError(
                "rate_actual_cost must return non-negative integer micros or null"
            )
        return ChildRunOutcome(
            dispatch=dispatch,
            status=str(source.get("status") or fallback.get("status") or "failed"),
            outputs=tuple(
                _output_refs(receipt.get("outputs")) if receipt is not None else []
            ),
            errors=tuple(_mapping_items(source.get("errors"))),
            warnings=tuple(
                str(item) for item in source.get("warnings") or [] if item is not None
            ),
            provider_cost_usd=provider_cost,
            billed_cost_micros=billed_cost,
            public_job_status=public_job_status,
        )


async def _requested(callback: StopRequested) -> bool:
    result = callback()
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


def _optional_string(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _job_fallback(
    initial: V1ActionResult, public_job_status: Mapping[str, Any]
) -> dict[str, Any]:
    status = str(public_job_status.get("status") or "failed")
    action_status = {
        "done": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status, status)
    error = public_job_status.get("error")
    errors: list[dict[str, Any]] = []
    if error:
        errors.append(
            {
                "code": "child_job_failed",
                "message": str(error),
                "action_kind": initial.action.kind,
                "details": {"job_id": initial.job_id},
            }
        )
    return {
        "status": action_status,
        "outputs": [],
        "errors": errors,
        "warnings": [],
    }


def _mapping_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _output_refs(value: Any) -> list[dict[str, Any]]:
    """Keep receipt-proven output references without copying result cells."""
    outputs: list[dict[str, Any]] = []
    for item in _mapping_items(value):
        ref = item.get("ref")
        if not isinstance(ref, Mapping):
            continue
        output: dict[str, Any] = {"ref": dict(ref)}
        for key in ("name", "kind"):
            if item.get(key) is not None:
                output[key] = str(item[key])
        outputs.append(output)
    return outputs


def _provider_cost(receipt: Mapping[str, Any] | None) -> float | None:
    if receipt is None:
        return None
    provider_use = receipt.get("provider_use")
    if not isinstance(provider_use, list):
        return None
    costs: list[Any] = []
    for item in provider_use:
        if not isinstance(item, Mapping):
            return None
        costs.append(item.get("cost_actual"))
    # provider_cost_total is the canonical bounded fact aggregation.  Its
    # empty sum is exactly zero (no metered operations); any present unknown
    # fact makes the aggregate unknown.
    return provider_cost_total(costs)


def _run_provider_cost(project: Project, run_id: int) -> float | None:
    row = project.db.execute(
        "SELECT cost_actual FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if row is None:
        return None
    value = row["cost_actual"]
    return provider_cost_value(value)
