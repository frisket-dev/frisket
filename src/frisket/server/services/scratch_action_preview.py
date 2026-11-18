"""One accounted lifecycle for bounded, server-owned scratch inputs."""

from __future__ import annotations
from frisket.execution.consent_coverage import ConsentCoverage

import copy
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Iterator, Mapping

from frisket.engine.executor.table_preview import TablePreviewResult
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import ATTEMPT_EXTRA, AttemptCommitment
from frisket.execution.resolve_for_action import SCRATCH_PREVIEW_UNIT_ID
from frisket.execution.resolver import ResolvedExecution
from frisket.ops.base import ActionContext
from frisket.ops.cost_source import estimate_from_cost_basis


ScratchPreviewRun = Callable[
    ["ScratchActionPreviewRunContext"], Awaitable[TablePreviewResult]
]


def scratch_estimate(
    resolved: ResolvedExecution, *, quantity_name: str | None, quantity: float
) -> dict[str, Any]:
    """Project the shared cost basis once, adding only its display quantity."""

    presentation = resolved.presentation
    estimate = estimate_from_cost_basis(
        resolved,
        **(
            {
                "billing_label": presentation.billing_label,
                "venue_label": presentation.venue_label,
            }
            if presentation is not None
            else {}
        ),
    )
    estimate["rows"] = 1
    if quantity_name is not None:
        estimate[quantity_name] = quantity
    return estimate


@dataclass(frozen=True)
class ScratchActionProgram:
    """The routed identity facts needed by resolution and attempt authority."""

    name: str
    execution_capability: str
    consumes_resolution: bool = True


@dataclass(frozen=True)
class ScratchActionPreviewPlan:
    action_kind: str
    program: ScratchActionProgram
    spec: dict[str, Any]
    resolved_execution: ResolvedExecution
    estimate: dict[str, Any]
    work_scope: dict[str, Any]
    source_identity: dict[str, Any]
    run: ScratchPreviewRun
    consent_coverage: ConsentCoverage | None = None


@dataclass(frozen=True)
class ScratchActionPreviewRunContext:
    project: Any
    resolved_execution: ResolvedExecution
    receipt_id: str | None
    attempt: AttemptCommitment | None
    action_kind: str
    router: Any
    composition: Any
    credential_use_context: Any
    execution_limits: Any
    progress: Callable[[int, int | None], None]
    cancel_event: threading.Event
    owner_cancelled: Callable[[], bool] | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set() or (
            self.owner_cancelled is not None and self.owner_cancelled()
        )

    def action_context(self, *, http: Any = None) -> ActionContext:
        return self._action_context(self._cloud_binding(), http=http)

    def _action_context(self, binding: Any, *, http: Any = None) -> ActionContext:
        extras: dict[str, Any] = {
            "preview": True,
            "preview_execution": self.resolved_for_preview,
            "router": self.router,
            "cancelled": lambda: self.cancelled,
        }
        if self.attempt is not None:
            extras[ATTEMPT_EXTRA] = self.attempt
        return ActionContext(
            project=self.project,
            http=http if http is not None else getattr(self.router, "client", None),
            extras=extras,
            credential_use_context=self.credential_use_context,
            execution_limits=self.execution_limits,
            cloud_execution=binding.media_binding if binding is not None else None,
        )

    def _cloud_binding(self) -> Any:
        if self.attempt is None:
            return None
        from frisket.cloud_runtime_binding import cloud_runtime_binding

        return cloud_runtime_binding(
            self.project,
            self.composition,
            self.attempt,
            row_id=SCRATCH_PREVIEW_UNIT_ID,
            request_id=f"preview:{self.attempt.receipt_id}:row:{SCRATCH_PREVIEW_UNIT_ID}",
            group_key=f"preview:{self.attempt.receipt_id}:row:{SCRATCH_PREVIEW_UNIT_ID}",
            action_kind=self.action_kind,
            run_id=None,
            claim_token=None,
            claimless_direct_effect=True,
        )

    @contextmanager
    def action_context_scope(self, *, http: Any = None) -> Iterator[ActionContext]:
        """Scope scratch execution to the admitted durable Cloud model call."""

        binding = self._cloud_binding()
        ctx = self._action_context(binding, http=http)
        if binding is None or binding.llm_binding is None:
            from contextlib import nullcontext

            capture = getattr(
                getattr(self.router, "model_call_policy", None), "operation_scope", None
            )
            with capture(ctx) if capture is not None else nullcontext():
                yield ctx
            return
        from frisket.cloud_execution import cloud_llm_execution_scope

        from frisket.execution.runtime_binding import bind_fact_to_route

        try:
            with cloud_llm_execution_scope(binding.llm_binding):
                yield ctx
        finally:
            # Parsing or rendering may fail after Cloud has returned paid work.
            # The scope owns these facts independently of the engine's output.
            calls = [
                bind_fact_to_route(binding.route, call)
                for accounting in binding.llm_coordinator.returned_accounting
                for call in accounting.get("model_calls", ())
            ]
            self.write_model_calls(calls)
            # If recording fails, leave the returned checkpoint recoverable.
            binding.llm_coordinator.retire_returned()

    @property
    def resolved_for_preview(self) -> ResolvedExecution | None:
        return (
            None
            if self.attempt is not None
            else replace(self.resolved_execution, persistence="ephemeral")
        )

    def write_model_calls(self, calls: list[Mapping[str, Any]]) -> float:
        if not calls or self.attempt is None or self.receipt_id is None:
            return 0.0
        batch = [
            {
                "row_id": SCRATCH_PREVIEW_UNIT_ID,
                "column_id": None,
                "model_calls": [copy.deepcopy(dict(c)) for c in calls],
            }
        ]
        return RunResultStore(self.project).write_returned_call_accounting(
            None,
            batch,
            writer_attempt_id=self.attempt.attempt_id,
            authorized_attempt_id=self.attempt.attempt_id,
            receipt_id=self.receipt_id,
        )
