"""Shared v1 queued action dispatch metadata.

HTTP enqueue and worker finalization both need the same action-kind inventory:
params model, queue reservation helpers, finalizer, payload metadata, and any
stale-input guard. Keep that table here so adding a queued action is one edit.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel

from frisket.actions.core import ModelRowsEvaluationContext

from frisket.contracts.action import (
    ActionError,
    ActionResult,
    Receipt,
)
from frisket.engine.executor.action_inventory import (
    _ActionExecutionEnvelope,
    _QueuedActionInventoryEntry,
    _QueuedPayloadCodec,
)
from frisket.engine.executor.action_receipts import receipt_status_from_failures
from frisket.engine.executor.action_reservations import (
    QUEUED_ACTION_RUN_MARKER_PARAM,
    _queued_action_lifecycle_adapter,
    _queued_action_run_marker,
    _queued_receipt_job_id,
    _queued_receipt_prepared_attempt_id,
)
from frisket.engine.executor.action_specs import ResolvedAction
from frisket.engine.executor.project_run_terminalization import (
    AbandonedAttemptRecoveryAuthority,
    AbandonedRunRecoveryAuthority,
    CurrentWriterTerminalAuthority,
    NeverDispatchedRunTerminalAuthority,
    PreparedRunTerminalAuthority,
    ProjectRunTerminalizationResult,
    ReceiptOnlyOrphanAuthority,
    TerminalRunReceiptRepairAuthority,
    UnclaimedRunTerminalAuthority,
    terminalize_project_run,
)
from frisket.redaction import safe_error
from frisket.engine.runner import CostGate
from frisket.ops.base import ActionProgram
from frisket.engine.store import Project
from frisket.engine.store.document_text import DocumentTextPublicationRefused
from frisket.engine.store.receipts import ReceiptStatus, ReceiptStore

LOG = logging.getLogger("frisket.worker")

QueuedPreRunGuard = Callable[
    [Project, Mapping[str, Any], BaseModel], ActionError | None
]
TerminalQueuedStatus = Literal["failed", "cancelled"]


@dataclass(frozen=True)
class QueuedV1TerminalReceiptTransition:
    action_result: ActionResult
    terminalization: ProjectRunTerminalizationResult


@dataclass(frozen=True)
class QueuedV1ActionEntry:
    kind: str
    params_model: type[BaseModel]
    reserve_action: Callable[..., dict[str, Any] | ActionResult]
    reserve_action_in_transaction: Callable[..., dict[str, Any] | ActionResult]
    requires_atomic_publication: Callable[[BaseModel], bool]
    cleanup_reservation: Callable[..., None]
    mark_runner_spec: Callable[..., dict[str, Any]]
    mark_prepared: Callable[..., None]
    mark_enqueued: Callable[..., None]
    finalize_action: Callable[..., ActionResult]
    payload_codecs: tuple[_QueuedPayloadCodec, ...]
    pre_run_guard: QueuedPreRunGuard | None = None
    cost_gate_error_fn: (
        Callable[[_ActionExecutionEnvelope, CostGate], ActionError] | None
    ) = None
    map_error_code: str = "map_run_failed"
    output_claim_error_field: str = "params.output_name"

    @property
    def payload_keys(self) -> tuple[str, ...]:
        return tuple(codec.key for codec in self.payload_codecs)

    def prepare_cost_gate_error(
        self,
        action: _ActionExecutionEnvelope,
        exc: CostGate,
    ) -> ActionError:
        if self.cost_gate_error_fn is not None:
            return self.cost_gate_error_fn(action, exc)
        return self.prepare_run_error(action, exc)

    def prepare_run_error(
        self,
        action: _ActionExecutionEnvelope,
        exc: Exception,
    ) -> ActionError:
        return ActionError(
            code=self.map_error_code,
            message=safe_error(self.map_error_code, exc).detail,
            action_kind=action.kind,
        )


@dataclass(frozen=True)
class QueuedV1ActionRequest:
    entry: QueuedV1ActionEntry
    action: _ActionExecutionEnvelope
    params: BaseModel
    program: ActionProgram | None = None
    expected_runner_spec: Mapping[str, Any] | None = None
    expected_params_hash: str | None = None


@dataclass(frozen=True)
class QueuedV1PayloadEnvelope:
    entry: QueuedV1ActionEntry
    action: _ActionExecutionEnvelope
    params: BaseModel
    program: ActionProgram | None
    action_body: dict[str, Any]
    action_id: str
    receipt_id: str
    params_hash: str
    runner_spec: dict[str, Any]
    payload: Mapping[str, Any]

    def finalize_kwargs(
        self,
        *,
        project_id: str,
        run_id: int,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
        terminal_status: str | None = None,
        receipt_status: str | None = None,
    ) -> dict[str, Any]:
        kwargs = {
            "runner_spec": self.runner_spec,
            "params_hash": self.params_hash,
            "project_id": project_id,
            "action_id": self.action_id,
            "receipt_id": self.receipt_id,
            "run_id": run_id,
        }
        # The worker decodes the op's payload codecs (input_column_ids, or
        # media's input_column/row_ids/blob_refs) and threads them as a single
        # ResolvedAction, the same shape the sync path passes.
        decoded = queued_v1_finalize_kwargs(self.entry, self.payload)
        kwargs["resolved"] = ResolvedAction.from_resolve_dict(decoded)
        if terminal_status is not None:
            kwargs["terminal_status"] = terminal_status
        if receipt_status is not None:
            kwargs["receipt_status"] = receipt_status
        if writer_attempt_id is not None:
            kwargs["writer_attempt_id"] = writer_attempt_id
            kwargs["claim_token"] = claim_token
        return kwargs

    def pre_run_error(self, project: Project) -> ActionError | None:
        from frisket.actions.registry import ACTION_REGISTRY
        from frisket.authoring.workbench.installed_actions import (
            resolve_installed_action,
        )
        from frisket.execution.action_admission import action_admission_error

        installed = resolve_installed_action(project, self.action.kind)
        action = (
            installed[0]
            if installed is not None
            else ACTION_REGISTRY.get(self.action.kind)
        )
        refusal = action_admission_error(
            project,
            action,
            runtime_binding=installed[1] if installed is not None else None,
        )
        if refusal is not None:
            return refusal
        if self.entry.pre_run_guard is None:
            return None
        return self.entry.pre_run_guard(project, self.payload, self.params)

    def finalize_action_result(
        self,
        project: Project,
        *,
        project_id: str,
        run_id: int,
        writer_attempt_id: str | None = None,
        claim_token: str | None = None,
    ) -> ActionResult:
        run = project.db.execute(
            "SELECT status,total_rows,failed_rows FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        terminal_status = "completed"
        receipt_status = "completed"
        if run is not None:
            run_status = str(run["status"])
            if run_status in {"completed", "partial", "failed", "cancelled"}:
                terminal_status = run_status
                receipt_status = run_status
            else:
                receipt_status = receipt_status_from_failures(
                    int(run["failed_rows"] or 0), int(run["total_rows"] or 0)
                )
        return self.entry.finalize_action(
            project,
            self.action,
            self.params,
            **self.finalize_kwargs(
                project_id=project_id,
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
                terminal_status=terminal_status,
                receipt_status=receipt_status,
            ),
        )


def _queued_v1_action_entry(
    item: _QueuedActionInventoryEntry,
) -> QueuedV1ActionEntry:
    spec = item.spec
    lifecycle = _queued_action_lifecycle_adapter(spec)
    finalize_action = item.finalize_action or spec.finalize_action
    if finalize_action is None:
        raise ValueError(f"queued action {spec.kind} must define a finalizer")
    return QueuedV1ActionEntry(
        kind=spec.kind,
        params_model=spec.params_model,
        reserve_action=lifecycle.reserve,
        reserve_action_in_transaction=lifecycle.reserve_in_transaction,
        requires_atomic_publication=lifecycle.requires_atomic_publication,
        cleanup_reservation=lifecycle.cleanup_reservation,
        mark_runner_spec=lifecycle.mark_runner_spec,
        mark_prepared=lifecycle.mark_prepared,
        mark_enqueued=lifecycle.mark_enqueued,
        finalize_action=finalize_action,
        payload_codecs=spec.payload_codecs,
        pre_run_guard=spec.pre_run_guard,
        cost_gate_error_fn=spec.cost_gate_error_fn,
        map_error_code=spec.map_error_code,
        output_claim_error_field=spec.output_claim_error_field,
    )


def queued_v1_action_request(
    action_body: Mapping[str, Any],
    *,
    project: Project | None = None,
    payload: Mapping[str, Any] | None = None,
    initial_typed_plan: Any | None = None,
) -> QueuedV1ActionRequest | None:
    if not isinstance(action_body, Mapping):
        return None
    if isinstance(action_body.get("action_id"), str):
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.action_dispatch import placement_for_kind
        from frisket.engine.executor.action_specs import PlacementPolicy
        from frisket.engine.executor.map_rows_action import typed_queued_map_spec

        action_id = str(action_body["action_id"])
        try:
            bound = None
            if project is not None:
                from frisket.actions.types import ActionRequest
                from frisket.authoring.workbench.installed_actions import (
                    bind_installed_action,
                )

                bound = bind_installed_action(
                    project, ActionRequest.model_validate(action_body)
                )
            if bound is None:
                if (
                    placement_for_kind(action_id)
                    is not PlacementPolicy.QUEUED_PROJECT_RUN
                ):
                    return None
                bound = typed_action_for_request(dict(action_body))
            else:
                from frisket.actions.core import CreateSheet

                if bound.action.catalog_entry()["async_mode"] != "queued" or isinstance(
                    bound.action.definition.run, CreateSheet
                ):
                    return None
            assert bound is not None
            from frisket.actions.types import SheetRows

            if (
                isinstance(bound.request.scope, SheetRows)
                and bound.request.scope.selection is not None
            ):
                from .selection_scope import (
                    freeze_action_selection,
                    resolve_action_selection,
                )

                if payload is not None:
                    frozen = payload.get("spec", {}).get("row_ids")
                    if not isinstance(frozen, list):
                        return None
                    bound = freeze_action_selection(bound, frozen)
                elif initial_typed_plan is not None:
                    bound = freeze_action_selection(
                        bound, initial_typed_plan.request.scope.row_ids
                    )
                elif project is not None:
                    bound = resolve_action_selection(project, bound)
                else:
                    return None
            from frisket.actions.cluster_types import ValueClusterer

            clustering = getattr(bound.action.definition.run, "capabilities", ()) == (
                ValueClusterer,
            )
            initial_plan = initial_typed_plan
            if initial_plan is None and payload is not None and not clustering:
                from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

                output_names = payload.get("v1_output_names")
                output_target_preconditions = payload.get(
                    "v1_output_target_preconditions"
                )
                if not isinstance(output_names, Mapping) or not isinstance(
                    output_target_preconditions, Mapping
                ):
                    return None
                evaluation = payload["spec"].get("evaluation_context")
                initial_plan = _typed_map_rows_plan(
                    bound,
                    output_names={
                        str(key): str(value) for key, value in output_names.items()
                    },
                    output_target_preconditions={
                        str(key): None if value is None else int(value)
                        for key, value in output_target_preconditions.items()
                    },
                    evaluation_context=(
                        ModelRowsEvaluationContext.from_payload(evaluation)
                        if isinstance(evaluation, Mapping)
                        else None
                    ),
                )
            from frisket.actions.core import SemanticJoin

            queue_spec = typed_queued_map_spec
            semantic = isinstance(bound.action.definition.run, SemanticJoin)
            if semantic:
                from frisket.engine.executor.semantic_join_action import (
                    typed_semantic_join_queue_spec,
                )

                queue_spec = typed_semantic_join_queue_spec
            elif clustering:
                from frisket.engine.executor.cluster_action import (
                    typed_cluster_queue_spec,
                )

                queue_spec = typed_cluster_queue_spec
            action, spec, program = queue_spec(
                bound,
                initial_plan=initial_plan,
                **(
                    {
                        "semantic_state": payload.get("v1_semantic_join")
                        if payload
                        else None
                    }
                    if semantic
                    else {
                        "cluster_state": payload.get("v1_cluster_values")
                        if payload
                        else None
                    }
                    if clustering
                    else {}
                ),
            )
            entry = _queued_v1_action_entry(_QueuedActionInventoryEntry(spec=spec))
        except (KeyError, TypeError, ValueError):
            return None
        return QueuedV1ActionRequest(
            entry=entry,
            action=action,
            params=bound.params,
            program=program,
            expected_runner_spec=initial_plan.spec
            if initial_plan is not None and not (semantic or clustering)
            else spec.runner_spec_fn(bound.params),
            expected_params_hash=spec.params_hash_fn(action),
        )
    return None


def queued_v1_payload_metadata(
    entry: QueuedV1ActionEntry,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    # Every DECLARED codec key must be present in the reservation: the old
    # `if codec.key in reservation` skip meant a
    # forgotten reservation key was silently omitted from the payload, and the
    # worker's decoder then finalized against empty inputs ({}/[]) with a
    # completed status. Absence here is a wiring defect — fail the enqueue
    # loudly instead of manufacturing an empty run.
    missing = [
        codec.key for codec in entry.payload_codecs if codec.key not in reservation
    ]
    if missing:
        raise ValueError(
            f"queued action {entry.kind} reservation is missing declared "
            f"payload key(s): {', '.join(missing)}"
        )
    return {
        f"v1_{codec.key}": codec.encode(reservation[codec.key])
        for codec in entry.payload_codecs
    }


def queued_v1_job_payload(
    request: QueuedV1ActionRequest,
    action_body: Mapping[str, Any],
    reservation: Mapping[str, Any],
    *,
    run_id: int,
) -> dict[str, Any]:
    return {
        "dedupe_key": f"run:{run_id}",
        "v1_action": dict(action_body),
        "v1_action_id": reservation["action_id"],
        "action_kind": request.action.kind,
        "v1_receipt_id": reservation["receipt_id"],
        "v1_params_hash": reservation["params_hash"],
        **queued_v1_payload_metadata(request.entry, reservation),
    }


def queued_v1_payload_envelope(
    payload: Mapping[str, Any],
    *,
    project: Project | None = None,
) -> QueuedV1PayloadEnvelope | None:
    action_kind = payload.get("action_kind")
    if not isinstance(action_kind, str):
        return None
    action_body = payload.get("v1_action")
    if not isinstance(action_body, Mapping):
        return None
    request = queued_v1_action_request(action_body, project=project, payload=payload)
    if request is None or request.action.kind != action_kind:
        return None
    action_id = payload.get("v1_action_id")
    receipt_id = payload.get("v1_receipt_id")
    params_hash = payload.get("v1_params_hash")
    if not action_id or not receipt_id or not params_hash:
        return None
    runner_spec = payload.get("spec")
    if not isinstance(runner_spec, Mapping):
        return None
    if "recipe" in runner_spec or runner_spec.get("action_kind") != action_kind:
        return None
    deferred = getattr(request.program, "defer_generation_seal", False) is True
    if deferred:
        if runner_spec.get("deferred_publication") is not True:
            return None
    elif "deferred_publication" in runner_spec:
        return None
    if request.expected_runner_spec is not None:
        canonical_runner_spec = {
            key: value
            for key, value in runner_spec.items()
            if key not in {QUEUED_ACTION_RUN_MARKER_PARAM, "deferred_publication"}
        }
        expected_runner_spec = {
            key: value
            for key, value in request.expected_runner_spec.items()
            if key != "deferred_publication"
        }
        if canonical_runner_spec != expected_runner_spec:
            return None
    if (
        request.expected_params_hash is not None
        and str(params_hash) != request.expected_params_hash
    ):
        return None
    if runner_spec.get(QUEUED_ACTION_RUN_MARKER_PARAM) != _queued_action_run_marker(
        action_kind=action_kind,
        receipt_id=str(receipt_id),
        action_id=str(action_id),
        params_hash=str(params_hash),
    ):
        return None
    return QueuedV1PayloadEnvelope(
        entry=request.entry,
        action=request.action,
        params=request.params,
        program=request.program,
        action_body=dict(action_body),
        action_id=str(action_id),
        receipt_id=str(receipt_id),
        params_hash=str(params_hash),
        runner_spec=dict(runner_spec),
        payload=payload,
    )


def queued_v1_identity_error(
    payload: Mapping[str, Any], *, project: Project | None = None
) -> ActionError | None:
    """Refuse any project.run whose four action-identity stamps disagree.

    The outer payload, canonical v1 ActionSpec, runner spec, and private
    marker are all producer-owned copies of one kind. Validate them before an
    edition admission callback or MapRunner/program lookup can observe a
    different spelling.
    """

    if queued_v1_payload_envelope(payload, project=project) is not None:
        return None
    raw_kind = payload.get("action_kind")
    return ActionError(
        code="invalid_queued_action_identity",
        message=(
            "queued project.run action identity is missing, noncanonical, "
            "or inconsistent across its action, runner spec, and marker"
        ),
        action_kind=(raw_kind if isinstance(raw_kind, str) and raw_kind else None),
    )


def queued_v1_run_authorizes_action_lifecycle(
    project: Project,
    payload: Mapping[str, Any],
    *,
    run_id: int,
) -> bool:
    """Bind protected-program worker access to durable queue provenance.

    Parsing a v1-shaped payload is not authorization: the queued spec must be
    byte-for-value equal to the spec persisted on this run, its private marker
    must bind the action/receipt/hash tuple, and that receipt must still bind
    the same run. Corrupt or forged queue metadata therefore fails closed and
    lets ``MapRunner`` reject action-lifecycle-only programs.
    """
    envelope = queued_v1_payload_envelope(payload, project=project)
    if envelope is None:
        return False
    run = project.db.execute(
        "SELECT action_kind,params FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if run is None or str(run["action_kind"]) != envelope.action.kind:
        return False
    try:
        persisted_spec = json.loads(run["params"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    candidate_spec = {
        key: value for key, value in envelope.runner_spec.items() if key != "confirmed"
    }
    if not isinstance(persisted_spec, dict) or persisted_spec != candidate_spec:
        return False
    if persisted_spec.get(QUEUED_ACTION_RUN_MARKER_PARAM) != _queued_action_run_marker(
        action_kind=envelope.action.kind,
        receipt_id=envelope.receipt_id,
        action_id=envelope.action_id,
        params_hash=envelope.params_hash,
    ):
        return False
    receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    if not (
        receipt is not None
        and receipt.run_id == run_id
        and receipt.action_kind == envelope.action.kind
        and receipt.action_id == envelope.action_id
        and receipt.params_hash == envelope.params_hash
        and receipt.status in {"queued", "running"}
    ):
        return False
    from frisket.engine.runner.validation import action_program_for_spec

    program = envelope.program or action_program_for_spec(envelope.runner_spec)
    return (
        not program.consumes_resolution
        or queued_v1_prepared_attempt_id(project, payload, run_id=run_id) is not None
    )


def queued_v1_prepared_attempt_id(
    project: Project,
    payload: Mapping[str, Any],
    *,
    run_id: int,
) -> str | None:
    """Return the one exact attempt named by a linked prepared receipt."""

    envelope = queued_v1_payload_envelope(payload, project=project)
    if envelope is None:
        return None
    receipt = ReceiptStore(project).parsed_by_id(envelope.receipt_id)
    if (
        receipt is None
        or receipt.run_id != run_id
        or receipt.action_id != envelope.action_id
        or receipt.action_kind != envelope.action.kind
        or receipt.params_hash != envelope.params_hash
        or receipt.status not in {"queued", "running"}
    ):
        return None
    attempt_id = _queued_receipt_prepared_attempt_id(receipt)
    if attempt_id is None:
        return None
    attempt = project.db.execute(
        "SELECT run_id,state FROM execution_attempts WHERE id=?",
        (attempt_id,),
    ).fetchone()
    if (
        attempt is None
        or attempt["run_id"] is None
        or int(attempt["run_id"]) != run_id
        or str(attempt["state"]) != "admitted"
    ):
        return None
    return attempt_id


def queued_v1_finalize_kwargs(
    entry: QueuedV1ActionEntry,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    # Absence is a defect, not an empty value: decoding a
    # missing key as {}/[] let a payload/codec drift finalize a run against
    # empty inputs with a completed status. Raise instead — the worker's
    # exception path terminalizes the receipt as failed with the honest error.
    missing = [
        codec.key for codec in entry.payload_codecs if f"v1_{codec.key}" not in payload
    ]
    if missing:
        raise ValueError(
            f"queued action {entry.kind} payload is missing declared "
            f"key(s): {', '.join(f'v1_{key}' for key in missing)}"
        )
    return {
        codec.key: codec.decode(payload[f"v1_{codec.key}"])
        for codec in entry.payload_codecs
    }


def queued_v1_pre_run_error(
    project: Project,
    payload: Mapping[str, Any],
) -> ActionError | None:
    envelope = queued_v1_payload_envelope(payload, project=project)
    if envelope is None:
        return queued_v1_identity_error(payload, project=project)
    return envelope.pre_run_error(project)


def _terminalize_undecodable_queued_payload(
    project: Project,
    payload: Mapping[str, Any],
    *,
    project_id: str,
    run_id: int,
) -> ActionResult | None:
    """Repair a run-bound receipt after terminal-only queued schema drift.

    Envelope reconstruction is deterministic for the payload version the
    worker enqueued. If enqueue/finalize schema drift makes it undecodable only
    after the run has gone terminal, close its already run-bound receipt and
    claim with an honest failure. The terminal authority rechecks that status
    under its write lock so a concurrent resume wins and this repair refuses.
    A still-running run, or a payload without a v1 receipt id, is not repairable
    here and remains a no-op.
    """
    receipt_id = payload.get("v1_receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        return None
    run = project.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or str(run["status"]) == "running":
        return None
    terminal_run_status = str(run["status"])
    if terminal_run_status not in {"completed", "failed", "cancelled"}:
        return None
    action_kind = payload.get("action_kind")
    error = ActionError(
        code="map_run_failed",
        message=(
            "the queued action payload could not be decoded at finalize "
            "(enqueue/finalize schema drift); the run's outputs were not "
            "finalized and the receipt is recorded failed"
        ),
        action_kind=str(action_kind or "unknown"),
    )
    LOG.warning(
        "queued_v1_finalize_envelope_missing_receipt_terminalized",
        extra={
            "event": "queued_v1_finalize_envelope_missing_receipt_terminalized",
            "receipt_id": receipt_id,
            "run_id": run_id,
        },
    )
    return queued_v1_terminal_receipt_result(
        project,
        payload,
        project_id=project_id,
        run_id=run_id,
        status="failed",
        error=error,
        receipt_id=receipt_id,
        repair_terminal_run_receipt=True,
        terminal_run_status=terminal_run_status,
    )


def queued_v1_finalize_action_result(
    project: Project,
    payload: Mapping[str, Any],
    *,
    project_id: str,
    run_id: int,
    writer_attempt_id: str | None = None,
    claim_token: str | None = None,
    envelope: QueuedV1PayloadEnvelope | None = None,
) -> ActionResult | None:
    # The live worker supplies the envelope whose program actually executed.
    receipt_id = payload.get("v1_receipt_id")
    if isinstance(receipt_id, str):
        stored = ReceiptStore(project).parsed_by_id(receipt_id)
        if (
            stored is not None
            and stored.run_id == run_id
            and stored.status not in {"queued", "running"}
        ):
            # A redelivered worker observes the recorded terminal answer. It
            # must not regenerate errors from row projections or release again.
            from frisket.engine.executor.action_receipts import _result_from_receipt

            return _result_from_receipt(stored)
    # Reconstruct only for recovery paths without invocation-local resources.
    envelope = envelope or queued_v1_payload_envelope(payload, project=project)
    if envelope is None:
        return _terminalize_undecodable_queued_payload(
            project,
            payload,
            project_id=project_id,
            run_id=run_id,
        )
    return envelope.finalize_action_result(
        project,
        project_id=project_id,
        run_id=run_id,
        writer_attempt_id=writer_attempt_id,
        claim_token=claim_token,
    )


def queued_v1_terminal_receipt_transition(
    project: Project,
    payload: Mapping[str, Any] | None = None,
    *,
    project_id: str,
    run_id: int,
    status: TerminalQueuedStatus,
    error: ActionError | None = None,
    receipt_id: str | None = None,
    action_kind: str | None = None,
    action_id: str | None = None,
    job_id: int | None = None,
    writer_attempt_id: str | None = None,
    claim_token: str | None = None,
    receipt_only: bool = False,
    repair_terminal_run_receipt: bool = False,
    terminal_run_status: str | None = None,
    include_terminal_receipt_lookup: bool = False,
    raise_on_conflict: bool = True,
    require_cancel_intent: bool = False,
    claimless_direct_effect: bool = False,
    never_dispatched: bool = False,
    publication_refusal: DocumentTextPublicationRefused | None = None,
) -> QueuedV1TerminalReceiptTransition:
    if status not in {"failed", "cancelled"}:
        raise ValueError(f"unsupported terminal queued receipt status: {status}")
    payload = payload or {}
    envelope = queued_v1_payload_envelope(payload, project=project)
    action_kind = (
        envelope.action.kind
        if envelope is not None
        else str(
            action_kind
            or payload.get("action_kind")
            or (error.action_kind if error is not None else None)
            or "unknown"
        )
    )
    action_id = (
        envelope.action_id
        if envelope is not None
        else str(action_id or payload.get("v1_action_id") or "act_queued_terminal")
    )
    receipt_id = (
        envelope.receipt_id
        if envelope is not None
        else receipt_id or str(payload.get("v1_receipt_id") or "") or None
    )
    receipts = ReceiptStore(project)
    if receipt_id:
        stored = receipts.find_by_id(receipt_id)
    elif receipt_only:
        # An orphan repair deliberately leaves the run live, so status polls
        # repeat. After the first poll the producer receipt is terminal and an
        # open-only lookup would lose its identity and raise on the second.
        # Bind by both run and producer action kind so a newer auxiliary
        # receipt cannot mask the one this branch owns.
        stored = receipts.latest_for_run_action_statuses(
            run_id,
            action_kind,
            get_args(ReceiptStatus),
        )
    else:
        # Missing queue evidence must not make recovery guess across every
        # receipt attached to the run. A newer auxiliary action (for example a
        # backfill) may have its own open receipt; bind the producer lookup to
        # the already-resolved run action kind before deriving authority.
        stored = receipts.latest_for_run_action_statuses(
            run_id,
            action_kind,
            (
                get_args(ReceiptStatus)
                if include_terminal_receipt_lookup
                else {"queued", "running"}
            ),
        )
    caller_supplied_fence = writer_attempt_id is not None or claim_token is not None
    if caller_supplied_fence and (
        not isinstance(writer_attempt_id, str) or not writer_attempt_id
    ):
        from frisket.execution.attempt import StaleAttemptWriter

        raise StaleAttemptWriter(
            "a dispatched queued terminal write requires its writer attempt"
        )
    receipt: Receipt | None = None
    if stored is not None:
        receipt_id = stored.id
        action_kind = stored.action_kind
        action_id = stored.action_id
        try:
            receipt = stored.parsed()
        except Exception:
            # The kernel owns malformed-body refusal and reports it without a
            # partial write. Do not let this convenience projection turn an
            # admin cancel/status poll into an uncaught parse-time 500.
            receipt = None
        if receipt is not None:
            job_id = job_id if job_id is not None else _queued_receipt_job_id(receipt)
            errors = [error] if error is not None else []
            receipt = receipt.model_copy(
                update={"run_id": run_id, "status": status, "errors": errors}
            )
    if receipt_id is None:
        raise ValueError("queued project-run terminalization has no receipt id")

    if caller_supplied_fence:
        assert writer_attempt_id is not None
        authority = CurrentWriterTerminalAuthority(
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
            claimless_direct_effect=claimless_direct_effect,
        )
    elif receipt_only:
        authority = ReceiptOnlyOrphanAuthority(
            claimless_direct_effect=claimless_direct_effect
        )
    elif repair_terminal_run_receipt:
        authority = TerminalRunReceiptRepairAuthority(
            claimless_direct_effect=claimless_direct_effect
        )
    elif never_dispatched:
        authority = NeverDispatchedRunTerminalAuthority(
            claimless_direct_effect=claimless_direct_effect,
        )
    else:
        prepared_attempt_id = (
            None if receipt is None else _queued_receipt_prepared_attempt_id(receipt)
        )
        prepared_attempt = (
            project.db.execute(
                "SELECT state FROM execution_attempts WHERE id=? AND run_id=?",
                (prepared_attempt_id, run_id),
            ).fetchone()
            if isinstance(prepared_attempt_id, str) and prepared_attempt_id
            else None
        )
        latest_attempt = project.db.execute(
            "SELECT state FROM execution_attempts WHERE run_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        has_abandoned_attempt = (
            project.db.execute(
                "SELECT 1 FROM execution_attempts "
                "WHERE run_id=? AND state='abandoned' LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        )
        if prepared_attempt is not None and prepared_attempt["state"] == "abandoned":
            authority = AbandonedAttemptRecoveryAuthority(
                claimless_direct_effect=claimless_direct_effect,
                require_cancel_intent=require_cancel_intent,
            )
        elif prepared_attempt is not None and prepared_attempt["state"] == "admitted":
            assert isinstance(prepared_attempt_id, str)
            authority = PreparedRunTerminalAuthority(
                claimless_direct_effect=claimless_direct_effect,
                prepared_attempt_id=prepared_attempt_id,
                require_cancel_intent=require_cancel_intent,
            )
        elif not has_abandoned_attempt and (
            (prepared_attempt is not None and prepared_attempt["state"] == "created")
            or (
                prepared_attempt is None
                and latest_attempt is not None
                and latest_attempt["state"] == "created"
            )
        ):
            # Consent/route verification can refuse after the attempt identity
            # is recorded but before admission. Close that zero-effect attempt
            # under the terminalizer's NeverDispatched proof. Unclaimed
            # terminalization cannot distinguish this legitimate refusal from
            # an arbitrary open attempt and correctly refuses it.
            authority = NeverDispatchedRunTerminalAuthority(
                claimless_direct_effect=claimless_direct_effect,
            )
        elif has_abandoned_attempt:
            authority = AbandonedRunRecoveryAuthority(
                claimless_direct_effect=claimless_direct_effect,
                require_cancel_intent=require_cancel_intent,
            )
        else:
            authority = UnclaimedRunTerminalAuthority(
                claimless_direct_effect=claimless_direct_effect,
                require_cancel_intent=require_cancel_intent,
            )
    terminalization = terminalize_project_run(
        project,
        run_id=run_id,
        receipt_id=receipt_id,
        status=status,
        run_status=terminal_run_status,  # type: ignore[arg-type]
        authority=authority,
        errors=[error] if error is not None else [],
        publication_refusal=publication_refusal,
    )
    if raise_on_conflict and terminalization.disposition in {
        "conflict",
        "reconciliation_required",
        "receipt_missing",
    }:
        from frisket.execution.attempt import StaleAttemptWriter

        raise StaleAttemptWriter(
            terminalization.reason
            or f"project-run terminalization {terminalization.disposition}"
        )
    action_result = ActionResult(
        action={"kind": action_kind, "action_id": action_id},
        status=status,
        project_id=project_id,
        run_id=run_id,
        job_id=job_id,
        receipt_id=receipt_id,
        errors=[error] if error is not None else [],
    )
    return QueuedV1TerminalReceiptTransition(
        action_result=action_result,
        terminalization=terminalization,
    )


def queued_v1_terminal_receipt_result(
    project: Project,
    payload: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> ActionResult:
    return queued_v1_terminal_receipt_transition(
        project,
        payload,
        **kwargs,
    ).action_result


def queued_v1_terminal_failure_result(
    project: Project,
    payload: Mapping[str, Any],
    *,
    project_id: str,
    run_id: int,
    error: ActionError,
    writer_attempt_id: str | None = None,
    claim_token: str | None = None,
    never_dispatched: bool = False,
    publication_refusal: DocumentTextPublicationRefused | None = None,
) -> ActionResult:
    return queued_v1_terminal_receipt_result(
        project,
        payload,
        project_id=project_id,
        run_id=run_id,
        status="failed",
        error=error,
        writer_attempt_id=writer_attempt_id,
        claim_token=claim_token,
        never_dispatched=never_dispatched,
        publication_refusal=publication_refusal,
    )


def queued_v1_worker_exception_failure_result(
    project: Project,
    payload: Mapping[str, Any],
    *,
    project_id: str,
    run_id: int,
    exc: Exception,
) -> ActionResult | None:
    envelope = queued_v1_payload_envelope(payload, project=project)
    if envelope is None:
        # Exception twin of the finalize seam: a v1
        # payload whose envelope cannot be rebuilt must still terminalize
        # its receipt (failed, honest error, claim released) instead of
        # returning None and stranding it queued/running. Non-v1 payloads
        # (no v1_receipt_id) still return None — nothing to terminalize.
        receipt_id = payload.get("v1_receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            return None
        error = ActionError(
            code="map_run_failed",
            message=safe_error("map_run_failed", exc).detail,
            action_kind=str(payload.get("action_kind") or "unknown"),
        )
        return queued_v1_terminal_receipt_result(
            project,
            payload,
            project_id=project_id,
            run_id=run_id,
            status="failed",
            error=error,
            receipt_id=receipt_id,
        )
    return queued_v1_terminal_failure_result(
        project,
        payload,
        project_id=project_id,
        run_id=run_id,
        error=envelope.entry.prepare_run_error(envelope.action, exc),
    )
