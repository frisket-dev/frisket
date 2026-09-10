"""Action-preview job service.

Ties the executor's resolve+precheck (``resolve_map_preview``) to the in-memory
job registry (``ActionPreviewJobRegistry``) and ``MapRunner.preview``. The POST
handler resolves the action, runs the synchronous guards (so cost-gate /
missing-key / cap violations become a 4xx before any job exists), then spawns a
daemon job that computes the sample in memory. GET polls; DELETE cancels.
Output values stay ephemeral; paid previews retain receipt-owned call facts.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from frisket.contracts.action import ActionError as V1ActionError
from frisket.contracts.action import Receipt, ReceiptIO
from frisket.ai.models.metadata import model_calls_cost_actual
from frisket.actions.system import action_id_from_request
from frisket.actions.core import CreateSheet
from frisket.actions.registry import ACTION_REGISTRY, NEW_ACTION_IDS
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ActionRequest
from frisket.authoring.workbench.installed_actions import (
    bind_installed_action,
    resolve_installed_action,
)
from frisket.authoring.action_metadata import (
    action_available_in_edition,
    action_edition_unavailable_message,
)
from frisket.engine.executor import (
    ExecutorDeps,
    resolve_map_preview,
)
from frisket.engine.executor.column_transform_action import ColumnTransformPreviewPlan
from frisket.engine.executor.action_support import _new_id, _routed_call_provider_use
from frisket.engine.runner import validation
from frisket.engine.runner.network_policy import row_effect_spends_or_meters
from frisket.engine.runner.confirmation_echo import refuse_unless_exact_echo
from frisket.engine.runner.preview import _has_exact_free_local_terms
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from frisket.execution.attempt import claim, set_attempt_state
from frisket.execution.attempt_authority import AttemptAuthority, attempt_identity
from frisket.ops.base import RecipeInvocationHalt
from frisket.execution.resolve_for_action import (
    consented_set_hash,
    exact_match_consent_covers,
    gate_claims_payload,
    project_uncovered_user_claims,
    record_resolved_execution,
)
from frisket.engine.store.execution_routes import CONSENT_BASIS_EXACT_MATCH
from frisket.engine.executor.table_preview import (
    PreviewFile,
    TablePreviewResult,
    preview_table_source,
    table_preview_refusal,
)
from frisket.engine.runner.map_runner import (
    BatchRowLimitExceeded,
    CostGate,
    EmptyInputColumns,
    InvalidTargetRows,
    InvalidTargetSheet,
    ProviderKeyRefusal,
    NetworkDisabled,
    PreviewColumn,
    PreviewEffectRequiresRun,
    PreviewResult,
    PreviewRowCapError,
)
from frisket.server.services.action_preview_jobs import (
    ActionPreviewJobRegistry,
    PreviewJob,
)
from frisket.server.services.scratch_action_preview import (
    ScratchActionPreviewPlan,
    ScratchActionPreviewRunContext,
)
from frisket.execution.resolve_for_action import SCRATCH_PREVIEW_UNIT_ID
from frisket.server.workspace import Workspace

PREVIEW_SCHEMA_VERSION = "frisket.action_preview.v1"


@dataclass(frozen=True)
class ActionPreviewRunResponse:
    status_code: int
    payload: dict[str, Any]


class ActionPreviewRunService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        registry: ActionPreviewJobRegistry | None = None,
    ) -> None:
        self._workspace = workspace
        self._registry = registry or ActionPreviewJobRegistry()

    def prepare_transcribe_scratch(
        self,
        project_id: str,
        media_bytes: bytes,
        payload: dict[str, Any],
        *,
        request_context: Any = None,
    ):
        from frisket.server.services.scratch_transcribe import (
            paid_transcribe_scratch_plan,
        )

        project = self._workspace.get(project_id)
        router = self._workspace.action_execution_router_for(project)
        execution_context = self._workspace.edition_execution_composition_context_for(
            request_context
        )
        composition = self._workspace.execution_composition_for(
            project, router, execution_context
        )
        plan, source = paid_transcribe_scratch_plan(
            project,
            media_bytes=media_bytes,
            payload=payload,
            composition=composition,
        )
        if self._workspace.executor_deps_factory is not None:
            plan = replace(
                plan,
                consent_coverage=self._workspace.executor_deps_factory(
                    project_id, request_context
                ).consent_coverage,
            )
        return plan, source, router, composition, execution_context

    def prepare_ocr_scratch(
        self,
        project_id: str,
        media_bytes: bytes,
        payload: dict[str, Any],
        *,
        request_context: Any = None,
    ):
        from frisket.server.services.scratch_ocr import paid_ocr_scratch_plan

        project = self._workspace.get(project_id)
        router = self._workspace.action_execution_router_for(project)
        execution_context = self._workspace.edition_execution_composition_context_for(
            request_context
        )
        composition = self._workspace.execution_composition_for(
            project, router, execution_context
        )
        plan, source = paid_ocr_scratch_plan(
            project,
            media_bytes=media_bytes,
            payload=payload,
            composition=composition,
        )
        if self._workspace.executor_deps_factory is not None:
            plan = replace(
                plan,
                consent_coverage=self._workspace.executor_deps_factory(
                    project_id, request_context
                ).consent_coverage,
            )
        return plan, source, router, composition, execution_context

    def scratch_estimate(
        self, project_id: str, plan: ScratchActionPreviewPlan
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        resolved, estimate, _uncovered = _scratch_claims(project, plan)
        del resolved
        return estimate

    def start_scratch_preview(
        self,
        project_id: str,
        plan: ScratchActionPreviewPlan,
        *,
        confirmation: str | None,
        router: Any,
        composition: Any,
        execution_context: Any,
    ) -> ActionPreviewRunResponse:
        project = self._workspace.get(project_id)
        resolved, estimate, uncovered = _scratch_claims(project, plan)
        expected_hash = consented_set_hash(resolved.promise_set)
        if uncovered:
            refusal = refuse_unless_exact_echo(
                confirmed=confirmation is not None,
                echoed_hash=confirmation,
                expected_hash=expected_hash,
                refuse=lambda: _error_response(
                    402,
                    _preview_error(
                        "cost_gate",
                        "Scratch preview requires confirmation of its exact claims.",
                        plan.action_kind,
                        details={
                            "estimate": estimate,
                            "claims": estimate.get("claims", []),
                            "promise_set_hash": expected_hash,
                        },
                    ).model_copy(update={"needs_confirmation": True}),
                ),
            )
            if refusal is not None:
                return refusal

        has_user_claims = any(
            p.audience == "user_claim" for p in resolved.promise_set.promises
        )
        consent_required = has_user_claims
        from frisket.engine.store.execution_routes import CONSENT_BASIS_PREAPPROVED

        consent_basis = (
            None
            if uncovered
            else CONSENT_BASIS_EXACT_MATCH
            if exact_match_consent_covers(
                project,
                plan.spec,
                resolved.promise_set,
                consent_coverage=plan.consent_coverage,
            )
            else CONSENT_BASIS_PREAPPROVED
        )
        receipt = None
        if has_user_claims:
            receipt = Receipt(
                receipt_id=_new_id("receipt"),
                project_id=project_id,
                action_id=_new_id("act"),
                action_kind=plan.action_kind,
                params_hash=attempt_identity(plan.recipe, plan.spec),
                status="running",
                inputs=[ReceiptIO(name="preview", ref={"kind": "action_preview"})],
            )
            ReceiptStore(project).insert_running(
                receipt, edition_run_context=execution_context.edition_snapshot
            )
            record_resolved_execution(
                project,
                None,
                resolved,
                receipt_id=receipt.receipt_id,
                spec=plan.spec,
                consent_required=consent_required,
                consent_basis=consent_basis,
                consent_coverage=plan.consent_coverage,
            )
        run_store = RunResultStore(project)

        def run(progress_cb, cancel_event: threading.Event) -> TablePreviewResult:
            attempt = None
            failure = None
            try:
                if receipt is not None:
                    attempt = AttemptAuthority(
                        project,
                        composition=composition,
                        consent_coverage=plan.consent_coverage,
                    ).mint(
                        receipt_id=receipt.receipt_id,
                        recipe=plan.recipe,
                        spec=plan.spec,
                        scope=(SCRATCH_PREVIEW_UNIT_ID,),
                        prepared_work_scope=plan.work_scope,
                    )
                    claim(project, attempt, claimless_direct_effect=True)
                return asyncio.run(
                    plan.run(
                        ScratchActionPreviewRunContext(
                            project=project,
                            resolved_execution=resolved,
                            receipt_id=receipt.receipt_id
                            if receipt is not None
                            else None,
                            attempt=attempt,
                            router=router,
                            credential_use_context=composition.credential_use_context,
                            execution_limits=composition.limits,
                            progress=progress_cb,
                            cancel_event=cancel_event,
                        )
                    )
                )
            except BaseException as exc:
                failure = exc
                raise
            finally:
                status = (
                    "failed"
                    if isinstance(failure, SandboxTeardownError)
                    else "cancelled"
                    if cancel_event.is_set()
                    else "failed"
                    if failure is not None
                    else "completed"
                )
                if receipt is not None:
                    try:
                        self._finish_receipt(
                            project,
                            project_id,
                            receipt,
                            run_store,
                            status,
                            attempt,
                            stamp_scratch_unit=True,
                            scratch_failure=failure,
                        )
                    except BaseException as finalization_error:
                        if isinstance(failure, SandboxTeardownError):
                            raise failure from finalization_error
                        raise

        try:
            job = self._registry.start(
                project_id,
                1,
                run,
                **({"receipt": receipt} if receipt is not None else {}),
            )
        except BaseException:
            if receipt is not None:
                ReceiptStore(project).delete_running(receipt.receipt_id)
            raise
        return ActionPreviewRunResponse(
            status_code=202,
            payload={
                "schema_version": PREVIEW_SCHEMA_VERSION,
                "preview_id": job.id,
                "total": 1,
            },
        )

    def _finish_receipt(
        self,
        project,
        project_id,
        receipt,
        run_store,
        status,
        attempt=None,
        *,
        stamp_scratch_unit: bool = False,
        scratch_failure: BaseException | None = None,
    ) -> None:
        calls = run_store.model_calls(receipt_id=receipt.receipt_id)
        provider_use = [
            item
            for capability in sorted({call["capability"] for call in calls})
            for item in _routed_call_provider_use(calls, capability=capability)
        ]
        finished = receipt.model_copy(
            update={"status": status, "provider_use": provider_use}
        )
        with project.db:
            if attempt is not None and stamp_scratch_unit:
                attempt_calls = [
                    call for call in calls if call["attempt_id"] == attempt.attempt_id
                ]
                terminal_outcome = None
                # Cancelling the preview UI cannot undo a completed provider call.
                # Rate the work's outcome, independently of the job's status.
                if scratch_failure is None:
                    terminal_outcome = "succeeded"
                elif (
                    status == "cancelled"
                    and not attempt_calls
                    and isinstance(scratch_failure, RecipeInvocationHalt)
                    and scratch_failure.code == "local_session_failed"
                ):
                    terminal_outcome = "cancelled"
                elif attempt_calls:
                    terminal_outcome = "failed"
                if terminal_outcome is not None:
                    project.db.execute(
                        "UPDATE attempt_row_authorizations SET terminal_outcome=? "
                        "WHERE attempt_id=? AND row_id=? AND terminal_outcome IS NULL",
                        (terminal_outcome, attempt.attempt_id, SCRATCH_PREVIEW_UNIT_ID),
                    )
            if attempt is not None:
                set_attempt_state(
                    project,
                    attempt.attempt_id,
                    "effected" if status == "completed" else "halted",
                    commit=False,
                )
            if not ReceiptStore(project).update_body_status(
                finished, require_status="running", commit=False
            ):
                raise RuntimeError("preview lost its running receipt")
        receipt.status = finished.status
        receipt.provider_use = finished.provider_use
        port = self._workspace.direct_action_receipt_settlement_port
        if port is not None:
            port.settle_action_receipt(
                project=project,
                project_id=project_id,
                receipt_id=receipt.receipt_id,
            )

    def start_preview(
        self,
        project_id: str,
        body: dict[str, Any],
        *,
        request_context: Any = None,
        on_finished: Callable[[], None] | None = None,
    ) -> ActionPreviewRunResponse:
        kind = action_id_from_request(body) if isinstance(body, dict) else None
        if not action_available_in_edition(kind, self._workspace.edition):
            return _error_response(
                400,
                _preview_error(
                    "action_unavailable_in_edition",
                    action_edition_unavailable_message(kind, self._workspace.edition),
                    kind if isinstance(kind, str) else None,
                    field="kind",
                ),
            )
        project = self._workspace.get(project_id)
        router = self._workspace.action_execution_router_for(project)
        request_deps = (
            self._workspace.executor_deps_factory(project_id, request_context)
            if self._workspace.executor_deps_factory
            else ExecutorDeps()
        )
        execution_context = self._workspace.edition_execution_composition_context_for(
            request_context
        )
        deps = replace(
            request_deps,
            execution_composition=self._workspace.execution_composition_for(
                project,
                router,
                execution_context,
            ),
        )
        try:
            from frisket.engine.executor.table_action import builtin_table_source

            table_source = None
            registered = None
            if kind in NEW_ACTION_IDS:
                registered = ACTION_REGISTRY.get(kind)
            elif isinstance(body.get("action_id"), str):
                installed = resolve_installed_action(project, kind)
                if installed is not None:
                    registered = installed[0]
            if registered is not None and isinstance(
                registered.definition.run, CreateSheet
            ):
                typed = (
                    typed_action_for_request(body)
                    if kind in NEW_ACTION_IDS
                    else bind_installed_action(
                        project, ActionRequest.model_validate(body)
                    )
                )
                assert typed is not None
                refusal = table_preview_refusal(typed)
                if refusal is not None:
                    return _error_response(400, refusal)

                table_source = builtin_table_source(
                    project, project_id, typed, deps=deps
                )
            if table_source is not None:

                def run_table(progress_cb, cancel_event):
                    return preview_table_source(
                        table_source,
                        progress=progress_cb,
                        cancelled=cancel_event.is_set,
                    )

                job = self._registry.start(
                    project_id, None, run_table, on_finished=on_finished
                )
                return ActionPreviewRunResponse(
                    status_code=202,
                    payload={
                        "schema_version": PREVIEW_SCHEMA_VERSION,
                        "preview_id": job.id,
                        "total": None,
                    },
                )
            plan = resolve_map_preview(project, body, router=router, deps=deps)
        except (KeyError, TypeError, ValueError) as exc:
            code = getattr(exc, "code", "invalid_action_request")
            return _error_response(400, _preview_error(code, str(exc), kind))
        if isinstance(plan, V1ActionError):
            return _error_response(_status_for_action_error(plan), plan)

        if isinstance(plan, ColumnTransformPreviewPlan):

            def run_column_transform(progress_cb, cancel_event) -> PreviewResult:
                if cancel_event.is_set():
                    raise ValueError("preview cancelled")
                result = plan.run(cancel_event)
                progress_cb(plan.total, plan.total)
                return result

            job = self._registry.start(
                project_id,
                plan.total,
                run_column_transform,
                on_finished=on_finished,
            )
            return ActionPreviewRunResponse(
                status_code=202,
                payload={
                    "schema_version": PREVIEW_SCHEMA_VERSION,
                    "preview_id": job.id,
                    "total": plan.total,
                },
            )

        # Compatibility for injected/legacy plan doubles that predate canonical
        # row scopes. Real MapPreviewPlan instances always define the field.
        semantic_scope_total = getattr(plan, "semantic_scope_total", None)
        program = getattr(plan, "program", None)
        program_kwargs = {} if program is None else {"program": program}
        runner = plan.make_runner()
        # The typed planner carries the exact token; normal run lifecycles
        # supply this separate admission flag at dispatch. Preview owns that
        # same boundary, without a second consent representation.
        plan.runner_spec["confirmed"] = bool(
            plan.runner_spec.get("consented_promise_set_hash")
        )
        # Guards run SYNCHRONOUSLY here so a cost-gate / missing-key / cap
        # violation is a 4xx on THIS request, not a hung or errored job.
        try:
            prepared = runner.prepare_preview(
                plan.runner_spec,
                allow_empty_scope=semantic_scope_total is not None,
                accounted=True,
                **program_kwargs,
            )
            sample_total = len(prepared.row_ids)
        except PreviewRowCapError as exc:
            return _error_response(
                400,
                _preview_error("preview_row_cap", str(exc), plan.action_kind),
            )
        except BatchRowLimitExceeded as exc:
            return _error_response(
                400,
                _preview_error(
                    "batch_row_limit_exceeded",
                    str(exc),
                    plan.action_kind,
                    details={"row_count": exc.row_count, "max_rows": exc.max_rows},
                ),
            )
        except PreviewEffectRequiresRun as exc:
            return _error_response(
                400,
                _preview_error(
                    "preview_effect_requires_run",
                    str(exc),
                    plan.action_kind,
                    field="kind",
                    details={"requires_durable_run": True},
                ),
            )
        except CostGate as exc:
            from frisket.engine.executor.action_support import _claims_gate_details

            return _error_response(
                402,
                _preview_error(
                    "cost_gate",
                    str(exc),
                    plan.action_kind,
                    # Claims and promise_set_hash ride additively when the
                    # gate is a ClaimsGate (empty otherwise).
                    details={
                        "estimate": exc.estimate_details or exc.estimate,
                        **_claims_gate_details(exc),
                    },
                ).model_copy(update={"needs_confirmation": True}),
            )
        except ProviderKeyRefusal as exc:
            # See the refusal-seat table at action_runs._v1_action_result_http_status.
            # Any refusal about the provider key this preview would spend
            # through; the exception owns its code, copy, and details.
            return _error_response(
                400,
                _preview_error(
                    exc.error_code,
                    exc.action_message(),
                    plan.action_kind,
                    field=exc.field,
                    details=dict(exc.details),
                ),
            )
        except NetworkDisabled as exc:
            return _error_response(
                400,
                _preview_error(
                    "network_disabled",
                    str(exc),
                    plan.action_kind,
                    details={"capability": exc.capability},
                ),
            )
        except InvalidTargetSheet as exc:
            return _error_response(
                400,
                _preview_error(
                    "invalid_input_ref",
                    str(exc),
                    plan.action_kind,
                    field="row_scope.sheet_id",
                    details={"sheet_id": exc.sheet_id},
                ),
            )
        except InvalidTargetRows as exc:
            return _error_response(
                400,
                _preview_error(
                    "invalid_input_ref",
                    str(exc),
                    plan.action_kind,
                    field="row_scope.selector.membership.row_ids",
                    details={"missing": exc.missing},
                ),
            )
        except EmptyInputColumns as exc:
            return _error_response(
                400,
                _preview_error(
                    "empty_input_column",
                    str(exc),
                    plan.action_kind,
                    field=(
                        program.empty_input_error_field
                        if program is not None
                        else "params.input_columns"
                    ),
                    details={"columns": exc.columns},
                ),
            )
        except ValueError as exc:
            return _error_response(
                400,
                _preview_error("invalid_preview", str(exc), plan.action_kind),
            )

        runner_spec = plan.runner_spec
        accounted = row_effect_spends_or_meters(
            prepared.recipe, runner_spec, runner.router
        ) and not _has_exact_free_local_terms(
            prepared.recipe, runner_spec, prepared.est
        )
        receipt = None
        if accounted:
            receipt = Receipt(
                receipt_id=_new_id("receipt"),
                project_id=project_id,
                action_id=_new_id("act"),
                action_kind=plan.action_kind,
                params_hash=attempt_identity(prepared.recipe, runner_spec),
                status="running",
                inputs=[ReceiptIO(name="preview", ref={"kind": "action_preview"})],
            )
            ReceiptStore(project).insert_running(
                receipt, edition_run_context=execution_context.edition_snapshot
            )

        def finish_receipt(status, attempt=None):
            if receipt is None:
                return
            self._finish_receipt(
                project, project_id, receipt, runner.run_store, status, attempt
            )

        def run(progress_cb, cancel_event: threading.Event) -> PreviewResult:
            # A fresh thread has no running event loop, so asyncio.run is safe
            # (same reasoning as the executor's synchronous run path).
            preview_kwargs = {
                "progress_cb": progress_cb,
                "cancel_event": cancel_event,
            }
            if semantic_scope_total is not None:
                preview_kwargs["allow_empty_scope"] = True
            attempt = None
            failure = None
            try:
                if receipt is not None:
                    validation.persist_resolved_execution(
                        project,
                        runner_spec,
                        prepared,
                        None,
                        receipt_id=receipt.receipt_id,
                    )
                    attempt = AttemptAuthority(
                        project,
                        composition=runner.execution_composition,
                        consent_coverage=runner.consent_coverage,
                    ).mint(
                        receipt_id=receipt.receipt_id,
                        recipe=prepared.recipe,
                        spec=runner_spec,
                        scope=tuple(prepared.row_ids),
                    )
                    claim(project, attempt, claimless_direct_effect=True)
                    preview_kwargs["attempt"] = attempt
                result = asyncio.run(
                    runner.preview(runner_spec, **program_kwargs, **preview_kwargs)
                )
            except BaseException as exc:
                failure = exc
                raise
            finally:
                # asyncio.run has joined all runner work and capability scope
                # finalizers. Only now may receipt-scoped writers lose authority.
                status = (
                    "failed"
                    if isinstance(failure, SandboxTeardownError)
                    else "cancelled"
                    if cancel_event.is_set()
                    else "failed"
                    if failure is not None
                    else "completed"
                )
                try:
                    finish_receipt(status, attempt)
                except BaseException as finalization_error:
                    if isinstance(failure, SandboxTeardownError):
                        raise failure from finalization_error
                    raise
            if semantic_scope_total is not None:
                result = replace(result, total=semantic_scope_total)
            return result

        total = (
            semantic_scope_total if semantic_scope_total is not None else sample_total
        )
        try:
            job = self._registry.start(
                project_id,
                total,
                run,
                **({"receipt": receipt} if receipt is not None else {}),
                on_finished=on_finished,
            )
        except BaseException:
            finish_receipt("failed")
            raise
        return ActionPreviewRunResponse(
            status_code=202,
            payload={
                "schema_version": PREVIEW_SCHEMA_VERSION,
                "preview_id": job.id,
                "total": total,
            },
        )

    def get_preview(self, project_id: str, preview_id: str) -> ActionPreviewRunResponse:
        project = self._workspace.get(project_id)
        job = self._registry.get(project_id, preview_id)
        if job is None:
            return _error_response(
                404,
                _preview_error(
                    "preview_not_found",
                    "No preview exists with that id (it may have expired).",
                    None,
                    field="preview_id",
                ),
            )
        payload = _job_payload(job)
        if job.receipt is not None:
            receipt = ReceiptStore(project).parsed_by_id(job.receipt.receipt_id)
            if receipt is None:
                raise RuntimeError("preview receipt is missing")
            calls = RunResultStore(project).model_calls(receipt_id=receipt.receipt_id)
            payload["accounting"] = {
                "receipt_id": receipt.receipt_id,
                "status": receipt.status,
                "model_call_count": len(calls),
                "cost_actual": model_calls_cost_actual(calls),
                "elapsed_ms": max(
                    0,
                    round(
                        ((job.finished_at or time.monotonic()) - job.created_at) * 1000
                    ),
                ),
            }
        return ActionPreviewRunResponse(
            status_code=200,
            payload=payload,
        )

    def cancel_preview(
        self, project_id: str, preview_id: str
    ) -> ActionPreviewRunResponse:
        self._workspace.get(project_id)
        # Idempotent: cancelling an unknown/finished preview is still a no-op 204.
        self._registry.cancel(project_id, preview_id)
        return ActionPreviewRunResponse(status_code=204, payload={})

    def open_preview_file(self, project_id: str, preview_id: str, artifact_id: str):
        self._workspace.get(project_id)
        job = self._registry.get(project_id, preview_id)
        result = job.result if job is not None else None
        if (
            job is None
            or job.status != "done"
            or not isinstance(result, TablePreviewResult)
        ):
            return None
        artifact = result.artifacts.get(artifact_id)
        if artifact is None:
            return None
        try:
            # Open before response streaming: expiry can unlink the scratch path,
            # but an already-open download still owns its bytes until it closes.
            return artifact.path.open("rb"), artifact
        except FileNotFoundError:
            return None


def _job_payload(job: PreviewJob) -> dict[str, Any]:
    result = job.result
    payload: dict[str, Any] = {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "preview_id": job.id,
        "status": job.status,
        "progress": dict(job.progress),
    }
    if job.status == "done" and isinstance(result, PreviewResult):
        payload["result"] = _preview_result_payload(result)
    elif job.status == "done" and isinstance(result, TablePreviewResult):

        def cell_value(value):
            from frisket.engine.executor.temporal_preview import PreviewTemporalValue

            if isinstance(value, PreviewTemporalValue):
                return value.wire_value()
            if isinstance(value, PreviewFile):
                from urllib.parse import quote

                return (
                    f"/api/projects/{quote(job.project_id, safe='')}/actions/v1/preview/"
                    f"{job.id}/artifacts/{value.id}"
                )
            if isinstance(value, list):
                return [cell_value(item) for item in value]
            return value

        payload["result"] = {
            "kind": "table",
            "columns": [_preview_column_payload(c) for c in result.columns],
            "rows": [
                {
                    name: {**cell, "value": cell_value(cell["value"])}
                    for name, cell in row.items()
                }
                for row in result.rows
            ],
            "sampled": len(result.rows),
            "total": result.total,
            "warnings": list(result.warnings),
        }
    if job.status == "error" and job.error is not None:
        payload["error"] = dict(job.error)
    return payload


def _preview_result_payload(result: PreviewResult) -> dict[str, Any]:
    payload = {
        "kind": "row_overlay",
        "sheet_id": result.sheet_id,
        "columns": [_preview_column_payload(c) for c in result.columns],
        "rows": {str(row_id): cells for row_id, cells in result.values.items()},
        "row_ids": list(result.row_ids),
        "sampled": result.sampled,
        "total": result.total,
    }
    return payload


def _preview_column_payload(column: PreviewColumn) -> dict[str, Any]:
    return {
        "name": column.name,
        "column_type": column.column_type,
        "format": column.format,
        "hidden": column.hidden,
        "overwrites_column_id": column.overwrites_column_id,
    }


def _scratch_claims(project, plan: ScratchActionPreviewPlan):
    from frisket.contracts.http.action_estimate_validation import ActionEstimate
    from frisket.engine.runner.confirmation_context import rate_estimate
    from frisket.engine.runner.validation import _bind_rated_quote
    from frisket.execution.pricing_policy import default_pricing_policy

    estimate = rate_estimate(plan.estimate, policy=default_pricing_policy())
    resolved = _bind_rated_quote(plan.resolved_execution, estimate)
    uncovered = project_uncovered_user_claims(
        project,
        resolved.promise_set,
        spec=plan.spec,
        consent_coverage=plan.consent_coverage,
    )
    payload = {
        **estimate,
        "rows": 1,
        "requires_confirmation": bool(uncovered),
    }
    if uncovered:
        payload["claims"] = gate_claims_payload(
            uncovered,
            resolved.resolution.facts,
            resolved.presentation,
            has_cost_claim=any(
                promise.field == "cost" for promise in resolved.promise_set.promises
            ),
        )
        payload["promise_set_hash"] = consented_set_hash(resolved.promise_set)
    return (
        resolved,
        ActionEstimate.model_validate(payload).model_dump(
            mode="json", exclude_none=True
        ),
        uncovered,
    )


def _preview_error(
    code: str,
    message: str,
    action_kind: str | None,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> V1ActionError:
    return V1ActionError(
        code=code,
        message=message,
        action_kind=action_kind,
        field=field,
        details=dict(details or {}),
    )


def _status_for_action_error(error: V1ActionError) -> int:
    if error.code in {"unsupported_action_kind"}:
        return 400
    return 400


def _error_response(status_code: int, error: V1ActionError) -> ActionPreviewRunResponse:
    return ActionPreviewRunResponse(
        status_code=status_code,
        payload={
            "schema_version": PREVIEW_SCHEMA_VERSION,
            "error": error.model_dump(mode="json"),
        },
    )
