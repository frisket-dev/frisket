"""One accounted lifecycle for bounded, server-owned scratch inputs."""

from __future__ import annotations
from frisket.execution.consent_coverage import ConsentCoverage

import copy
import threading
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping

from frisket.engine.executor.table_preview import TablePreviewResult
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import ATTEMPT_EXTRA, AttemptCommitment
from frisket.execution.resolve_for_action import SCRATCH_PREVIEW_UNIT_ID
from frisket.execution.resolver import ResolvedExecution
from frisket.ops.base import OpContext
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
class ScratchActionRecipe:
    """The routed identity facts needed by resolution and attempt authority."""

    name: str
    execution_capability: str
    consumes_resolution: bool = True


@dataclass(frozen=True)
class ScratchActionPreviewPlan:
    action_kind: str
    recipe: ScratchActionRecipe
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
    router: Any
    credential_use_context: Any
    execution_limits: Any
    progress: Callable[[int, int | None], None]
    cancel_event: threading.Event

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def op_context(self, *, http: Any = None) -> OpContext:
        extras: dict[str, Any] = {
            "preview": True,
            "preview_execution": self.resolved_for_preview,
            "router": self.router,
            "cancelled": self.cancel_event.is_set,
        }
        if self.attempt is not None:
            extras[ATTEMPT_EXTRA] = self.attempt
        return OpContext(
            project=self.project,
            http=http if http is not None else getattr(self.router, "client", None),
            extras=extras,
            credential_use_context=self.credential_use_context,
            execution_limits=self.execution_limits,
        )

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
