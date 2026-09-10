"""Prepared exhaustive findings on the existing action.run queue lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from frisket.actions.find_types import (
    FindOptions,
    FindScanner,
    FindSourceColumn,
    PreparedFind,
)
from frisket.actions.types import SheetRows
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionResult,
    ReceiptEvidence,
)
from frisket.engine.executor.action_inventory import _TypedProjectEnvelope
from frisket.engine.executor.action_jobs import (
    bind_typed_action_job,
    reserve_typed_action_job,
)
from frisket.engine.executor.action_support import _failed_result
from frisket.engine.executor.find_plan import FindOperation
from frisket.engine.executor.map_find_planning import (
    PreparedFindScan,
    _prepare_find_scan,
)
from frisket.engine.executor.map_find_publication import (
    _failure_after_egress,
    _failure_before_egress,
    write_find_result,
)
from frisket.engine.executor.map_find_scan import scan_find_plans
from frisket.engine.executor.map_find_source import _snapshot_payload
from frisket.engine.store.receipts import ReceiptStore


class FindRefused(ValueError):
    def __init__(self, error):
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class PreparedFindPlan:
    operation: FindOperation
    scan: PreparedFindScan
    output_fields: tuple[dict[str, Any], ...]
    creates_sheet: bool = True
    required_capabilities: tuple[str, ...] = ("project:write", "model:complete")

    @property
    def estimate(self):
        return self.scan.estimate


class AdmittedFindScanner:
    def __init__(self, project, bound):
        self.project, self.bound = project, bound
        self.plans: dict[PreparedFind, PreparedFindPlan] = {}

    def prepare(self, source, *, options):
        source = FindSourceColumn.model_validate(source)
        if not isinstance(options, FindOptions):
            raise TypeError("find options must be a FindOptions value")
        options = FindOptions.model_validate(options.model_dump())
        request = self.bound.request
        if not isinstance(request.scope, SheetRows):
            raise ValueError("Find requires sheet_rows scope")
        if not request.sheet_name:
            raise ValueError("Find requires a findings sheet_name")
        if request.replace_existing:
            raise ValueError(
                "Find manages its own findings generation; column replacement is unsupported"
            )
        fields = [
            {"key": "match", "column_type": "text"},
            *(
                {"key": field.name, "column_type": field.column_type}
                for field in options.fields
            ),
        ]
        logical = {field["key"] for field in fields}
        if set(request.output_names) - logical:
            raise ValueError("output_names contains an unknown finding field")
        names = {key: request.output_names.get(key, key) for key in logical}
        if len(set(names.values())) != len(names):
            raise ValueError("finding output names must be distinct")
        operation = FindOperation(
            action_kind=self.bound.action.action_id,
            request=request.model_dump(mode="json", exclude={"confirmation"}),
            sheet_id=request.scope.sheet_id,
            row_ids=request.scope.row_ids,
            source_column=source.name,
            model=options.model.root,
            instruction=options.instruction,
            fields=options.fields,
            target_sheet_name=request.sheet_name,
            output_names=names,
        )
        scan = _prepare_find_scan(self.project, operation)
        if isinstance(scan, ActionError):
            raise FindRefused(scan)
        handle = PreparedFind(len(scan.sources), len(scan.plans))
        self.plans[handle] = PreparedFindPlan(operation, scan, tuple(fields))
        return handle


def prepare_find_action(project, bound):
    if getattr(bound.action.definition.run, "capabilities", ()) != (FindScanner,):
        raise TypeError("Find preparation requires the admitted FindScanner capability")
    scanner = AdmittedFindScanner(project, bound)
    handle = bound.action.definition.run.handler(bound.params, scanner)
    if not isinstance(handle, PreparedFind) or handle not in scanner.plans:
        raise ValueError("Find must return a preparation issued by this invocation")
    return scanner.plans[handle]


def _admission(bound, prepared, *, consent_coverage):
    from frisket.engine.executor.map_rows_action import typed_request_hash
    from frisket.engine.runner.confirmation_context import (
        ActionScope,
        action_confirmation,
        mint_confirmation_hash,
        quoted_usd,
    )

    estimate = prepared.estimate
    token = mint_confirmation_hash(
        action_confirmation(
            family_kind=bound.action.action_id,
            scope=ActionScope(action_hash=typed_request_hash(bound)),
            estimate=estimate,
        )
    )
    price = quoted_usd(estimate)
    if (
        price is None or price > float(consent_coverage.threshold_usd)
    ) and bound.request.confirmation != token:
        raise FindRefused(
            ActionError(
                code="model_cost_requires_confirmation",
                message="Confirm the exhaustive scan before running",
                action_kind=bound.action.action_id,
                field="confirmation",
                needs_confirmation=True,
                details={"estimate": estimate, "promise_set_hash": token},
            )
        )
    return {
        "schema_version": "frisket.map_find_admission.v1",
        "sources": _snapshot_payload(prepared.scan.sources),
        "target": prepared.scan.target,
        "resolved_prompt_hash": estimate["resolved_prompt_hash"],
        "window_count": len(prepared.scan.plans),
        "estimate": estimate,
        "confirmation_hash": token,
        "consent_principal": consent_coverage.principal,
        "operation": prepared.operation.model_dump(mode="json"),
    }


def prepare_typed_find_admission(project, bound, *, consent_coverage=None):
    from frisket.execution.consent_coverage import effective_consent_coverage

    try:
        return _admission(
            bound,
            prepare_find_action(project, bound),
            consent_coverage=effective_consent_coverage(project, consent_coverage),
        )
    except (TypeError, ValueError) as exc:
        return (
            exc.error
            if isinstance(exc, FindRefused)
            else ActionError(
                code="invalid_params",
                message=str(exc),
                action_kind=bound.action.action_id,
            )
        )


def reserve_typed_find_action_job(
    project, project_id, bound, *, edition_run_context=None, consent_coverage=None
):
    from frisket.engine.executor.action_jobs import probe_queued_action_job_receipt
    from frisket.engine.executor.map_rows_action import typed_request_hash

    identity = _TypedProjectEnvelope(
        bound.action.action_id, bound.request.idempotency_key, bound.request.params
    )
    replay = probe_queued_action_job_receipt(
        project, identity, project_id=project_id, params_hash=typed_request_hash(bound)
    )
    if replay is not None:
        return replay
    admission = prepare_typed_find_admission(
        project, bound, consent_coverage=consent_coverage
    )
    if isinstance(admission, ActionError):
        return ActionResult(
            action=ActionIdentity(
                kind=bound.action.action_id, action_id=f"act_{uuid4().hex}"
            ),
            project_id=project_id,
            status="needs_confirmation" if admission.needs_confirmation else "failed",
            errors=[admission],
        )
    envelope = reserve_typed_action_job(
        project, project_id, bound, edition_run_context=edition_run_context
    )
    if isinstance(envelope, ActionResult):
        return envelope
    receipts = ReceiptStore(project)
    receipt = receipts.parsed_by_id(envelope.receipt_id)
    assert receipt is not None
    evidence = [
        *receipt.evidence,
        ReceiptEvidence(
            ref={"kind": "find_admission", "admission": admission}, retention="pinned"
        ),
    ]
    if not receipts.update_body_status(
        receipt.model_copy(update={"evidence": evidence}), require_status="queued"
    ):
        raise RuntimeError("Find lost its queued receipt before pinning admission")
    return replace(envelope, resolved_snapshot=admission)


def run_typed_find_action_job(project, envelope, *, deps=None):
    from frisket.ai.llm.router import model_call_cannot_go_live
    from frisket.ai.llm.types import provider_from_model_id
    from frisket.engine.jobs.runs import project_scoped_router
    from frisket.engine.runner.validation import (
        ProviderKeyRefusal,
        assert_provider_spend_cap,
    )

    bound = bind_typed_action_job(envelope)
    if isinstance(bound, ActionResult):
        return _failure_before_egress(project, envelope, bound.errors[0])
    receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    admitted = dict(envelope.resolved_snapshot or {})
    # The worker adds its queue identity after admission. It is transport
    # bookkeeping, not an admitted source, target, prompt, or consent fact.
    admitted.pop("job_id", None)
    durable = (
        next(
            (
                item.ref.get("admission")
                for item in receipt.evidence
                if item.ref.get("kind") == "find_admission"
            ),
            None,
        )
        if receipt
        else None
    )
    if durable != admitted:
        return _failure_before_egress(
            project,
            envelope,
            ActionError(
                code="action_job_snapshot_mismatch",
                message="Find queue preparation differs from its durable admission",
                action_kind=envelope.action_kind,
            ),
        )
    try:
        prepared = prepare_find_action(project, bound)
    except (TypeError, ValueError) as exc:
        return _failure_before_egress(
            project,
            envelope,
            exc.error
            if isinstance(exc, FindRefused)
            else ActionError(
                code="invalid_params",
                message=str(exc),
                action_kind=envelope.action_kind,
            ),
        )
    scan = prepared.scan
    if (
        prepared.operation.model_dump(mode="json") != admitted.get("operation")
        or _snapshot_payload(scan.sources) != admitted.get("sources")
        or scan.target != admitted.get("target")
        or scan.estimate["resolved_prompt_hash"] != admitted.get("resolved_prompt_hash")
    ):
        return _failure_before_egress(
            project,
            envelope,
            ActionError(
                code="source_changed",
                message="Find sources, target or actual scan changed after admission",
                action_kind=envelope.action_kind,
            ),
        )
    router = project_scoped_router(project, getattr(deps, "router", None))
    if not model_call_cannot_go_live(router):
        if (
            project.effective_network_policy() == "off"
            and provider_from_model_id(prepared.operation.model) != "ollama"
        ):
            return _failure_before_egress(
                project,
                envelope,
                ActionError(
                    code="network_disabled",
                    message="Project network access is disabled",
                    action_kind=envelope.action_kind,
                ),
            )
        try:
            assert_provider_spend_cap(
                project, provider_from_model_id(prepared.operation.model)
            )
        except ProviderKeyRefusal as exc:
            return _failure_before_egress(
                project,
                envelope,
                ActionError(
                    code=exc.error_code,
                    message=exc.action_message(),
                    action_kind=envelope.action_kind,
                    field=exc.field,
                    details=dict(exc.details),
                ),
            )
    result = asyncio.run(scan_find_plans(prepared.operation, scan.plans, router=router))
    action = _TypedProjectEnvelope(
        bound.action.action_id, bound.request.idempotency_key, bound.request.params
    )
    if result.issues and not result.matches:
        return _failure_after_egress(
            project,
            action,
            prepared.operation,
            project_id=envelope.project_id,
            action_id=envelope.action_id,
            receipt_id=envelope.receipt_id,
            params_hash=envelope.params_hash,
            wire_calls=result.wire_calls,
            error=ActionError(
                code="incomplete_scan",
                message="No grounded findings could be published because the scan was incomplete",
                action_kind=action.kind,
                details={
                    "window_count": len(scan.plans),
                    "issue_codes": [issue.code for issue in result.issues],
                },
            ),
        )
    return write_find_result(
        project,
        action,
        prepared.operation,
        result,
        project_id=envelope.project_id,
        action_id=envelope.action_id,
        receipt_id=envelope.receipt_id,
        params_hash=envelope.params_hash,
        admitted=admitted,
    )


def run_typed_find_action(project, project_id, bound, router=None):
    del project, router
    return _failed_result(
        project_id=project_id,
        action_kind=bound.action.action_id,
        error=ActionError(
            code="action_job_required",
            message="Find must execute through its queued action job",
            action_kind=bound.action.action_id,
        ),
    )
