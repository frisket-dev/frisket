"""Host-owned backfill preparation, source estimates and successor execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import partial
from types import MappingProxyType
from typing import Any, Callable

from frisket.actions.core import SemanticJoin, _ProjectAction
from frisket.actions.python_types import PythonEvaluator
from frisket.actions.http_types import HttpRequester
from frisket.actions.research_types import WebSearcher
from frisket.actions.file_types import FileFetcher
from frisket.actions.screenshot_types import Screenshotter
from frisket.actions.row_media_types import FrameExtractor, FaceExtractor
from frisket.actions.pdf_table_types import PdfTablesReader
from frisket.actions.opencorporates_types import OpenCorporates
from frisket.actions.temporal_types import TopicSectionsReader
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ColumnRef, PreparedBackfill, RunBackfiller, SheetRows
from frisket.contracts.action import ActionError, ActionResult, Receipt, ReceiptEvidence
from frisket.engine.executor.action_families.runs import (
    _guard_backfill_run_spec,
    _resolve_backfill_target,
    _run_backfill_output_ref,
    _run_backfill_receipt,
    _run_backfill_terminal_projection,
    _successful_result_row_ids,
)
from frisket.engine.executor.action_lifecycle import (
    _PreparedMapExecution,
    _run_reserved_maprunner_action,
)
from frisket.engine.executor.action_reservations import (
    _receipt_for_idempotency,
    _reserved_receipt_result_from_existing,
)
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.map_rows_action import (
    _TypedExecutionEnvelope,
    _typed_external_cost_error,
    _typed_model_cost_error,
    _typed_receipt,
    build_typed_map_rows_plan,
    bound_typed_program_request_from_runner_spec,
    normalized_typed_request_identity,
    typed_program_from_runner_spec,
    typed_request_hash,
)
from frisket.engine.runner.validation import recipe_for_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import FINISHED_RECEIPT_STATUSES, ReceiptStore


class BackfillRefused(Exception):
    def __init__(self, error: ActionError):
        self.error = error
        super().__init__(error.message)


@dataclass(frozen=True)
class _BackfillPlan:
    sheet_id: int
    column: str
    column_id: int
    source_run_id: int
    row_ids: tuple[int, ...]
    spec_json: str

    def spec(self) -> dict[str, Any]:
        return json.loads(self.spec_json)


def supports_typed_backfill_action(terminal: Any) -> bool:
    return isinstance(terminal, _ProjectAction) and terminal.capabilities == (
        RunBackfiller,
    )


def _resolve(
    project: Project, bound: BoundTypedActionRequest, column: ColumnRef[Any]
) -> _BackfillPlan:
    scope = bound.request.scope
    if not isinstance(scope, SheetRows) or not isinstance(column, ColumnRef):
        raise ValueError(
            "backfill requires an admitted sheet selection and a ColumnRef"
        )
    resolved = _resolve_backfill_target(
        project,
        sheet_id=scope.sheet_id,
        column=column.name,
        row_ids=list(scope.row_ids) if scope.row_ids is not None else None,
        confirmation=bound.request.confirmation,
    )
    if isinstance(resolved, ActionError):
        raise BackfillRefused(resolved)
    spec = resolved["runner_spec"]
    guard = _guard_backfill_run_spec(spec, project=project)
    if guard is not None:
        raise BackfillRefused(guard)
    # No source-run consent or caller-supplied routing state survives reconstruction.
    spec.pop("edition_run_context", None)
    return _BackfillPlan(
        scope.sheet_id,
        column.name,
        int(resolved["column_id"]),
        int(resolved["run_id"]),
        tuple(resolved["unrun_row_ids"]),
        json.dumps(spec, sort_keys=True, separators=(",", ":")),
    )


class _RunBackfiller:
    def __init__(self, project: Project, bound: BoundTypedActionRequest):
        self.project = project
        self.bound = bound
        self.plans: dict[PreparedBackfill, _BackfillPlan] = {}

    def prepare(self, column: ColumnRef[Any]) -> PreparedBackfill:
        plan = _resolve(self.project, self.bound, column)
        handle = PreparedBackfill(selected_row_count=len(plan.row_ids))
        self.plans[handle] = plan
        return handle


def prepare_backfill_action(
    project: Project, bound: BoundTypedActionRequest
) -> _BackfillPlan:
    """Run the complete callable using only real, read-only preparation results."""
    terminal = bound.action.definition.run
    if not supports_typed_backfill_action(terminal):
        raise TypeError("backfill requires a RunBackfiller action")
    capability = _RunBackfiller(project, bound)
    returned = terminal.handler(bound.params, capability)
    if not isinstance(returned, PreparedBackfill) or returned not in capability.plans:
        raise ValueError("backfill must return a preparation issued by this invocation")
    return capability.plans[returned]


def backfill_program(project: Project, spec: dict[str, Any]) -> Any:
    return typed_program_from_runner_spec(project, spec) or recipe_for_spec(spec)


def _cost_error(action: Any, exc: Any) -> ActionError:
    estimate = exc.estimate_details
    external = (
        isinstance(estimate, dict)
        and str(estimate.get("remote_capability") or "").startswith("external:")
        and "avg_input_tokens" not in estimate
    )
    return (_typed_external_cost_error if external else _typed_model_cost_error)(
        action, exc
    )


def run_typed_backfill_action(
    project: Project,
    project_id: str,
    bound: BoundTypedActionRequest,
    router: Any,
    map_runner_factory: Callable[..., Any],
) -> ActionResult:
    action = _TypedExecutionEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=MappingProxyType(dict(bound.request.params)),
    )
    request_hash = typed_request_hash(bound)
    existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
    if existing is not None:
        prior = Receipt.model_validate(json.loads(existing["body"]))
        replay_error = None
        if any(
            output.ref.get("kind") == "semantic_join_link_sheet"
            for output in prior.outputs
        ):
            from frisket.engine.executor.semantic_join_action import _replay_error

            replay_error = partial(_replay_error, project)
        return _reserved_receipt_result_from_existing(
            project,
            existing,
            params_hash=request_hash,
            project_id=project_id,
            action=action,
            replay_error_fn=replay_error,
        )
    try:
        plan = prepare_backfill_action(project, bound)
        # A callable may inspect other admitted preparations before choosing one.
        # Recheck the selected source after it returns, before reserving or running.
        if _resolve(project, bound, ColumnRef[Any](plan.column)) != plan:
            raise ValueError("the prepared backfill source changed before execution")
        spec = plan.spec()
        typed_program = bound_typed_program_request_from_runner_spec(
            spec, project=project
        )
        semantic_origin = None
        semantic_resolved = None
        if typed_program is not None and isinstance(
            typed_program.action.definition.run, SemanticJoin
        ):
            from frisket.engine.executor.semantic_join_action import (
                _resolve as resolve_semantic_join,
                validate_semantic_join_child,
            )

            previous = ReceiptStore(project).latest_for_run_statuses(
                plan.source_run_id, FINISHED_RECEIPT_STATUSES
            )
            if previous is None:
                raise ValueError("semantic join source receipt is unavailable")
            semantic_origin = previous.parsed()
            targets = dict(spec["output_target_preconditions"])
            semantic_resolved = resolve_semantic_join(
                project, typed_program, spec, router, admission=False
            )
            validate_semantic_join_child(
                project, semantic_resolved["semantic_join"], semantic_origin
            )
            spec["output_target_preconditions"] = targets
            if bound.request.confirmation is not None:
                spec["consented_promise_set_hash"] = bound.request.confirmation
        program = backfill_program(project, spec)
        successor_request_hash = (
            typed_request_hash(typed_program) if typed_program is not None else None
        )
        producer_plan = None
        if typed_program is not None and any(
            capability
            in getattr(typed_program.action.definition.run, "capabilities", ())
            for capability in (
                PythonEvaluator,
                HttpRequester,
                WebSearcher,
                TopicSectionsReader,
                PdfTablesReader,
                FileFetcher,
                Screenshotter,
                FrameExtractor,
                FaceExtractor,
                OpenCorporates,
            )
        ):
            producer_plan = replace(
                build_typed_map_rows_plan(
                    project, typed_program, _allow_existing_outputs=True
                ),
                program=program,
                output_names=MappingProxyType(dict(spec["output_names"])),
                output_fields=tuple(program.output_fields(spec)),
            )
    except BackfillRefused as exc:
        return _failed_result(
            project_id=project_id, action_kind=action.kind, error=exc.error
        )
    except (TypeError, ValueError) as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="invalid_params", message=str(exc), action_kind=action.kind
            ),
        )

    def receipt(
        current: Project, run_id: int, action_id: str, receipt_id: str
    ) -> Receipt:
        filled = _successful_result_row_ids(
            current,
            run_id=run_id,
            column_id=plan.column_id,
            row_ids=list(plan.row_ids),
        )
        output = _run_backfill_output_ref(
            sheet_id=plan.sheet_id,
            column=plan.column,
            column_id=plan.column_id,
            run_id=run_id,
            requested_row_ids=list(plan.row_ids),
            filled_row_ids=filled,
        )
        status, errors = _run_backfill_terminal_projection(
            current,
            action,
            run_id,
            requested_row_ids=list(plan.row_ids),
            filled_row_ids=filled,
        )
        result = _run_backfill_receipt(
            action=action,
            action_id=action_id,
            project_id=project_id,
            receipt_id=receipt_id,
            params_hash=request_hash,
            output_ref=output,
            status=status,
            errors=errors,
        )
        result.evidence.extend(
            [
                ReceiptEvidence(
                    ref={
                        "kind": "typed_action_request",
                        "request": normalized_typed_request_identity(bound),
                    }
                ),
                ReceiptEvidence(
                    ref={
                        "kind": "backfill_source_generation",
                        "run_id": plan.source_run_id,
                        "sheet_id": plan.sheet_id,
                        "column_id": plan.column_id,
                        "column": plan.column,
                        "row_ids": list(plan.row_ids),
                        # The successor's program differs from its parent's row
                        # scope/output names. Pin what this host actually ran,
                        # independently of the backfill callable's request hash.
                        "successor_run_id": run_id,
                        "successor_request_hash": successor_request_hash,
                        **(
                            {"source_action_kind": typed_program.action.action_id}
                            if typed_program is not None
                            else {}
                        ),
                    }
                ),
            ]
        )
        if producer_plan is not None:
            producer = _typed_receipt(
                current,
                run_id,
                action_id,
                receipt_id,
                project_id=project_id,
                plan=producer_plan,
                params_hash=successor_request_hash,
            )
            result.outputs.extend(
                output
                for output in producer.outputs
                if output.ref.get("kind") == "named_result"
            )
            result.evidence.extend(
                item
                for item in producer.evidence
                if item.ref.get("kind")
                in {
                    "typed_hidden_output",
                    "map_python_receipt_evidence",
                    "map_python_code",
                    "topic_segmentation_analysis",
                    "pdf_table_read",
                    "row_file_call",
                    "row_file_output",
                    "web_search_call",
                    "opencorporates_request",
                }
            )
            result.provider_use = producer.provider_use
            result.op_ids = producer.op_ids
            if producer.status == "failed" and result.status != "cancelled":
                result.status = "failed"
                result.errors = producer.errors
        return result

    def runner_factory(current: Project, current_router: Any) -> Any:
        runner = map_runner_factory(current, current_router)
        runner.allow_action_lifecycle_only_recipes = True
        return runner

    write = None

    def replay_error(_receipt):
        return None

    if semantic_origin is not None:
        from frisket.engine.executor.action_specs import ResolvedAction
        from frisket.engine.executor.semantic_join_action import (
            _replay_error,
            finalize_semantic_join,
        )

        replay_error = partial(_replay_error, project)

        def write(current, action, params, **kwargs):
            kwargs["resolved"] = ResolvedAction.from_resolve_dict(semantic_resolved)
            return finalize_semantic_join(
                current,
                action,
                params,
                bound=typed_program,
                receipt_fn=receipt,
                origin_receipt=semantic_origin,
                **kwargs,
            )

    return _run_reserved_maprunner_action(
        project,
        action,
        bound.params,
        project_id=project_id,
        router=router,
        map_runner_factory=runner_factory,
        resolved_execution=_PreparedMapExecution(
            runner_spec=spec,
            output_fields=tuple(program.output_fields(spec)),
            program=program,
            params_hash=request_hash,
            receipt_fn=receipt,
            replay_error_fn=replay_error,
            reservation_kind="typed_backfill_idempotency_reservation",
        ),
        write_fn=write,
        confirmed_fn=lambda _params: bound.request.confirmation is not None,
        cost_gate_error_fn=partial(_cost_error, action),
        output_claim_error_field="params.column",
    )
