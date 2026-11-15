"""Live dispatch permission; returned responses are never checked here."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class DispatchControl(Protocol):
    def check(self) -> None: ...


@dataclass(frozen=True)
class StandaloneDispatch:
    """Explicit non-run work, such as a provider probe or interactive chat."""

    def check(self) -> None:
        pass


STANDALONE = StandaloneDispatch()


@dataclass(frozen=True)
class TransientDispatch:
    """Preview lifetime belongs to its owning asyncio task, not a saved run."""

    cancelled: Callable[[], bool]

    def check(self) -> None:
        task = asyncio.current_task()
        if self.cancelled() or (task is not None and task.cancelling()):
            raise asyncio.CancelledError


@dataclass(frozen=True)
class RunDispatch:
    project: Any
    run_id: int
    attempt_id: str
    cancelled: Callable[[], bool] | None = None
    claim_token: str | None = None

    def check(self) -> None:
        live = self.project.db.execute(
            "SELECT op.id FROM runs run JOIN ops op ON op.id=run.op_id "
            "JOIN execution_attempts attempt ON attempt.run_id=run.id "
            "WHERE run.id=? AND attempt.id=? AND attempt.state='dispatching' "
            "AND run.current_attempt_id=attempt.id AND run.status='running' "
            "AND run.cancel_requested_at IS NULL AND op.status='applied' "
            "AND (op.change_order IS NULL OR op.publication_closed=0)",
            (self.run_id, self.attempt_id),
        ).fetchone()
        if live is None or (self.cancelled is not None and self.cancelled()):
            raise asyncio.CancelledError
        from frisket.engine.store.output_claims import OutputColumnClaimStore
        from frisket.engine.store.source_dependencies import (
            ExecutionSourceUnavailable,
            require_execution_sources,
        )
        from frisket.execution.attempt import StaleAttemptWriter

        try:
            require_execution_sources(self.project, int(live[0]))
            OutputColumnClaimStore.require_current_writer(
                self.project.db,
                run_id=self.run_id,
                writer_attempt_id=self.attempt_id,
                claim_token=self.claim_token,
                claimless_direct_effect=self.claim_token is None,
            )
        except (StaleAttemptWriter, ExecutionSourceUnavailable):
            raise asyncio.CancelledError from None


def dispatch_for_context(ctx: Any) -> DispatchControl:
    """Owned actions cannot silently fall back to an unbound provider call."""
    from frisket.execution.attempt import attempt_in_scope

    run_id = ctx.extras.get("run_id")
    if run_id is not None:
        attempt = attempt_in_scope(ctx.extras)
        if attempt is None or attempt.run_id != run_id:
            raise RuntimeError("Managed model dispatch requires its execution attempt")
        return RunDispatch(
            ctx.project,
            int(run_id),
            attempt.attempt_id,
            ctx.extras.get("cancelled"),
            ctx.extras.get("claim_token"),
        )
    if ctx.extras.get("preview") is True:
        cancelled = ctx.extras.get("cancelled")
        if not callable(cancelled):
            raise RuntimeError("Preview dispatch requires its cancellation owner")
        return TransientDispatch(cancelled)
    if ctx.project is None and ctx.extras.get("standalone_dispatch") is STANDALONE:
        return STANDALONE
    raise RuntimeError("Model dispatch requires a run or explicit preview context")
