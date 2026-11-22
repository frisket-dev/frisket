"""Host-owned backfill preparation, source estimates and successor execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import partial
from types import MappingProxyType
from typing import Any, Callable, TypeGuard

from frisket.engine.executor.action_inventory import _typed_request_envelope
from frisket.actions.core import SemanticJoin, _ProjectAction
from frisket.actions.python_types import PythonEvaluator
from frisket.actions.http_types import HttpRequester
from frisket.actions.research_types import Researcher, SearchAnswerer, WebSearcher
from frisket.actions.file_types import FileFetcher
from frisket.actions.screenshot_types import Screenshotter
from frisket.actions.row_media_types import FrameExtractor, FaceExtractor
from frisket.actions.pdf_table_types import PdfTablesReader
from frisket.actions.opencorporates_types import OpenCorporates
from frisket.actions.temporal_types import TopicSectionsReader
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ColumnRef, PreparedBackfill, RunBackfiller, SheetRows
from frisket.contracts.action import ActionError, ActionResult, Receipt, ReceiptRecord
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
from frisket.engine.executor.action_support import ActionCtx
from frisket.engine.executor.map_rows_action import (
    _typed_external_cost_error,
    _typed_model_cost_error,
    _typed_receipt,
    build_typed_map_rows_plan,
    bound_typed_program_request_from_runner_spec,
    normalized_typed_request_identity,
    typed_program_from_runner_spec,
    typed_request_hash,
)
from frisket.engine.runner.validation import action_program_for_spec
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


def supports_typed_backfill_action(terminal: object) -> TypeGuard[_ProjectAction]:
    return isinstance(terminal, _ProjectAction) and terminal.capabilities == (
        RunBackfiller,
    )


def _resolve(
    project: Project,
    bound: BoundTypedActionRequest,
    column: ColumnRef[Any],
    *,
    edition: str | None = None,
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
    guard = _guard_backfill_run_spec(spec, project=project, edition=edition)
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


def _resolve_retry(
    project: Project,
    bound: BoundTypedActionRequest,
    source_run_id: int,
    *,
    edition: str | None = None,
) -> _BackfillPlan:
    from frisket.engine.store.execution_groups import execution_runs
    from frisket.engine.store.runs import RunResultStore
    from frisket.engine.store.result_generations import ResultGenerationStore

    scope = bound.request.scope
    if not isinstance(scope, SheetRows):
        raise ValueError("Retry requires a sheet selection")
    root_id, run_ids = execution_runs(project, source_run_id)
    original = project.db.execute(
        "SELECT * FROM runs WHERE id=?", (root_id,)
    ).fetchone()
    if original is None or original["sheet_id"] != scope.sheet_id:
        raise ValueError("Execution does not belong to the selected sheet")
    from frisket.authoring.action_metadata import action_supports_row_retry

    if not action_supports_row_retry(original["action_kind"]):
        raise BackfillRefused(
            ActionError(
                code="run_required",
                message="This action transforms the whole column; run the action again instead of retrying rows.",
                action_kind="run.retry",
            )
        )
    original_rows = set(RunResultStore(project).run_row_scope(root_id))
    requested = (
        original_rows.intersection(project.visible_row_ids(scope.sheet_id))
        if scope.row_ids is None
        else set(scope.row_ids)
    )
    if not requested.issubset(original_rows):
        raise ValueError("Retry cannot expand the original execution cohort")
    bindings = ResultGenerationStore(project).bindings_for_run(root_id)
    if not bindings:
        raise ValueError("Execution has no retained output schema")
    from frisket.engine.store.execution_groups import completed_execution_rows

    done = completed_execution_rows(project, root_id, run_ids)
    remaining = sorted(requested - done)
    if not remaining:
        raise ValueError("Execution has no unfinished requested rows")
    columns = [project.get_column(binding.column_id) for binding in bindings]
    active_columns = [
        column for column in columns if column is not None and column["active"]
    ]
    if len(active_columns) != len(columns) or not active_columns:
        raise ValueError(
            "Original output columns are no longer active. Reapply saved results "
            "to restore completed work, or start a new action for unfinished rows."
        )
    target = active_columns[0]
    resolved = _resolve_backfill_target(
        project,
        sheet_id=scope.sheet_id,
        column=target["name"],
        row_ids=remaining,
        confirmation=bound.request.confirmation,
        source_run_id=root_id,
    )
    if isinstance(resolved, ActionError):
        raise BackfillRefused(resolved)
    spec = resolved["runner_spec"]
    guard = _guard_backfill_run_spec(spec, project=project, edition=edition)
    if guard is not None:
        raise BackfillRefused(guard)
    spec.pop("edition_run_context", None)
    spec["execution_root_run_id"] = root_id
    return _BackfillPlan(
        scope.sheet_id,
        target["name"],
        bindings[0].column_id,
        root_id,
        tuple(remaining),
        json.dumps(spec, sort_keys=True, separators=(",", ":")),
    )


class _RunBackfiller:
    def __init__(
        self, project: Project, bound: BoundTypedActionRequest, edition: str | None
    ):
        self.project = project
        self.bound = bound
        self.edition = edition
        self.plans: dict[PreparedBackfill, _BackfillPlan] = {}

    def retry(self, source_run_id: int) -> PreparedBackfill:
        plan = _resolve_retry(
            self.project, self.bound, source_run_id, edition=self.edition
        )
        handle = PreparedBackfill(selected_row_count=len(plan.row_ids))
        self.plans[handle] = plan
        return handle

    def prepare(self, column: ColumnRef[Any]) -> PreparedBackfill:
        plan = _resolve(self.project, self.bound, column, edition=self.edition)
        handle = PreparedBackfill(selected_row_count=len(plan.row_ids))
        self.plans[handle] = plan
        return handle


def prepare_backfill_action(
    project: Project, bound: BoundTypedActionRequest, *, edition: str | None = None
) -> _BackfillPlan:
    """Run the complete callable using only real, read-only preparation results."""
    terminal = bound.action.definition.run
    if not supports_typed_backfill_action(terminal):
        raise TypeError("backfill requires a RunBackfiller action")
    capability = _RunBackfiller(project, bound, edition)
    returned = terminal.handler(bound.params, capability)
    if not isinstance(returned, PreparedBackfill) or returned not in capability.plans:
        raise ValueError("backfill must return a preparation issued by this invocation")
    return capability.plans[returned]


def backfill_program(project: Project, spec: dict[str, Any]) -> Any:
    return typed_program_from_runner_spec(project, spec) or action_program_for_spec(
        spec
    )


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
    *,
    edition: str | None = None,
) -> ActionResult:
    action = _typed_request_envelope(bound)
    request_hash = typed_request_hash(bound)
    existing = _receipt_for_idempotency(project, bound.request.idempotency_key)
    if existing is not None:
        prior = Receipt.model_validate(json.loads(existing["body"]))
        existing_replay_error: Callable[[Receipt], ActionError | None] | None = None
        if any(
            output.ref.get("kind") == "semantic_join_link_sheet"
            for output in prior.outputs
        ):
            from frisket.engine.executor.semantic_join_action import _replay_error

            existing_replay_error = partial(_replay_error, project)
        return _reserved_receipt_result_from_existing(
            project,
            existing,
            params_hash=request_hash,
            project_id=project_id,
            action=action,
            replay_error_fn=existing_replay_error,
        )
    try:
        plan = prepare_backfill_action(project, bound, edition=edition)
        # A callable may inspect other admitted preparations before choosing one.
        # Recheck the selected source after it returns, before reserving or running.
        source_run_id = getattr(bound.params, "source_run_id", None)
        if (
            _resolve_retry(project, bound, source_run_id, edition=edition)
            if isinstance(source_run_id, int)
            else _resolve(project, bound, ColumnRef[Any](plan.column), edition=edition)
        ) != plan:
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
                Researcher,
                SearchAnswerer,
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
        return ActionCtx(project_id, action.kind).failed(exc.error)
    except (TypeError, ValueError) as exc:
        return ActionCtx(project_id, action.kind).fail("invalid_params", str(exc))

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
        result.records.extend(
            [
                ReceiptRecord(
                    ref={
                        "kind": "typed_action_request",
                        "request": normalized_typed_request_identity(bound),
                    }
                ),
                ReceiptRecord(
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
        if producer_plan is not None and successor_request_hash is not None:
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
            result.records.extend(
                item
                for item in producer.records
                if item.ref.get("kind")
                in {
                    "typed_hidden_output",
                    "map_python_receipt_record",
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
        runner.allow_action_lifecycle_only_programs = True
        return runner

    def _no_replay_error(_receipt: Receipt) -> ActionError | None:
        return None

    write: Callable[..., ActionResult] | None = None
    replay_error: Callable[[Receipt], ActionError | None] = _no_replay_error

    if semantic_origin is not None and semantic_resolved is not None:
        from frisket.engine.executor.action_specs import ResolvedAction
        from frisket.engine.executor.semantic_join_action import (
            _replay_error,
            finalize_semantic_join,
        )

        resolved_semantic = semantic_resolved
        replay_error = partial(_replay_error, project)

        def _write_semantic(current, action, params, **kwargs):
            kwargs["resolved"] = ResolvedAction.from_resolve_dict(resolved_semantic)
            return finalize_semantic_join(
                current,
                action,
                params,
                bound=typed_program,
                receipt_fn=receipt,
                origin_receipt=semantic_origin,
                **kwargs,
            )

        write = _write_semantic

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
