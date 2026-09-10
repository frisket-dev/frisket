"""Invocation-owned grouped-summary preparation and concrete paid execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from frisket.actions.group_summary_types import (
    GROUP_SUMMARY_COLUMNS,
    GroupSummaryColumn,
    GroupSummaryOptions,
    PreparedGroupSummary,
)
from frisket.actions.types import SheetRows
from frisket.contracts.action import ActionError, ActionIdentity, ActionResult
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.action_reservations import (
    _receipt_for_idempotency,
    _reserve_running_action_receipt,
)
from frisket.engine.executor.action_support import (
    _estimate_confirmation_reason,
    _failed_result,
)
from frisket.engine.executor.group_summary_plan import GroupSummaryPlan
from frisket.engine.executor import group_summary_runtime as runtime
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.attempt import StaleAttemptWriter


class GroupSummaryRefused(ValueError):
    def __init__(self, error: ActionError):
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class PreparedGroupSummaryPlan:
    operation: GroupSummaryPlan
    resolved: dict[str, Any]
    estimate: dict[str, Any]
    output_fields: tuple[dict[str, str], ...]
    creates_sheet: bool = True
    required_capabilities: tuple[str, ...] = ("project:write", "model:complete")


class AdmittedGroupSummarizer:
    def __init__(self, project, bound):
        self.project, self.bound = project, bound
        self.plans: dict[PreparedGroupSummary, PreparedGroupSummaryPlan] = {}

    def prepare(self, source, *, options):
        from frisket.actions.group_summary import GroupSummaryParams
        from frisket.engine.executor.map_rows_action import (
            TypedMapRowsPlanError,
            validate_typed_project_references,
        )

        if not isinstance(options, GroupSummaryOptions):
            raise TypeError("group summary options must be a GroupSummaryOptions value")
        options = GroupSummaryOptions.model_validate(options.model_dump())
        columns = [GroupSummaryColumn.model_validate(column) for column in source]
        if not columns or len({column.name for column in columns}) != len(columns):
            raise ValueError("group summary requires distinct source columns")
        request = self.bound.request
        if not isinstance(request.scope, SheetRows):
            raise ValueError("group summary requires sheet_rows scope")
        if not request.sheet_name:
            raise ValueError("group summary requires sheet_name")
        if request.replace_existing:
            raise ValueError(
                "group summary creates a new sheet; replacement is not supported"
            )
        fields = tuple((column.key, column.type) for column in GROUP_SUMMARY_COLUMNS)
        if set(request.output_names) - {key for key, _ in fields}:
            raise ValueError("output_names contains an unknown group summary output")
        names = {key: request.output_names.get(key, key) for key, _ in fields}
        if len(set(names.values())) != len(names):
            raise ValueError("group summary output names must be distinct")
        payload = request.model_dump(mode="json", exclude={"confirmation"})
        operation = GroupSummaryPlan(
            action_kind=self.bound.action.action_id,
            request=payload,
            sheet_id=request.scope.sheet_id,
            row_ids=request.scope.row_ids,
            input_columns=[column.name for column in columns],
            group_by=options.group_by.name if options.group_by is not None else None,
            model=options.model.root,
            instruction=options.instruction,
            target_sheet_name=request.sheet_name,
            group_column_name=names["group"],
            row_count_column_name=names["rows"],
            summary_column_name=names["summary"],
        )
        resolved = runtime._resolve_reduce_group_summary_inputs(self.project, operation)
        if isinstance(resolved, ActionError):
            raise GroupSummaryRefused(resolved)
        try:
            validate_typed_project_references(
                self.project,
                request.scope.sheet_id,
                GroupSummaryParams(source=columns, **options.model_dump()),
            )
        except TypedMapRowsPlanError as exc:
            raise GroupSummaryRefused(
                ActionError(
                    code=exc.code,
                    message=str(exc),
                    action_kind=self.bound.action.action_id,
                    field=exc.field or "params.source",
                    details=dict(exc.details),
                )
            ) from exc
        duplicate = runtime._precheck_reduce_group_summary_target(
            self.project, operation
        )
        if duplicate is not None:
            raise GroupSummaryRefused(duplicate)
        handle = PreparedGroupSummary(
            len(resolved["source_row_ids"]), len(resolved["groups"])
        )
        self.plans[handle] = PreparedGroupSummaryPlan(
            operation,
            resolved,
            runtime._estimate_reduce_group_summary_cost(operation, resolved),
            tuple({"key": key, "column_type": typ} for key, typ in fields),
        )
        return handle


def prepare_group_summary_action(project, bound):
    summarizer = AdmittedGroupSummarizer(project, bound)
    handle = bound.action.definition.run.handler(bound.params, summarizer)
    if not isinstance(handle, PreparedGroupSummary) or handle not in summarizer.plans:
        raise ValueError("group summary must return its own invocation's preparation")
    return summarizer.plans[handle]


def run_typed_group_summary_action(
    project, project_id, bound, router=None, *, deps=None
):
    from frisket.execution.consent_coverage import effective_consent_coverage

    consent_coverage = effective_consent_coverage(
        project, deps.consent_coverage if deps is not None else None
    )
    from frisket.ai.llm.router import model_call_cannot_go_live
    from frisket.ai.llm.types import provider_from_model_id
    from frisket.engine.executor.map_rows_action import typed_request_hash
    from frisket.engine.jobs.runs import project_scoped_router
    from frisket.engine.runner.confirmation_context import (
        ActionScope,
        action_confirmation,
        mint_confirmation_hash,
        quoted_usd,
        rate_estimate,
    )
    from frisket.engine.runner.validation import (
        ProviderKeyRefusal,
        assert_provider_spend_cap,
    )
    from frisket.execution.pricing_policy import default_pricing_policy

    action = _TypedProjectEnvelope(
        bound.action.action_id, bound.request.idempotency_key, bound.request.params
    )
    identity = typed_request_hash(bound)
    existing = _receipt_for_idempotency(project, action.idempotency_key)
    if existing is not None:
        return runtime._reduce_group_summary_result_from_existing_receipt(
            project,
            existing,
            params_hash=identity,
            project_id=project_id,
            action=action,
        )
    try:
        prepared = prepare_group_summary_action(project, bound)
    except (TypeError, ValueError) as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=exc.error
            if isinstance(exc, GroupSummaryRefused)
            else ActionError(
                code="invalid_params", message=str(exc), action_kind=action.kind
            ),
        )
    estimate = rate_estimate(prepared.estimate, policy=default_pricing_policy())
    price = quoted_usd(estimate)
    token = mint_confirmation_hash(
        action_confirmation(
            family_kind=action.kind,
            scope=ActionScope(action_hash=identity),
            estimate=estimate,
        )
    )
    if (
        price is None or price > float(consent_coverage.threshold_usd)
    ) and bound.request.confirmation != token:
        return ActionResult(
            action=ActionIdentity(kind=action.kind, action_id=f"act_{uuid4().hex}"),
            project_id=project_id,
            status="needs_confirmation",
            errors=[
                ActionError(
                    code="model_cost_requires_confirmation",
                    message="Confirm the grouped model work before running",
                    action_kind=action.kind,
                    field="confirmation",
                    needs_confirmation=True,
                    details={
                        "reason": _estimate_confirmation_reason(price),
                        "estimate": estimate,
                        "promise_set_hash": token,
                    },
                )
            ],
        )
    router = project_scoped_router(project, router)
    if not model_call_cannot_go_live(router):
        if (
            project.effective_network_policy() == "off"
            and provider_from_model_id(prepared.operation.model) != "ollama"
        ):
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code="network_disabled",
                    message="Project network access is disabled",
                    action_kind=action.kind,
                ),
            )
        try:
            assert_provider_spend_cap(
                project, provider_from_model_id(prepared.operation.model)
            )
        except ProviderKeyRefusal as exc:
            return _failed_result(
                project_id=project_id,
                action_kind=action.kind,
                error=ActionError(
                    code=exc.error_code,
                    message=exc.action_message(),
                    action_kind=action.kind,
                    field=exc.field,
                    details=dict(exc.details),
                ),
            )
    reservation = _reserve_running_action_receipt(
        project,
        action,
        params_hash=identity,
        project_id=project_id,
        reservation_kind="reduce_group_summary_idempotency_reservation",
        result_from_existing_fn=runtime._reduce_group_summary_result_from_existing_receipt,
    )
    if isinstance(reservation, ActionResult):
        return reservation
    try:
        computations = asyncio.run(
            runtime._complete_reduce_group_summaries(
                project,
                prepared.operation,
                prepared.resolved,
                router=router,
            )
        )
        if isinstance(computations, ActionError):
            ReceiptStore(project).delete_running(reservation["receipt_id"])
            return _failed_result(
                project_id=project_id, action_kind=action.kind, error=computations
            )
        result = runtime._write_reduce_group_summary_result(
            project,
            action,
            prepared.operation,
            params_hash=identity,
            project_id=project_id,
            action_id=reservation["action_id"],
            receipt_id=reservation["receipt_id"],
            resolved=prepared.resolved,
            estimate=estimate,
            computations=computations,
        )
    except StaleAttemptWriter as exc:
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="stale_attempt_writer", message=str(exc), action_kind=action.kind
            ),
        )
    except Exception as exc:
        ReceiptStore(project).delete_running(reservation["receipt_id"])
        return _failed_result(
            project_id=project_id,
            action_kind=action.kind,
            error=ActionError(
                code="model_run_failed", message=str(exc), action_kind=action.kind
            ),
        )
    if result.status == "failed" and result.receipt_id is None:
        ReceiptStore(project).delete_running(reservation["receipt_id"])
    return result
