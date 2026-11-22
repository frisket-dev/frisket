"""Execute a program over rows as a parallel,
partial-failure-tolerant, resumable map.

Guarantees under test:
- failed rows land in per-row error state; the run continues past them
- retry/backoff respects the router; schema violations get ONE corrective
  retry with the violation echoed back, then fail cleanly per-cell
- a re-invoked run skips rows that already have results (manifest resume)
- 429 storms throttle concurrency instead of failing the run
- nothing is billed twice: completion goes through the response cache
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from frisket.ai.llm import ModelRouter
from frisket.cloud_execution import (
    CloudPayerSubject,
    cloud_llm_execution_scope,
)
from frisket.cloud_llm_checkpoint import CloudLLMCheckpointCoordinator
from frisket.cloud_runtime_binding import cloud_runtime_binding
from frisket.execution.consent_coverage import (
    ConsentCoverage,
    effective_consent_coverage,
)
from frisket.ops.base import (
    ACTION_PROGRAM_INVOCATION_HALT_CODES,
    ActionContext,
    ActionProgram,
    ActionProgramInvocationHalt,
    normalize_action_program_invocation_halt,
)
from frisket.redaction import safe_error
from frisket.engine.runner import preparation, validation
from frisket.engine.runner.network_policy import (
    row_effect_cannot_egress,
    row_effect_spends_or_meters,
)
from frisket.engine.runner.batch_normalization import normalize_batch_row
from frisket.engine.runner.finalization import (
    finalize_dispatched_run,
    finalize_pre_dispatch_run,
)
from frisket.engine.runner.preparation import PreparedRun
from frisket.engine.runner.publication import required_publication_fields
from frisket.engine.runner.result_generations import declare_prepared_outputs
from frisket.engine.runner.preview import PreviewColumn as PreviewColumn
from frisket.engine.runner.preview import (
    PreviewEffectRequiresRun as PreviewEffectRequiresRun,
)
from frisket.engine.runner.preview import PreviewResult as PreviewResult
from frisket.engine.runner.preview import PreviewRowCapError as PreviewRowCapError
from frisket.engine.runner.preview import _preview_validated, run_preview
from frisket.engine.runner.row_execution import AdaptiveThrottle
from frisket.engine.runner.row_execution import (
    GROUNDING_SIDECAR_PREFIX as GROUNDING_SIDECAR_PREFIX,
)
from frisket.engine.runner.row_execution import _await_owned_tasks, execute_row
from frisket.engine.runner.row_inputs import RowSourcePage, read_source_page, row_values
from frisket.engine.runner.validation import COST_GATE_USD as COST_GATE_USD
from frisket.engine.runner.validation import (
    BatchRowLimitExceeded as BatchRowLimitExceeded,
)
from frisket.engine.runner.validation import CostGate as CostGate
from frisket.engine.runner.validation import EmptyInputColumns as EmptyInputColumns
from frisket.engine.runner.validation import InvalidTargetRows as InvalidTargetRows
from frisket.engine.runner.validation import InvalidTargetSheet as InvalidTargetSheet
from frisket.engine.runner.validation import MissingProviderKey as MissingProviderKey
from frisket.engine.runner.validation import NetworkDisabled as NetworkDisabled
from frisket.engine.runner.validation import OutputColumnExists as OutputColumnExists
from frisket.engine.runner.validation import ProviderKeyRefusal as ProviderKeyRefusal
from frisket.engine.runner.validation import (
    ProviderSpendCapExceeded as ProviderSpendCapExceeded,
    ProviderSpendCapUnenforceable as ProviderSpendCapUnenforceable,
)
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.engine.store import Project
from frisket.engine.store.transactions import (
    roll_back_failed_work,
    write_transaction,
)
from frisket.engine.store.effect_checkpoints import (
    EffectCheckpointRef,
    EffectCheckpointStore,
    row_effect_ref,
)
from frisket.engine.store.writer_fence import WriterFence
from frisket.engine.store.generated_column_claims import (
    ClaimLeaseRenewalFailed,
    GeneratedColumnClaimStore,
)
from frisket.engine.store.result_generations import (
    GenerationSealedError,
    ResultGenerationStore,
)
from frisket.engine.store.runs import (
    EXPECTED_ROW_ERROR,
    FAILURE_OUTCOMES,
    TERMINAL_FAILURE_OUTCOMES,
    RunResultStore,
    RunResumeAdmission,
)
from frisket.execution.attempt import (
    ATTEMPT_EXTRA,
    AttemptClaimRefused,
    AttemptCommitment,
    RoutedAdmission,
    StaleAttemptWriter,
    claim as claim_attempt,
)
from frisket.execution.attempt_authority import (
    AttemptAuthority,
    DependentChoiceRefusal,
)
from frisket.execution.resolver import CandidateBinding
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.execution.pricing_policy import PricingPolicy, default_pricing_policy
from frisket.execution.provider import (
    ExecutionComposition,
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.operability.trace import TraceWriter

_log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 8
# Consecutive row failures before the run halts resumably (successes reset
# the streak).
DEFAULT_FAILURE_HALT_STREAK = 10
BATCH_SIZE = 25
LOCAL_RESULT_BATCH_ROWS = 256
# Checked after each completed row, not by a timer during a running handler.
LOCAL_RESULT_BATCH_SECONDS = 0.1
RUN_SCOPE_PAGE_SIZE = 500


class ResultEvidenceWriteFailed(RuntimeError):
    """A returned row could not publish its evidence, after savepoint rollback.

    Distinct from interruption/result-persistence failures: those retain their
    live writer for recovery. The action owner may settle this failure only
    with this exact writer and no ambiguous in-flight provider checkpoints.
    """

    def __init__(self, run_id: int, writer_attempt_id: str | None) -> None:
        super().__init__("project write failed")
        self.run_id = run_id
        self.writer_attempt_id = writer_attempt_id


_ROW_EFFECT_IDENTITY_IGNORED_SPEC_KEYS = frozenset(
    {
        # Authorization/recovery controls do not change what this one row asks
        # the provider to do.  Their durable owner is the attempt/consent row,
        # which the checkpoint carries separately.
        "confirmed",
        "consented_promise_set_hash",
        "halted_code",
        "halted_reason",
        # Selection is represented exactly by run_id + row_id.  A backfill may
        # spell its delta differently while asking the same row operation.
        "row_ids",
        "row_scope",
    }
)
_CLOUD_ROW_EFFECT_IDENTITY_IGNORED_SPEC_KEYS = (
    _ROW_EFFECT_IDENTITY_IGNORED_SPEC_KEYS
    | {
        # Output-generation preconditions change from absent to the columns
        # created by a failed first run. They fence local publication, not the
        # provider request, so an exact lost-response retry must retain its
        # Cloud purchase identity.
        "output_target_preconditions",
        "output_names",
        "child_output_names",
        "expected_output_columns",
        "expected_generated_columns",
        "replace_existing",
        "overwrite",
    }
)


def _row_effect_identity(
    *,
    project_storage_identity: str,
    run_id: int,
    row_id: int,
    program: ActionProgram,
    spec: dict[str, Any],
    values: dict[str, Any],
    out_cols: dict[str, int],
    cloud_payer: Mapping[str, object] | None = None,
) -> tuple[str, str]:
    """Return a stable logical id and exact request/scope identity digest."""

    logical = {
        "schema_version": "frisket.map_row_effect_id.v1",
        "run_id": int(run_id),
        "row_id": int(row_id),
    }
    checkpoint_id = (
        "row_effect_"
        + hashlib.sha256(
            json.dumps(logical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    if cloud_payer is None:
        semantic_spec = {
            str(key): value
            for key, value in spec.items()
            if key not in _ROW_EFFECT_IDENTITY_IGNORED_SPEC_KEYS
        }
        identity_payload = {
            "schema_version": "frisket.map_row_effect_identity.v1",
            **logical,
            "action_kind": spec["action_kind"],
            "action_version": program.version,
            "spec": semantic_spec,
            "values": values,
            "output_columns": sorted(
                (str(name), int(column_id)) for name, column_id in out_cols.items()
            ),
        }
    else:
        semantic_spec = {
            str(key): value
            for key, value in spec.items()
            if key not in _CLOUD_ROW_EFFECT_IDENTITY_IGNORED_SPEC_KEYS
        }
        identity_payload = {
            "schema_version": "frisket.cloud_map_row_effect_identity.v1",
            "project_storage_identity": project_storage_identity,
            "row_id": int(row_id),
            "action_kind": spec["action_kind"],
            "action_version": program.version,
            "spec": semantic_spec,
            "values": values,
            "cloud_payer": dict(cloud_payer),
        }
    # Equal decoded values are not the same executable input when a source
    # becomes pending, invalid, or failed. Never complete an uncertain paid
    # checkpoint as a zero-cost availability short-circuit.
    identity_payload["unavailable_inputs"] = list(
        getattr(values, "unavailable_inputs", ())
    )
    encoded = json.dumps(
        identity_payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return checkpoint_id, hashlib.sha256(encoded).hexdigest()


def _cloud_row_request_id(
    project: Project,
    *,
    current: EffectCheckpointRef,
    fence: WriterFence,
) -> tuple[EffectCheckpointRef, str, bool]:
    """Return the globally scoped Cloud key for this unresolved row effect.

    A fresh run may be the only way to recover after a create response was
    lost. Reuse only an exact prior reserved semantic identity; completed rows
    have been consumed and therefore mint a new purchase on an intentional
    later run.
    """

    query = (
        "SELECT id FROM effect_checkpoints "
        "WHERE family='row_effect' AND action_kind=? AND identity=? "
        "AND state='reserved' AND id<>? ORDER BY created_at, id LIMIT 1"
    )
    params = (current.action_kind, current.identity, current.checkpoint_id)
    prior = project.db.execute(query, params).fetchone()
    if prior is None:
        # A reopened worker may have started its SQLite snapshot before the
        # preceding failed run committed. The fresh read sees that committed
        # reservation without exposing the current uncommitted checkpoint.
        with project.read_snapshot() as snapshot:
            prior = snapshot.db.execute(query, params).fetchone()
    owner_ref = current
    adopted = False
    if prior is not None:
        owner_checkpoint_id = str(prior["id"])
        source = EffectCheckpointStore(project.db).get(owner_checkpoint_id)
        if source is None:
            raise ActionProgramInvocationHalt(
                "external_effect_reconciliation_required",
                "The prior Cloud reservation disappeared during recovery.",
            )
        source_ref = EffectCheckpointRef(
            checkpoint_id=owner_checkpoint_id,
            family="row_effect",
            group_key=str(source["group_key"]),
            unit_key=str(source["unit_key"]),
            action_kind=current.action_kind,
            identity=current.identity,
        )
        EffectCheckpointStore(project.db).adopt_reserved_for_recovery(
            source_ref,
            current=current,
            fence=fence,
        )
        owner_ref = replace(current, checkpoint_id=owner_checkpoint_id)
        adopted = True
    logical = {
        "schema_version": "frisket.cloud_map_row_request.v1",
        "project_storage_identity": project.storage_identity,
        "row_checkpoint_id": owner_ref.checkpoint_id,
    }
    encoded = json.dumps(
        logical, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return (
        owner_ref,
        "cloud_row_" + hashlib.sha256(encoded).hexdigest(),
        adopted,
    )


def _returned_row_response(checkpoint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    response = checkpoint.get("response")
    if not isinstance(response, dict) or not all(
        isinstance(name, str) and isinstance(payload, dict)
        for name, payload in response.items()
    ):
        raise ActionProgramInvocationHalt(
            "external_effect_reconciliation_required",
            "A returned row-effect checkpoint is incomplete or corrupt; "
            "refusing another provider call until it is reconciled.",
        )
    return response


def _accounted_replay_response(
    response: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Copy a returned response after cancellation already booked its facts."""

    replay = json.loads(
        json.dumps(response, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )
    for payload in replay.values():
        payload["cost"] = 0.0
    return replay


async def _cancel_and_join_owned_task(task: asyncio.Task[Any]) -> None:
    """Cancel one invocation task and shield its teardown until it is joined."""

    task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A repeated outer cancellation must not detach program cleanup.
            continue
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        raise error


@dataclass
class RunProgress:
    run_id: int
    total: int
    completed: int = 0
    failed: int = 0
    cost: float = 0.0
    done: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    # Cooperative cancellation: flip this to True (the server's POST
    # /runs/{id}/cancel does) and the runner stops dispatching queued rows,
    # cancels in-flight siblings, keeps completed results, and finishes
    # the run with status='cancelled'.
    cancelled: bool = False
    # Set only when the durable ``should_cancel`` projection requested the
    # stop.  A queued caller uses this to leave the exact dispatching attempt
    # open until its run/receipt/claim terminal transaction lands; circuit
    # breakers and typed program halts retain their existing close paths.
    cancel_requested: bool = False
    # Circuit breaker: consecutive-failure streak (successes reset it) and,
    # when the breaker trips, the reason. A tripped breaker cancels through
    # the SAME cooperative path above, so the run stays resumable via
    # backfill like any hand-cancelled run.
    failure_streak: int = 0
    halted_code: str | None = None
    halted_reason: str | None = None
    # Transient hand-off to a canonical action finalizer. It is never written
    # into run params, receipt evidence, or another holder representation.
    writer_attempt_id: str | None = None
    # Distinguishes an admitted/prepared attempt from the one that won the
    # dispatch CAS. A queued cancel observed before dispatch enters the
    # terminal kernel as unclaimed authority; it must never impersonate the
    # current-writer path merely because an attempt id exists.
    writer_attempt_claimed: bool = False

    def cancel(self) -> None:
        self.cancelled = True


def _require_execution_composition(
    composition: ExecutionComposition | None,
) -> ExecutionComposition:
    if composition is None:
        raise RuntimeError("MapRunner execution_composition is not initialized")
    return composition


@dataclass
class MapRunner:
    project: Project
    router: ModelRouter
    concurrency: int = DEFAULT_CONCURRENCY
    on_progress: Callable[[RunProgress], None] | None = None
    should_cancel: Callable[[int], bool] | None = None
    defer_cancel_terminal_close: bool = field(kw_only=True, default=False)
    action_context_extras: dict[str, Any] = field(default_factory=dict)
    allow_action_lifecycle_only_programs: bool = False
    halt_after_consecutive_failures: int | None = DEFAULT_FAILURE_HALT_STREAK
    # Mint once after admission and before any row executes.
    authority: "AttemptAuthority" = field(kw_only=True)
    consent_coverage: ConsentCoverage | None = field(kw_only=True, default=None)
    claimless_direct_effect: bool = field(kw_only=True, default=False)
    # Rating occurs at consent mint; dispatch never rates.
    pricing_policy: PricingPolicy = field(
        kw_only=True, default_factory=default_pricing_policy
    )
    execution_composition: ExecutionComposition | None = field(
        kw_only=True, default=None
    )
    _throttle: AdaptiveThrottle = field(default_factory=AdaptiveThrottle, init=False)
    run_store: RunResultStore = field(init=False)

    def __post_init__(self) -> None:
        authority_coverage = (
            self.authority.consent_coverage
            if isinstance(self.authority, AttemptAuthority)
            else None
        )
        if (
            self.consent_coverage is not None
            and authority_coverage is not None
            and self.consent_coverage != authority_coverage
        ):
            raise ValueError("runner and attempt authority must share consent coverage")
        self.consent_coverage = effective_consent_coverage(
            self.project, self.consent_coverage or authority_coverage
        )
        if isinstance(self.authority, AttemptAuthority):
            self.authority = replace(
                self.authority, consent_coverage=self.consent_coverage
            )
        if self.execution_composition is None:
            self.execution_composition = open_execution_composition(
                self.project,
                self.router,
                ExecutionCompositionContext.direct(),
            )
        self.run_store = RunResultStore(self.project)

    def _row_worker_count(self, program: ActionProgram, spec: dict) -> int:
        configured = max(1, int(self.concurrency or 1))
        # ``max_concurrency`` remains the established static program cap used
        # by existing temporal/media programs. ``max_row_concurrency`` adds a
        # spec-dependent cap for run-scoped local workers (for example,
        # Parakeet). Both are independent safety limits.
        caps = [configured]
        if program.max_concurrency is not None:
            caps.append(max(1, int(program.max_concurrency)))
        dynamic_cap = program.max_row_concurrency(spec)
        if dynamic_cap is not None:
            caps.append(max(1, int(dynamic_cap)))
        return min(caps)

    def estimate(self, spec: dict, *, program: ActionProgram | None = None) -> dict:
        """Pre-run estimate for the cost gate. Facade over validation.estimate_run
        (public — server/services/action_previews.py and workbench call this)."""
        return validation.estimate_run(
            self.project,
            spec,
            program=program,
            composition=_require_execution_composition(self.execution_composition),
            pricing_policy=self.pricing_policy,
            consent_coverage=self.consent_coverage,
        )

    def exact_replay_available(
        self,
        spec: dict[str, Any],
        *,
        program: ActionProgram | None = None,
    ) -> bool:
        """Facade over validation.exact_replay_available (public — called from
        server/action_enqueue.py)."""
        return validation.exact_replay_available(
            self.project,
            self.router,
            spec,
            program=program,
        )

    def prepare_run(
        self,
        spec: dict,
        *,
        program: ActionProgram | None = None,
        confirmed: bool = False,
        trusted_consent_authorizer: validation.TrustedConsentAuthorizer | None = None,
    ) -> RunProgress:
        """Create the op/run row synchronously without executing rows.

        Queue-backed APIs use this to preserve the public `/run -> run_id`
        contract while moving the actual row work to a worker process.
        """
        prepared = self._prepare(
            spec,
            program=program,
            confirmed=confirmed,
            resume_run_id=None,
            trusted_consent_authorizer=trusted_consent_authorizer,
        )
        progress = RunProgress(run_id=prepared.run_id, total=prepared.row_count)
        if self.on_progress:
            self.on_progress(progress)
        return progress

    def _prepare(
        self,
        spec: dict,
        *,
        program: ActionProgram | None = None,
        confirmed: bool,
        resume_run_id: int | None,
        precomputed_output_fields: list[dict[str, Any]] | None = None,
        trusted_consent_authorizer: validation.TrustedConsentAuthorizer | None = None,
        _validated: validation._ValidatedSpec | None = None,
    ) -> PreparedRun:
        if resume_run_id is not None and any(
            binding.state == "sealed"
            for binding in ResultGenerationStore(self.project).bindings_for_run(
                resume_run_id
            )
        ):
            raise GenerationSealedError(
                f"generation-managed run {resume_run_id} is sealed; "
                "start a fresh explicitly scoped run"
            )
        validated = _validated or self._validate_prepare_spec(
            spec,
            program=program,
            confirmed=confirmed,
            resume_run_id=resume_run_id,
            precomputed_output_fields=precomputed_output_fields,
            trusted_consent_authorizer=trusted_consent_authorizer,
        )
        if resume_run_id is None and (
            validated.program.atomic_output_columns
            or validated.program.consumes_resolution
            or validated.resolved_execution is not None
        ):
            # A fresh atomic family is one lifecycle unit: generated columns, op,
            # undo provenance, run row/scope, and the initial live pointer must
            # either all exist or none of them may. A SAVEPOINT composes with a
            # caller-owned transaction while committing on release when this is
            # the outermost transaction.
            savepoint = f"maprunner_atomic_prepare_{id(self)}"
            self.project.db.execute(f"SAVEPOINT {savepoint}")
            try:
                prepared = preparation.prepare_validated(
                    self.project,
                    self.run_store,
                    spec,
                    validated=validated,
                    resume_run_id=resume_run_id,
                )
                # Persist inside
                # the savepoint, so route artifacts commit with run creation.
                validation.persist_resolved_execution(
                    self.project,
                    spec,
                    validated,
                    prepared.run_id,
                    txn=self.project.db,
                )
                self.project.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                return prepared
            except BaseException:
                self.project.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.project.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
        if resume_run_id is not None:
            with write_transaction(self.project.db, what="resume run preparation"):
                prepared = preparation.prepare_validated(
                    self.project,
                    self.run_store,
                    spec,
                    validated=validated,
                    resume_run_id=resume_run_id,
                )
            validation.persist_resolved_execution(
                self.project,
                spec,
                validated,
                prepared.run_id,
            )
            return prepared

        if self.project.db.in_transaction:
            raise RuntimeError(
                "mode-B prepare requires no existing project transaction"
            )
        self.project.db.execute("BEGIN IMMEDIATE")
        try:
            prepared = preparation.prepare_validated(
                self.project,
                self.run_store,
                spec,
                validated=validated,
                resume_run_id=resume_run_id,
            )
            validation.persist_resolved_execution(
                self.project,
                spec,
                validated,
                prepared.run_id,
                txn=self.project.db,
            )
            self.project.db.commit()
            return prepared
        except BaseException:
            self.project.db.rollback()
            raise

    def _validate_prepare_spec(
        self,
        spec: dict[str, Any],
        *,
        program: ActionProgram | None = None,
        confirmed: bool,
        resume_run_id: int | None,
        precomputed_output_fields: list[dict[str, Any]] | None = None,
        trusted_consent_authorizer: validation.TrustedConsentAuthorizer | None = None,
    ) -> validation._ValidatedSpec:
        action_program = (
            program if program is not None else validation.action_program_for_spec(spec)
        )
        if (
            action_program.action_lifecycle_only
            and not self.allow_action_lifecycle_only_programs
        ):
            raise ValueError(
                f"action program '{action_program.name}' must be run through its supported action"
            )
        return validation.validate_spec(
            self.project,
            self.router,
            self.run_store,
            spec,
            program=action_program,
            confirmed=confirmed,
            resume_run_id=resume_run_id,
            pricing_policy=self.pricing_policy,
            composition=_require_execution_composition(self.execution_composition),
            consent_coverage=self.consent_coverage,
            precomputed_output_fields=precomputed_output_fields,
            trusted_consent_authorizer=trusted_consent_authorizer,
        )

    def prepare_admitted_run(
        self,
        spec: dict[str, Any],
        *,
        program: ActionProgram | None = None,
        confirmed: bool,
        output_fields: list[dict[str, Any]],
        trusted_consent_authorizer: validation.TrustedConsentAuthorizer | None = None,
    ) -> tuple[PreparedRun, AttemptCommitment]:
        """Prepare and admit a fresh canonical run in its caller's transaction."""

        if not self.project.db.in_transaction:
            raise RuntimeError(
                "prepare_admitted_run requires a caller-owned project transaction"
            )
        validated = self._validate_prepare_spec(
            spec,
            program=program,
            confirmed=confirmed,
            resume_run_id=None,
            precomputed_output_fields=output_fields,
            trusted_consent_authorizer=trusted_consent_authorizer,
        )
        prepared = self._prepare(
            spec,
            program=program,
            confirmed=confirmed,
            resume_run_id=None,
            precomputed_output_fields=output_fields,
            trusted_consent_authorizer=trusted_consent_authorizer,
            _validated=validated,
        )
        output_column_ids = sorted(prepared.out_cols.values())
        resolved = validated.resolved_execution
        prepared_binding = (
            CandidateBinding(
                facts=resolved.resolution.facts,
                connection=resolved.resolution.connection,
                target=resolved.resolution.target,
            )
            if resolved is not None
            else None
        )
        attempt = self.authority.mint_in_transaction(
            program=prepared.program,
            spec=spec,
            run_id=prepared.run_id,
            scope=self._attempt_scope(prepared, spec, output_column_ids),
            prepared_binding=prepared_binding,
        )
        if prepared.resolution_bound and not isinstance(
            attempt.admission,
            RoutedAdmission,
        ):
            raise DependentChoiceRefusal(
                f"action program '{prepared.program.name}' consumes execution resolution "
                "but its attempt was admitted UNROUTED; routed dispatch never "
                "executes unverified"
            )
        return prepared, attempt

    async def run(
        self,
        spec: dict,
        *,
        program: ActionProgram | None = None,
        confirmed: bool = False,
        resume_run_id: int | None = None,
        reopen_operator_cancelled: bool = True,
        claim_token: str | None = None,
        prepared_run: PreparedRun | None = None,
        admitted_attempt: AttemptCommitment | None = None,
        admitted_attempt_id: str | None = None,
        precomputed_output_fields: list[dict[str, Any]] | None = None,
        defer_attempt_close: bool = False,
        defer_generation_seal: bool = False,
        dispatch_claimed: bool = False,
        defer_output_publication: bool | None = None,
    ) -> RunProgress:
        if admitted_attempt is not None and admitted_attempt_id is not None:
            raise ValueError(
                "supply either admitted_attempt or admitted_attempt_id, not both"
            )
        # Pre-dispatch failure cleanup joins a transaction the caller already
        # held; it rolls back only failed work this run began itself.
        caller_transaction = self.project.db.in_transaction
        prepared = prepared_run or self._prepare(
            spec,
            program=program,
            confirmed=confirmed,
            resume_run_id=resume_run_id,
            precomputed_output_fields=precomputed_output_fields,
        )
        if (
            prepared_run is not None
            and resume_run_id is not None
            and prepared_run.run_id != resume_run_id
        ):
            raise ValueError("prepared_run belongs to a different resumed run")
        if admitted_attempt is not None and admitted_attempt.run_id != prepared.run_id:
            raise ValueError("admitted attempt belongs to a different run")
        resume_admitted = True
        admission: RunResumeAdmission | None = None
        if resume_run_id is not None and prepared.row_count > 0:
            # A resume begins only after validation/cost gates have passed. A
            # closed Change requires explicit Retry with a new Change; only
            # still-open interrupted work can resume. Admission reads current
            # durable state under the store transaction, never callback presence.
            admission = self.run_store.begin_run_resume(
                prepared.run_id,
                reopen_operator_cancelled=reopen_operator_cancelled,
                resumable_halt_codes=ACTION_PROGRAM_INVOCATION_HALT_CODES,
            )
            resume_admitted = admission.admitted
        action_program = prepared.program
        capture_result_evidence = getattr(
            action_program, "capture_result_evidence", None
        )
        write_result_evidence = getattr(action_program, "write_result_evidence", None)
        required_output_field_names = required_publication_fields(
            action_program,
            prepared.output_fields,
        )
        col_map = prepared.col_map
        row_ids = prepared.row_ids
        out_cols = prepared.out_cols
        output_column_ids = sorted(out_cols.values())
        change_id = prepared.change_id
        run_id = prepared.run_id
        if dispatch_claimed:
            if admitted_attempt is None:
                raise ValueError(
                    "a claimed dispatch requires its exact admitted attempt"
                )
            GeneratedColumnClaimStore.require_current_writer(
                self.project.db,
                fence=WriterFence(
                    run_id=run_id,
                    writer_attempt_id=admitted_attempt.attempt_id,
                    claim_token=claim_token,
                ),
                output_column_ids=frozenset(output_column_ids),
            )
        # The lazy writer creates one sidecar only if a row actually reaches a
        # model. Trace failures are diagnostic and never own action outcomes.
        try:
            recorder = TraceWriter.open(
                self.project.path,
                run_id,
                action_kind=spec["action_kind"],
                model=spec.get("model"),
            )
        except Exception:  # noqa: BLE001 - trace setup never owns the run
            recorder = None

        progress = RunProgress(run_id=run_id, total=prepared.row_count)
        if not resume_admitted:
            progress.cancel()
        if self.on_progress:
            # announce the run as soon as its row exists — callers (the
            # server's /run endpoint) poll for the run id; without this a
            # sub-batch-size run reports nothing until its final flush and
            # the endpoint times out with 500 "run failed to start".
            self.on_progress(progress)

        def cancelled() -> bool:
            if progress.cancelled:
                return True
            if self.run_store.cancellation_requested(run_id):
                progress.cancel_requested = True
                progress.cancel()
                return True
            if self.should_cancel is not None and self.should_cancel(run_id):
                # A positive stop poll is durable user intent. Close this
                # Change before any already-returned effect is published; its
                # accounting remains owned by the exact attempt and can still
                # drain through the terminal writer.
                self.run_store.request_cancel(run_id)
                progress.cancel_requested = True
                progress.cancel()
            return progress.cancelled

        pending: list[dict] = []
        lock = asyncio.Lock()
        source_column_types = {
            c["name"]: c["type"]
            for c in self.project.columns(prepared.sheet_id)
            if c["name"] in set(action_program.source_columns(spec))
        }
        # Source identity was resolved during preparation. Live names/types may
        # have changed since then; they must not retarget or omit these reads.
        source_column_ids = [
            col_map[name]
            for name in map(str, action_program.source_columns(spec))
            if name in col_map
        ]
        # Repair actions may deliberately consume a projection-invalid payload;
        # every other source keeps the ordinary typed-reader mask.
        preserve_invalid_column_ids = frozenset(
            col_map[name]
            for name in getattr(action_program, "preserve_invalid_source_columns", ())
            if name in col_map
        )

        publication_cancelled = False
        pre_dispatch_fence = WriterFence(run_id=run_id, claim_token=claim_token)

        def current_writer_fence() -> WriterFence:
            attempt_id = attempt.attempt_id if attempt is not None else None
            return WriterFence(
                run_id=run_id,
                writer_attempt_id=attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=self.claimless_direct_effect,
                authorized_attempt_id=attempt_id,
            )

        def persist_result_batch(
            batch: list[dict[str, Any]],
            *,
            checkpoint: EffectCheckpointRef | None = None,
        ) -> None:
            nonlocal publication_cancelled
            fence = current_writer_fence()
            writer_attempt_id = fence.writer_attempt_id

            citation_writer: Callable[[], None] | None = None
            consumed = {
                int(item["row_id"]): item["__consumed_sources"]
                for item in batch
                if isinstance(item.get("__consumed_sources"), dict)
            }
            if callable(write_result_evidence) or consumed:

                def _write_citations() -> None:
                    try:
                        from frisket.engine.store.consumed_sources import store_capture

                        for input_row_id, capture in consumed.items():
                            store_capture(
                                self.project,
                                capture,
                                owner_change_id=change_id,
                                output_sheet_id=prepared.sheet_id,
                                run_id=run_id,
                                row_id=input_row_id,
                            )
                        if callable(write_result_evidence):
                            write_result_evidence(
                                self.project,
                                spec,
                                batch=batch,
                                sheet_id=prepared.sheet_id,
                                run_id=run_id,
                                change_id=change_id,
                                output_columns=out_cols,
                                input_column_ids=col_map,
                                claim_token=claim_token,
                            )
                    except (StaleAttemptWriter, ClaimLeaseRenewalFailed):
                        raise
                    except Exception as exc:
                        raise ResultEvidenceWriteFailed(
                            run_id, writer_attempt_id
                        ) from exc

                citation_writer = _write_citations

            from frisket.engine.store.changes import ChangePublicationClosed
            from frisket.engine.store.result_generations import GenerationStateError

            try:
                self.run_store.publish_result_batch(
                    run_id,
                    batch,
                    fence=fence,
                    checkpoint=checkpoint,
                    defer_generation_seal=defer_generation_seal,
                    citation_writer=citation_writer,
                )
            except (
                ChangePublicationClosed,
                GenerationStateError,
                StaleAttemptWriter,
            ) as exc:
                # Source deletion closes publication while requests already in
                # flight still return. Accounting/checkpoints were committed
                # before this publication attempt; drain siblings normally.
                closed = isinstance(
                    exc, (ChangePublicationClosed, StaleAttemptWriter)
                ) or isinstance(exc.__cause__, ChangePublicationClosed)
                if not closed or writer_attempt_id is None:
                    raise
                from frisket.engine.store.run_lifecycle import require_terminal_writer

                closed_publication = require_terminal_writer(
                    self.project,
                    fence=fence,
                    terminal_status="cancelled",
                    output_column_ids=output_column_ids,
                )
                if not closed_publication:
                    raise
                publication_cancelled = True
                progress.cancel_requested = True
                progress.cancel()

        # Route-binding extras are
        # merged HERE, on the one execution path every routed run shares
        # (queued handler, direct /run, fresh backfill, and open-run recovery) —
        # not only in the queue handler. The caller's own extras win when
        # already supplied (the queued handler pre-loads them so its typed
        # no_live_target mapping stays in charge). The fact writer refuses
        # routed facts without an observation payload, so any path that
        # skipped this merge would fail loudly there, never silently write
        # a legacy fact under a routed run.
        from frisket.execution.runtime_binding import pre_dispatch_refusal_markers

        def close_out_pre_dispatch_failure(
            exc: BaseException,
            *,
            finalization_out_cols: dict[str, int] | None = None,
            never_point_column_ids: set[int] | frozenset[int] = frozenset(),
        ) -> None:
            """A pre-dispatch refusal (route-binding load, the unverified
            fence, run-start verification) means ZERO rows dispatched, so the
            run row must not be left at "running". Where it goes depends on
            whether it HAS a prior state:

            - a RESUME reverts to exactly where admission found it — status,
              finished_at, and the halt markers admission cleared. The
              failure belongs to the ACTION (whose boundary already releases
              its reservation and output claims), not to the run, and the
              run stays reconsent-reachable in its pre-resume shape.
            - a FRESH launch has no prior state to revert TO. Reverting
              nothing left it at status='running' with a NULL finished_at
              forever: never resumable (no terminal halt markers), never
              reported, unreachable by ``run.backfill``. It is terminalized
              instead — failed, finished, with the typed refusal code in the
              run's markers — through the same ``finalize_run`` every other
              terminal outcome uses.

            The halt machinery's own transitions (promise_violation ->
            resumable cancelled) are deliberately NOT routed through here —
            they terminalize the run on purpose, with their own markers.
            """
            if dispatch_claimed:
                # A coupled publisher already owns deterministic work under
                # this writer. It retains recovery and terminalization.
                return
            if not caller_transaction:
                roll_back_failed_work(
                    self.project.db, before="pre-dispatch run close-out"
                )
            if admission is not None:
                self.run_store.revert_run_resume(run_id, admission)
                return
            code, reason = pre_dispatch_refusal_markers(exc)
            terminal_attempt_id = (
                attempt.attempt_id if attempt is not None else admitted_attempt_id
            )
            if terminal_attempt_id is None:
                prepared_attempts = self.project.db.execute(
                    "SELECT id FROM execution_attempts WHERE run_id=? "
                    "AND state IN ('created','admitted') ORDER BY seq",
                    (run_id,),
                ).fetchall()
                if len(prepared_attempts) == 1:
                    terminal_attempt_id = str(prepared_attempts[0]["id"])
            finalize_pre_dispatch_run(
                self.project,
                self.run_store,
                change_id=change_id,
                run_id=run_id,
                status="failed",
                out_cols=(
                    out_cols if finalization_out_cols is None else finalization_out_cols
                ),
                fence=pre_dispatch_fence,
                keep_zero_success_generated_columns=(
                    action_program.keep_zero_success_generated_columns
                ),
                halted_code=code,
                halted_reason=reason,
                terminal_attempt_id=terminal_attempt_id,
                terminal_attempt_state=(
                    "halted" if terminal_attempt_id is not None else None
                ),
                never_point_column_ids=never_point_column_ids,
            )

        def terminalize_halt(exc: ActionProgramInvocationHalt) -> RunProgress:
            """A ``promise_violation`` (or any allowlisted invocation halt)
            raised before a single row dispatched: the run terminalizes as
            CANCELLED with its persisted markers. Recovery is a fresh scoped
            run. Identical to the halt handling on both dispatch branches
            below — the mint simply reaches it first."""
            progress.halted_code, progress.halted_reason = (
                normalize_action_program_invocation_halt(exc.code, exc.detail)
            )
            progress.cancel()
            finalize_pre_dispatch_run(
                self.project,
                self.run_store,
                change_id=change_id,
                run_id=run_id,
                status="cancelled",
                out_cols=out_cols,
                fence=pre_dispatch_fence,
                keep_zero_success_generated_columns=(
                    action_program.keep_zero_success_generated_columns
                ),
                halted_code=progress.halted_code,
                halted_reason=progress.halted_reason,
                terminal_attempt_id=(
                    attempt.attempt_id if attempt is not None else None
                ),
                terminal_attempt_state=("halted" if attempt is not None else None),
            )
            progress.done = True
            if self.on_progress:
                self.on_progress(progress)
            return progress

        attempt: AttemptCommitment | None = admitted_attempt
        run_extras = dict(self.action_context_extras)
        try:
            declare_prepared_outputs(
                self.project,
                self.run_store,
                prepared,
                claim_token=claim_token,
                defer_publication=(
                    defer_generation_seal
                    if defer_output_publication is None
                    else defer_output_publication
                ),
            )
        except BaseException as exc:
            close_out_pre_dispatch_failure(
                exc,
                finalization_out_cols={
                    name: column_id
                    for name, column_id in out_cols.items()
                    if int(column_id) in prepared.created_generated_column_ids
                },
                never_point_column_ids=frozenset(
                    int(column_id) for column_id in prepared.out_cols.values()
                ),
            )
            if admission is None and not dispatch_claimed:
                from frisket.engine.runner.column_retirement import (
                    retire_unbound_created_columns,
                )

                retire_unbound_created_columns(
                    self.project, change_id, prepared.created_generated_column_ids
                )
            raise
        if not cancelled():
            try:
                if attempt is None:
                    if admitted_attempt_id is not None:
                        attempt = self.authority.load_admitted(
                            attempt_id=admitted_attempt_id,
                            program=action_program,
                            spec=spec,
                            run_id=run_id,
                        )
                    else:
                        attempt = self.authority.mint(
                            program=action_program,
                            spec=spec,
                            run_id=run_id,
                            scope=self._attempt_scope(
                                prepared,
                                spec,
                                output_column_ids,
                            ),
                        )
                if prepared.resolution_bound and not isinstance(
                    attempt.admission, RoutedAdmission
                ):
                    raise DependentChoiceRefusal(
                        f"action program '{action_program.name}' consumes execution resolution "
                        "but its attempt was admitted UNROUTED; routed "
                        "dispatch never executes unverified"
                    )
            except ActionProgramInvocationHalt as exc:
                return terminalize_halt(exc)
            except BaseException as exc:
                close_out_pre_dispatch_failure(exc)
                raise
        # A pre-admitted attempt remains part of the owned context even when
        # Stop arrives before its dispatch claim. Domain scopes may ask for
        # dispatch permission during setup; they must see that attempt and let
        # RunDispatch reject it, rather than treating managed work as unowned.
        if attempt is not None:
            run_extras[ATTEMPT_EXTRA] = attempt
            progress.writer_attempt_id = attempt.attempt_id

        admitted_work_scope = None
        if attempt is not None and attempt.routed is not None:
            snapshot = attempt.routed.work_scope_snapshot
            if snapshot is not None:
                # Membership is checked for every dispatched row; normalize once.
                admitted_work_scope = {
                    **snapshot,
                    "row_ids": frozenset(snapshot.get("row_ids", ())),
                }

        batch_local_results = bool(
            action_program.batch_local_results
            and attempt is not None
            and attempt.routed is None
            and not action_program.is_llm(spec)
            and not row_effect_spends_or_meters(action_program, spec, self.router)
        )
        progress.writer_attempt_claimed = dispatch_claimed
        claimed = [dispatch_claimed]

        def claim_the_attempt() -> None:
            """The compare-and-swap of §1.3, taken exactly once before the
            first effect — the earliest operation that discloses user data
            externally or incurs provider cost.

            ``ActionProgramInvocationHalt`` never originates here; a lost CAS is the
            durable, reconsent-shaped refusal (a concurrent reconsent
            superseded this attempt), so it escapes with zero rows dispatched
            and the run reverts (resume) or terminalizes (fresh launch) — see
            ``close_out_pre_dispatch_failure``."""
            if claimed[0] or attempt is None:
                return
            claimed[0] = True
            try:
                claim_attempt(
                    self.project,
                    attempt,
                    fence=current_writer_fence(),
                    output_column_ids=frozenset(output_column_ids),
                )
                progress.writer_attempt_claimed = True
            except AttemptClaimRefused as exc:
                from frisket.execution.runtime_binding import (
                    ExecutionRouteVerificationFailed,
                )

                failure = ExecutionRouteVerificationFailed(exc.code, str(exc))
                close_out_pre_dispatch_failure(failure)
                raise failure from exc
            except BaseException as exc:
                close_out_pre_dispatch_failure(exc)
                raise

        last_work_progress: dict[int, float] = {}
        work_progress_files: dict[int, dict[str, Any]] = {}

        def report_work_progress(
            *,
            kind: str,
            row_id: int,
            completed: int,
            total: int,
            filename: str | None = None,
        ) -> None:
            if kind != "ocr_pages":
                return
            now = time.monotonic()
            if (
                completed not in (0, 1, total)
                and now - last_work_progress.get(row_id, 0.0) < 0.5
            ):
                return
            file: dict[str, Any] = {
                "row_id": row_id,
                "completed": completed,
                "total": total,
            }
            if filename:
                file["filename"] = filename[:200]
            work_progress_files[row_id] = file
            try:
                self.run_store.set_work_progress(
                    run_id, files=list(work_progress_files.values())
                )
            except sqlite3.Error:
                _log.warning("work_progress_write_failed", exc_info=True)
            last_work_progress[row_id] = now

        def clear_work_progress(row_id: int) -> None:
            last_work_progress.pop(row_id, None)
            if work_progress_files.pop(row_id, None) is None:
                return
            try:
                if work_progress_files:
                    self.run_store.set_work_progress(
                        run_id, files=list(work_progress_files.values())
                    )
                else:
                    self.run_store.clear_work_progress(run_id)
            except sqlite3.Error:
                _log.warning("work_progress_write_failed", exc_info=True)

        # remote-engine ops (ocr's VLM tier) reach the router via extras
        ctx = ActionContext(
            project=self.project,
            http=self.router.client,
            credential_use_context=(
                self.execution_composition.credential_use_context
                if self.execution_composition is not None
                else None
            ),
            execution_limits=(
                self.execution_composition.limits
                if self.execution_composition is not None
                else None
            ),
            extras={
                **run_extras,
                "run_id": run_id,
                "claim_token": claim_token,
                "router": self.router,
                "cancelled": cancelled,
                "report_work_progress": report_work_progress,
                "source_column_types": source_column_types,
                "output_columns": dict(out_cols),
                # Shared only by row contexts in this run; program singletons
                # must not hold mutable execution state.
                "run_state": {},
            },
        )
        if attempt is not None:
            from frisket.execution.dispatch_control import dispatch_for_context

            ctx.extras["router"] = self.router.for_execution(dispatch_for_context(ctx))
        if not action_program.is_llm(spec) and hasattr(action_program, "execute_batch"):
            # Batch programs intentionally keep whole-run semantics here rather
            # than any page-local execution shape: clean_column needs global
            # canonical grouping, and census demographics needs global dedupe.
            if prepared.run_scope_run_id is not None and not row_ids:
                row_ids = self._pending_run_scope_row_ids(
                    prepared.run_scope_run_id, output_column_ids
                )
            try:
                if not cancelled():
                    # The claim happens here too, before the batch branch
                    # returns — a batch program that consumes resolution is
                    # authorized exactly like a row-oriented one.
                    claim_the_attempt()
                batch_task = asyncio.create_task(
                    self._run_batch_program(
                        action_program,
                        spec,
                        ctx,
                        row_ids=row_ids,
                        col_map=col_map,
                        out_cols=out_cols,
                        column_types=source_column_types,
                        persist_result_batch=persist_result_batch,
                        progress=progress,
                        required_field_names=required_output_field_names,
                        admitted_work_scope=admitted_work_scope,
                        cancelled=cancelled,
                    )
                )
                try:
                    batch_progress = await asyncio.shield(batch_task)
                except asyncio.CancelledError:
                    cancelled()
                    cooperative_stop = progress.cancelled
                    await _cancel_and_join_owned_task(batch_task)
                    current_task = asyncio.current_task()
                    if not cooperative_stop or (
                        current_task is not None and current_task.cancelling()
                    ):
                        raise
                    # A handler observed the existing cooperative stop signal;
                    # return through the ordinary receipt-owning close below.
                    # An actual cancellation of this owner task still escapes.
                    batch_progress = progress
                cooperative_cancel = bool(
                    self.defer_cancel_terminal_close
                    and batch_progress.cancel_requested
                    and batch_progress.completed < len(row_ids)
                )
                if attempt is not None and claimed[0]:
                    finalize_dispatched_run(
                        self.project,
                        self.run_store,
                        change_id=change_id,
                        status=(
                            "cancelled"
                            if batch_progress.cancelled
                            and batch_progress.completed < len(row_ids)
                            else "completed"
                        ),
                        out_cols=out_cols,
                        keep_zero_success_generated_columns=(
                            action_program.keep_zero_success_generated_columns
                        ),
                        auto_verify=bool(action_program.auto_verify),
                        fence=current_writer_fence(),
                        defer_terminal_status=cooperative_cancel,
                        defer_generation_seal=defer_generation_seal,
                        terminal_attempt_state=(
                            None
                            if defer_attempt_close or cooperative_cancel
                            else "effected"
                        ),
                    )
                elif not cooperative_cancel:
                    finalize_pre_dispatch_run(
                        self.project,
                        self.run_store,
                        change_id=change_id,
                        run_id=run_id,
                        status=(
                            "cancelled"
                            if batch_progress.cancelled
                            and batch_progress.completed < len(row_ids)
                            else "completed"
                        ),
                        out_cols=out_cols,
                        fence=pre_dispatch_fence,
                        keep_zero_success_generated_columns=(
                            action_program.keep_zero_success_generated_columns
                        ),
                        auto_verify=bool(action_program.auto_verify),
                        defer_generation_seal=defer_generation_seal,
                        terminal_attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        terminal_attempt_state=(
                            "effected" if attempt is not None else None
                        ),
                    )
                batch_progress.done = True
                if self.on_progress:
                    self.on_progress(batch_progress)
                return batch_progress
            except asyncio.CancelledError:
                # Outer cancellation is post-dispatch ambiguous. Batch-owned
                # cleanup has joined above; terminalize the run through the
                # current-writer fence, but leave that writer and its claim
                # live for the returned-accounting owner.
                progress.cancel()
                if attempt is not None and claimed[0]:
                    finalize_dispatched_run(
                        self.project,
                        self.run_store,
                        change_id=change_id,
                        status="cancelled",
                        out_cols=out_cols,
                        keep_zero_success_generated_columns=(
                            action_program.keep_zero_success_generated_columns
                        ),
                        fence=current_writer_fence(),
                        terminal_attempt_state=None,
                    )
                else:
                    finalize_pre_dispatch_run(
                        self.project,
                        self.run_store,
                        change_id=change_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        fence=pre_dispatch_fence,
                        keep_zero_success_generated_columns=(
                            action_program.keep_zero_success_generated_columns
                        ),
                        terminal_attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        terminal_attempt_state=(
                            "halted" if attempt is not None else None
                        ),
                    )
                progress.done = True
                if self.on_progress:
                    self.on_progress(progress)
                raise
            except ActionProgramInvocationHalt as exc:
                # The batch path's twin of the row path's halt handling: a
                # promise_violation (or any allowlisted invocation halt)
                # terminalizes this generation as cancelled with its
                # persisted markers. Recovery gets a fresh scoped run.
                progress.halted_code, progress.halted_reason = (
                    normalize_action_program_invocation_halt(exc.code, exc.detail)
                )
                progress.cancel()
                if attempt is not None and claimed[0]:
                    finalize_dispatched_run(
                        self.project,
                        self.run_store,
                        change_id=change_id,
                        status="cancelled",
                        out_cols=out_cols,
                        keep_zero_success_generated_columns=(
                            action_program.keep_zero_success_generated_columns
                        ),
                        halted_code=progress.halted_code,
                        halted_reason=progress.halted_reason,
                        fence=current_writer_fence(),
                        terminal_attempt_state=(
                            None if defer_attempt_close else "halted"
                        ),
                        defer_generation_seal=defer_generation_seal,
                    )
                else:
                    finalize_pre_dispatch_run(
                        self.project,
                        self.run_store,
                        change_id=change_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        fence=pre_dispatch_fence,
                        keep_zero_success_generated_columns=(
                            action_program.keep_zero_success_generated_columns
                        ),
                        halted_code=progress.halted_code,
                        halted_reason=progress.halted_reason,
                        terminal_attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        terminal_attempt_state=(
                            "halted" if attempt is not None else None
                        ),
                        defer_generation_seal=defer_generation_seal,
                    )
                progress.done = True
                if self.on_progress:
                    self.on_progress(progress)
                return progress
            except StaleAttemptWriter:
                # A provider may already have charged the losing invocation.
                # Do not finalize the shared run or close/release replacement
                # authority; the action boundary owns the typed refusal.
                raise
            except SandboxTeardownError as exc:
                progress.halted_code, progress.halted_reason = (
                    normalize_action_program_invocation_halt(
                        "local_session_failed",
                        "The local model session could not be verified as "
                        "stopped; restart Frisket before resuming the run.",
                    )
                )
                progress.cancel()
                if attempt is not None and claimed[0]:
                    finalize_dispatched_run(
                        self.project,
                        self.run_store,
                        change_id=change_id,
                        status="cancelled",
                        out_cols=out_cols,
                        keep_zero_success_generated_columns=(
                            action_program.keep_zero_success_generated_columns
                        ),
                        halted_code=progress.halted_code,
                        halted_reason=progress.halted_reason,
                        fence=current_writer_fence(),
                        terminal_attempt_state="halted",
                    )
                else:
                    finalize_pre_dispatch_run(
                        self.project,
                        self.run_store,
                        change_id=change_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        fence=pre_dispatch_fence,
                        keep_zero_success_generated_columns=(
                            action_program.keep_zero_success_generated_columns
                        ),
                        halted_code=progress.halted_code,
                        halted_reason=progress.halted_reason,
                    )
                progress.done = True
                if self.on_progress:
                    self.on_progress(progress)
                exc.run_id = run_id
                raise

        pending_row_count = 0
        last_flush = time.monotonic()

        async def flush(*, force: bool = True) -> None:
            nonlocal pending_row_count, last_flush
            # Pure local rows share a bounded publication. All other rows keep
            # their original durability boundary, including provider effects.
            batch: list[dict] | None = None
            async with lock:
                if (
                    not force
                    and batch_local_results
                    and pending_row_count < LOCAL_RESULT_BATCH_ROWS
                    and time.monotonic() - last_flush < LOCAL_RESULT_BATCH_SECONDS
                ):
                    return
                if pending:
                    batch = list(pending)
                    pending.clear()
                    pending_row_count = 0
            if batch is None:
                return
            persist_result_batch(batch)
            last_flush = time.monotonic()
            if self.on_progress:
                self.on_progress(progress)

        streak_outcomes: dict[int, bool] = {}
        streak_next = [0]

        async def one_row(
            row_id: int, seq: int, source_page: RowSourcePage | None = None
        ) -> None:
            nonlocal pending_row_count
            if cancelled():
                return
            await self._throttle.wait()
            if cancelled():
                return
            input_capture: dict[str, dict[str, Any]] | None = (
                {} if callable(capture_result_evidence) else None
            )
            values = (
                prepared.read_inputs(row_id)
                if prepared.read_inputs
                else row_values(
                    self.project,
                    action_program,
                    spec,
                    col_map,
                    row_id,
                    for_model=action_program.is_llm(spec),
                    column_types=source_column_types,
                    capture=input_capture,
                    admitted_work_scope=admitted_work_scope,
                    source_page=source_page,
                )
            )
            consumed_capture = getattr(values, "consumed_capture", None)
            checkpoint_id: str | None = None
            checkpoint_identity: str | None = None
            checkpoint_created = False
            checkpoint_should_complete = False
            cloud_request_id: str | None = None
            cloud_llm_coordinator: CloudLLMCheckpointCoordinator | None = None
            results: dict[str, dict[str, Any]] | None = None
            cloud_payer = (
                attempt.routed.binding.facts.cloud_payer
                if attempt is not None and attempt.routed is not None
                else None
            )
            if cloud_payer is not None and not isinstance(
                cloud_payer, CloudPayerSubject
            ):
                raise TypeError("admitted Cloud payer has an invalid type")
            checkpoint_ref: EffectCheckpointRef | None = None
            if (
                row_effect_spends_or_meters(action_program, spec, self.router)
                and (
                    not getattr(values, "unavailable_inputs", ())
                    or self.run_store.row_effect_checkpoint(run_id, row_id) is not None
                )
                and getattr(action_program, "row_effect_checkpoint_owner", "runner")
                == "runner"
            ):
                checkpoint_id, checkpoint_identity = _row_effect_identity(
                    project_storage_identity=self.project.storage_identity,
                    run_id=run_id,
                    row_id=row_id,
                    program=action_program,
                    spec=spec,
                    values=values,
                    out_cols=out_cols,
                    cloud_payer=(
                        cloud_payer.to_snapshot() if cloud_payer is not None else None
                    ),
                )
                checkpoint_ref = row_effect_ref(
                    checkpoint_id,
                    run_id=run_id,
                    row_id=row_id,
                    action_kind=spec["action_kind"],
                    identity=checkpoint_identity,
                )
                checkpoint = self.run_store.row_effect_checkpoint(run_id, row_id)
                if checkpoint is None:
                    checkpoint_created = self.run_store.reserve_row_effect_checkpoint(
                        checkpoint_ref,
                        consumed_capture=consumed_capture,
                        fence=current_writer_fence(),
                    )
                    if not checkpoint_created:
                        checkpoint = self.run_store.row_effect_checkpoint(
                            run_id, row_id
                        )
                    else:
                        checkpoint_should_complete = True
                if not checkpoint_created:
                    if checkpoint is None:
                        raise ActionProgramInvocationHalt(
                            "external_effect_reconciliation_required",
                            "A row-effect reservation raced or disappeared; refusing "
                            "another provider call until it is reconciled.",
                        )
                    if (
                        checkpoint.get("action_kind") != spec["action_kind"]
                        or checkpoint.get("identity") != checkpoint_identity
                    ):
                        raise ActionProgramInvocationHalt(
                            "external_effect_reconciliation_required",
                            "A prior row effect was reserved for a different "
                            "request or source value; refusing to substitute the "
                            "current row or call the provider again.",
                        )
                    if checkpoint.get("state") == "reserved":
                        reserved = checkpoint.get("response")
                        consumed_capture = (
                            reserved.get("__consumed_sources")
                            if isinstance(reserved, dict)
                            else None
                        )
                        if cloud_payer is None:
                            raise ActionProgramInvocationHalt(
                                "external_effect_reconciliation_required",
                                "A prior process may have reached the provider for "
                                "this row, but no response was durably recorded; "
                                "refusing another call until the effect is reconciled.",
                            )
                        checkpoint_id = str(checkpoint["id"])
                        checkpoint_ref = replace(
                            checkpoint_ref, checkpoint_id=checkpoint_id
                        )
                        checkpoint_should_complete = True
                        checkpoint = None
                    if checkpoint is None:
                        pass
                    elif checkpoint.get("state") != "returned":
                        raise ActionProgramInvocationHalt(
                            "external_effect_reconciliation_required",
                            "A row-effect checkpoint has an invalid state; refusing "
                            "another provider call until it is reconciled.",
                        )
                    else:
                        checkpoint_id = str(checkpoint["id"])
                        checkpoint_ref = replace(
                            checkpoint_ref, checkpoint_id=checkpoint_id
                        )
                        results = _returned_row_response(checkpoint)
                        if (
                            callable(capture_result_evidence)
                            and input_capture is not None
                        ):
                            capture_result_evidence(
                                values,
                                input_capture,
                                results,
                                spec,
                                run_id=run_id,
                                change_id=change_id,
                                row_id=row_id,
                                output_columns=out_cols,
                            )
            unavailable = getattr(values, "unavailable_inputs", ())
            if results is None and unavailable and checkpoint_ref is None:
                from frisket.engine.store.input_availability import not_completed_result

                # Availability is decided before either local or Cloud dispatch.
                # An existing paid checkpoint must still take its recovery path.
                results = {
                    name: not_completed_result(unavailable[0]) for name in out_cols
                }
                if consumed_capture is not None:
                    next(iter(results.values()))["__consumed_sources"] = (
                        consumed_capture
                    )

            if results is None:
                if cloud_payer is not None:
                    if checkpoint_ref is None:
                        raise ActionProgramInvocationHalt(
                            "promise_violation",
                            "Cloud execution requires a durable admitted row effect",
                        )
                    if attempt is None:
                        raise ActionProgramInvocationHalt(
                            "promise_violation",
                            "Cloud execution requires an authorized attempt",
                        )
                    try:
                        checkpoint_ref, cloud_request_id, adopted_checkpoint = (
                            _cloud_row_request_id(
                                self.project,
                                current=checkpoint_ref,
                                fence=current_writer_fence(),
                            )
                        )
                    except BaseException:
                        # Selection/adoption has not reached the transport.  A
                        # just-created replacement is therefore proven
                        # no-egress, while the prior ambiguous reservation must
                        # remain available to its still-live owner.
                        if checkpoint_created:
                            self.run_store.discard_reserved_row_effect_checkpoint(
                                checkpoint_ref,
                                fence=current_writer_fence(),
                            )
                        raise
                    if adopted_checkpoint:
                        # This reservation may already have reached Cloud in a
                        # prior run.  A local refusal or cancellation cannot
                        # prove no egress and must not discard its recovery key.
                        checkpoint_created = False
                if checkpoint_created and cancelled():
                    assert checkpoint_ref is not None
                    self.run_store.discard_reserved_row_effect_checkpoint(
                        checkpoint_ref,
                        fence=current_writer_fence(),
                    )
                    return
                try:
                    row_ctx = ctx
                    cloud_llm_binding = None
                    if cloud_payer is not None:
                        if (
                            checkpoint_ref is None
                            or cloud_request_id is None
                            or attempt is None
                            or attempt.routed is None
                        ):
                            raise ActionProgramInvocationHalt(
                                "promise_violation",
                                "Cloud execution requires a durable admitted row effect",
                            )
                        if self.execution_composition is None:
                            raise ActionProgramInvocationHalt(
                                "promise_violation",
                                "Cloud execution transport is unavailable",
                            )
                        runtime_binding = cloud_runtime_binding(
                            self.project,
                            self.execution_composition,
                            attempt,
                            row_id=row_id,
                            request_id=cloud_request_id,
                            group_key=f"map:{cloud_request_id}",
                            action_kind=spec["action_kind"],
                            fence=current_writer_fence(),
                        )
                        if runtime_binding is None:
                            raise ActionProgramInvocationHalt(
                                "promise_violation",
                                "Cloud execution lost its admitted payer",
                            )
                        if runtime_binding.media_binding is not None:
                            row_ctx = replace(
                                ctx,
                                cloud_execution=runtime_binding.media_binding,
                            )
                        else:
                            cloud_llm_coordinator = runtime_binding.llm_coordinator
                            cloud_llm_binding = runtime_binding.llm_binding
                    with (
                        cloud_llm_execution_scope(cloud_llm_binding)
                        if cloud_llm_binding is not None
                        else nullcontext()
                    ):
                        results = await execute_row(
                            self.project,
                            self.router,
                            self._throttle,
                            action_program,
                            values,
                            spec,
                            row_ctx,
                            row_id=row_id,
                            recorder=recorder,
                            output_field_names=tuple(out_cols),
                            required_output_field_names=required_output_field_names,
                            managed_publication=True,
                        )
                except NetworkDisabled:
                    if checkpoint_created:
                        assert checkpoint_ref is not None
                        self.run_store.discard_reserved_row_effect_checkpoint(
                            checkpoint_ref,
                            fence=current_writer_fence(),
                        )
                    raise
                except BaseException:
                    # Only statically local rows prove no external effect occurred.
                    if checkpoint_created and row_effect_cannot_egress(
                        action_program, spec
                    ):
                        assert checkpoint_ref is not None
                        self.run_store.discard_reserved_row_effect_checkpoint(
                            checkpoint_ref,
                            fence=current_writer_fence(),
                        )
                    raise
                if callable(capture_result_evidence) and input_capture is not None:
                    capture_result_evidence(
                        values,
                        input_capture,
                        results,
                        spec,
                        run_id=run_id,
                        change_id=change_id,
                        row_id=row_id,
                        output_columns=out_cols,
                    )
                if consumed_capture is not None:
                    # This rides the returned-response checkpoint, so recovery
                    # publishes the inputs that actually produced that response.
                    # One input bundle is shared by every output in this row.
                    for name, payload in results.items():
                        if name in out_cols:
                            payload["__consumed_sources"] = consumed_capture
                            break
                if checkpoint_should_complete:
                    assert checkpoint_ref is not None
                    supplemental_accounting: tuple[dict[str, Any], ...] = ()
                    if cloud_llm_coordinator is not None:
                        if attempt is None or attempt.routed is None:
                            raise ActionProgramInvocationHalt(
                                "promise_violation",
                                "Cloud LLM accounting lost its admitted route",
                            )
                        accounting_call_ids = {
                            str(call.get("id"))
                            for entry in cloud_llm_coordinator.returned_accounting
                            for call in entry.get("model_calls", ())
                            if isinstance(call, dict) and call.get("id")
                        }
                        for result in results.values():
                            result["model_calls"] = [
                                bind_fact_to_route(attempt.routed.route, call)
                                if isinstance(call, dict)
                                and str(call.get("id")) in accounting_call_ids
                                else call
                                for call in result.get("model_calls", ())
                            ]
                    returned_batch = [
                        {
                            "row_id": row_id,
                            "column_id": out_cols[fname],
                            **payload,
                        }
                        for fname, payload in results.items()
                        if fname in out_cols
                    ]
                    replay_response = _accounted_replay_response(results)
                    if (
                        cloud_llm_coordinator is not None
                        and attempt is not None
                        and attempt.routed is not None
                    ):
                        returned_call_ids = {
                            str(call.get("id"))
                            for result in returned_batch
                            for call in result.get("model_calls", ())
                            if isinstance(call, dict) and call.get("id")
                        }
                        missing_accounting: list[dict[str, Any]] = []
                        for entry in cloud_llm_coordinator.returned_accounting:
                            calls = [
                                bind_fact_to_route(attempt.routed.route, call)
                                for call in entry.get("model_calls", ())
                                if str(call.get("id")) not in returned_call_ids
                            ]
                            if calls:
                                missing_accounting.append(
                                    {
                                        "row_id": row_id,
                                        "column_id": next(
                                            iter(out_cols.values()), None
                                        ),
                                        **entry,
                                        "model_calls": calls,
                                    }
                                )
                        supplemental_accounting = tuple(missing_accounting)
                    progress.cost += self.run_store.complete_row_effect_checkpoint(
                        checkpoint_ref,
                        batch=returned_batch,
                        replay_response=replay_response,
                        child_checkpoints=(
                            cloud_llm_coordinator.returned_checkpoints
                            if cloud_llm_coordinator is not None
                            else ()
                        ),
                        supplemental_accounting=supplemental_accounting,
                        fence=current_writer_fence(),
                    )
                    results = replay_response
                    # A cancellation during provider retry may carry paid usage.
                    # Save it first, then propagate task cancellation before
                    # publication or another dispatch (including synchronous
                    # test/local adapters that have not yielded yet).
                    await asyncio.sleep(0)
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise asyncio.CancelledError
            async with lock:
                returned_batch = [
                    {
                        "row_id": row_id,
                        "column_id": out_cols[fname],
                        **payload,
                    }
                    for fname, payload in results.items()
                    if fname in out_cols
                ]
                # Stop future dispatch, but retain this already-submitted work.
                # The ordinary result writer still checks the attempt/claim owner.
                cancelled()
                if checkpoint_ref is None:
                    pending.extend(returned_batch)
                    pending_row_count += 1
                else:
                    persist_result_batch(returned_batch, checkpoint=checkpoint_ref)
                failed_results = [
                    p
                    for p in results.values()
                    if p.get("error")
                    or p.get("outcome") in FAILURE_OUTCOMES + TERMINAL_FAILURE_OUTCOMES
                ]
                err = next(
                    (p.get("error") for p in failed_results if p.get("error")),
                    None,
                )
                if failed_results:
                    progress.failed += 1
                # Evaluate the failure streak in source order, not race order.
                threshold = (
                    None
                    if action_program.disable_failure_halt
                    else self.halt_after_consecutive_failures
                )
                if threshold is not None:
                    streak_outcomes[seq] = any(
                        p.get("outcome") not in {EXPECTED_ROW_ERROR, "not_completed"}
                        for p in failed_results
                    )
                    while streak_next[0] in streak_outcomes:
                        failed_here = streak_outcomes.pop(streak_next[0])
                        streak_next[0] += 1
                        progress.failure_streak = (
                            progress.failure_streak + 1 if failed_here else 0
                        )
                    if progress.failure_streak >= threshold and not progress.cancelled:
                        progress.halted_reason = (
                            f"halted after {progress.failure_streak} consecutive "
                            f"row failures; latest: {err or 'earlier row error'}"
                        )
                        progress.cancel()
                progress.completed += 1
                progress.cost += sum(p.get("cost") or 0 for p in results.values())
            if checkpoint_id is None:
                await flush(force=progress.cancelled)
            elif self.on_progress:
                self.on_progress(progress)

        async def source_rows():
            if prepared.run_scope_run_id is None:
                for row_id in row_ids:
                    yield row_id
                return
            after_position = -1
            while True:
                page = self.run_store.pending_run_row_scope_page_after(
                    prepared.run_scope_run_id,
                    output_column_ids,
                    after_position=after_position,
                    limit=RUN_SCOPE_PAGE_SIZE,
                )
                if not page:
                    return
                after_position = page[-1][0]
                for _position, row_id in page:
                    yield row_id

        worker_count = self._row_worker_count(action_program, spec)
        # Small prefetch buffer: enough to keep workers fed without letting the
        # event loop accumulate row tasks proportional to the run size.
        queue: asyncio.Queue[tuple[int, int, RowSourcePage | None] | None] = (
            asyncio.Queue(maxsize=worker_count * 2)
        )

        async def producer() -> None:
            seq = 0
            page: list[int] = []
            page_size = LOCAL_RESULT_BATCH_ROWS if batch_local_results else 1

            async def enqueue_page() -> None:
                nonlocal seq
                source_page = (
                    read_source_page(
                        self.project,
                        prepared.sheet_id,
                        source_column_ids,
                        page,
                        preserve_invalid_column_ids=preserve_invalid_column_ids,
                    )
                    if batch_local_results
                    else None
                )
                for row_id in page:
                    if cancelled():
                        break
                    await queue.put((row_id, seq, source_page))
                    seq += 1

            async for row_id in source_rows():
                if cancelled():
                    break
                page.append(row_id)
                if len(page) == page_size:
                    await enqueue_page()
                    page.clear()
            if page and not cancelled():
                await enqueue_page()
            # Sentinels belong only to normal exhaustion/cooperative stop.  On
            # an exception or task cancellation the owner cancels and joins all
            # consumers; attempting to fill a full queue from ``finally`` can
            # otherwise deadlock forever after a consumer has died.
            for _ in range(worker_count):
                await queue.put(None)

        async def worker() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    try:
                        await one_row(*item)
                    finally:
                        clear_work_progress(item[0])
                finally:
                    queue.task_done()

        # Give an already-latched operator cancellation precedence over program
        # admission.  Tasks are created inside the scope so task-local program
        # resources (ContextVars) are inherited by every row worker.
        teardown_error: SandboxTeardownError | None = None
        outer_cancellation: asyncio.CancelledError | None = None
        if prepared.row_count == 0 and defer_generation_seal and not cancelled():
            # Coupled publication still owns a write even when there are no row
            # effects. Leave it the same claimed writer as a nonempty row stage.
            claim_the_attempt()
        if prepared.row_count > 0 and not cancelled():
            try:
                # The attempt claim (§1.3): nothing has executed yet, so a lost
                # CAS refuses with zero rows dispatched. Same ONE helper the
                # batch branch above uses — it no-ops if that branch ran it.
                claim_the_attempt()

                async def execute_in_scope() -> None:
                    async with action_program.execution_scope(
                        spec, ctx, expected_rows=prepared.row_count
                    ):
                        try:
                            await _await_owned_tasks(
                                producer(),
                                *(worker() for _ in range(worker_count)),
                            )
                        finally:
                            # Drain accepted local rows and concurrent siblings
                            # before scope cleanup and terminal run/receipt state.
                            await flush()

                scope_task = asyncio.create_task(execute_in_scope())
                try:
                    await asyncio.shield(scope_task)
                except asyncio.CancelledError:
                    await _cancel_and_join_owned_task(scope_task)
                    raise
            except asyncio.CancelledError as exc:
                # Worker join, final flush, and execution-scope exit completed
                # under the shield above. A cooperative stop returns through
                # the ordinary finalizer; actual owner-task cancellation must
                # still propagate even when durable intent is also present.
                cancelled()
                cooperative_stop = progress.cancelled
                progress.cancel()
                current_task = asyncio.current_task()
                if not cooperative_stop or (
                    current_task is not None and current_task.cancelling()
                ):
                    outer_cancellation = exc
            except ActionProgramInvocationHalt as exc:
                progress.halted_code, progress.halted_reason = (
                    normalize_action_program_invocation_halt(exc.code, exc.detail)
                )
                progress.cancel()
            except SandboxTeardownError as exc:
                # The process tree may still be alive, so this infrastructure
                # exception continues escaping after every sibling is joined.
                # First terminalize the durable run resumably so direct and
                # queued action boundaries can preserve a typed receipt.
                progress.halted_code, progress.halted_reason = (
                    normalize_action_program_invocation_halt(
                        "local_session_failed",
                        "The local model session could not be verified as "
                        "stopped; restart Frisket before resuming the run.",
                    )
                )
                progress.cancel()
                teardown_error = exc
        # Preserve the deterministic-program review behavior, but never mark
        # results verified after an invocation/session halt or an unprovable
        # sandbox teardown.
        # a cancelled run keeps its partial results and lands in
        # status='cancelled' (resumable later via backfill, which re-runs
        # only the rows without results). Cancellation remains authoritative
        # until the terminal transaction, including after the final row.
        cancelled()
        truly_cancelled = (
            publication_cancelled
            or progress.halted_code is not None
            or progress.cancelled
        )
        cooperative_cancel = bool(
            self.defer_cancel_terminal_close
            and progress.cancel_requested
            and truly_cancelled
        )
        if attempt is not None and claimed[0]:
            finalize_dispatched_run(
                self.project,
                self.run_store,
                change_id=change_id,
                status="cancelled" if truly_cancelled else "completed",
                out_cols=out_cols,
                keep_zero_success_generated_columns=(
                    action_program.keep_zero_success_generated_columns
                ),
                halted_code=progress.halted_code,
                halted_reason=progress.halted_reason,
                auto_verify=bool(
                    action_program.auto_verify
                    and progress.halted_code is None
                    and teardown_error is None
                ),
                fence=current_writer_fence(),
                defer_terminal_status=cooperative_cancel,
                defer_generation_seal=defer_generation_seal,
                terminal_attempt_state=(
                    None
                    if outer_cancellation is not None
                    or (defer_attempt_close and teardown_error is None)
                    or cooperative_cancel
                    else ("halted" if progress.halted_code is not None else "effected")
                ),
            )
        elif not cooperative_cancel:
            finalize_pre_dispatch_run(
                self.project,
                self.run_store,
                change_id=change_id,
                run_id=run_id,
                status="cancelled" if truly_cancelled else "completed",
                out_cols=out_cols,
                fence=pre_dispatch_fence,
                keep_zero_success_generated_columns=(
                    action_program.keep_zero_success_generated_columns
                ),
                halted_code=progress.halted_code,
                halted_reason=progress.halted_reason,
                auto_verify=bool(
                    action_program.auto_verify
                    and progress.halted_code is None
                    and teardown_error is None
                ),
                defer_generation_seal=defer_generation_seal,
                terminal_attempt_id=(
                    attempt.attempt_id if attempt is not None else None
                ),
                terminal_attempt_state=(
                    ("halted" if progress.halted_code is not None else "effected")
                    if attempt is not None
                    else None
                ),
            )
        progress.done = True
        if self.on_progress:
            self.on_progress(progress)
        if teardown_error is not None:
            teardown_error.run_id = run_id
            raise teardown_error
        if outer_cancellation is not None:
            raise outer_cancellation
        return progress

    def prepare_preview(
        self,
        spec: dict,
        *,
        program: ActionProgram | None = None,
        allow_empty_scope: bool = False,
        accounted: bool = False,
    ) -> validation._ValidatedSpec:
        """Validate a bounded preview, including the ordinary exact consent gate.

        Accounted preparation returns durable resolution facts for the caller
        to reserve a receipt and admit an attempt; it never executes rows.
        """
        return _preview_validated(
            self.project,
            self.router,
            self.run_store,
            spec,
            program=program,
            pricing_policy=self.pricing_policy,
            composition=_require_execution_composition(self.execution_composition),
            consent_coverage=self.consent_coverage,
            allow_empty_scope=allow_empty_scope,
            accounted=accounted,
        )

    def preview_precheck(
        self,
        spec: dict,
        *,
        program: ActionProgram | None = None,
        allow_empty_scope: bool = False,
    ) -> int:
        """Run the preview guards synchronously (cap + ``validate_spec``)
        WITHOUT executing rows, so the server can surface categorical effect
        refusal, a retained free-sidecar CostGate, and existing structural
        errors as a 4xx before spawning the async job thread.
        Raises on failure; returns the visibility-resolved sample size (the
        progress ``total`` the job will report). Facade over
        frisket.runner.preview._preview_validated."""
        return len(
            _preview_validated(
                self.project,
                self.router,
                self.run_store,
                spec,
                program=program,
                pricing_policy=self.pricing_policy,
                composition=_require_execution_composition(self.execution_composition),
                consent_coverage=getattr(self, "consent_coverage", None),
                allow_empty_scope=allow_empty_scope,
            ).row_ids
        )

    async def preview(
        self,
        spec: dict,
        *,
        program: ActionProgram | None = None,
        allow_empty_scope: bool = False,
        progress_cb: Callable[[int, int], None] | None = None,
        cancel_event: Any | None = None,
        attempt: AttemptCommitment | None = None,
    ) -> PreviewResult:
        """Compute ephemeral values; an admitted receipt attempt retains accounting.

        Facade over frisket.runner.preview.run_preview. The caller owns the
        receipt lifecycle and terminalizes only after this joined execution.
        """
        return await run_preview(
            self.project,
            self.router,
            self.run_store,
            self._throttle,
            self.action_context_extras,
            self._row_worker_count,
            spec,
            program=program,
            pricing_policy=self.pricing_policy,
            composition=_require_execution_composition(self.execution_composition),
            consent_coverage=self.consent_coverage,
            allow_empty_scope=allow_empty_scope,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            attempt=attempt,
        )

    def _attempt_scope(
        self, prepared: PreparedRun, spec: dict, output_column_ids: list[int]
    ) -> tuple[int, ...]:
        """The rows this attempt will execute, frozen
        at ``created``.

        It has to be the rows dispatch will actually walk, not the spec's
        ``row_ids`` key — a run-scoped run leaves that empty and pages the
        PENDING scope instead, so recording the key would tell a receipt
        reader "this attempt covered no rows" about a run that transcribed
        forty. Mirrors ``source_rows`` exactly, so the attempt's answer to
        "which rows did it cover" and
        the dispatcher's answer are one computation apart, not two policies.
        """
        if prepared.run_scope_run_id is None:
            return tuple(prepared.row_ids)
        pending = self._pending_run_scope_row_ids(
            prepared.run_scope_run_id, output_column_ids
        )
        return tuple(pending)

    def _pending_run_scope_row_ids(
        self, run_id: int, output_column_ids: list[int]
    ) -> list[int]:
        row_ids: list[int] = []
        after_position = -1
        while True:
            page = self.run_store.pending_run_row_scope_page_after(
                run_id,
                output_column_ids,
                after_position=after_position,
                limit=RUN_SCOPE_PAGE_SIZE,
            )
            if not page:
                return row_ids
            after_position = page[-1][0]
            row_ids.extend(row_id for _position, row_id in page)

    async def _run_batch_program(
        self,
        program: ActionProgram,
        spec: dict,
        ctx: ActionContext,
        *,
        row_ids: list[int],
        col_map: dict[str, int],
        out_cols: dict[str, int],
        column_types: dict[str, str],
        persist_result_batch: Callable[[list[dict[str, Any]]], None],
        progress: RunProgress,
        required_field_names: frozenset[str],
        admitted_work_scope: Mapping[str, Any] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> RunProgress:
        """Execute a deterministic program that can batch its own external I/O.

        Most programs stay row-oriented. This hook exists for ops like Census
        demographics where correctness and cost depend on deduping a whole run
        before making external API calls.
        """
        # ``out_cols`` is the persisted preparation/claim enumeration.  A
        # resumed worker must not recompute dynamic descriptors from changed
        # program code after authority was published.
        batch_size = getattr(program, "batch_size", None)
        if batch_size is not None and len(row_ids) > batch_size:
            for start in range(0, len(row_ids), batch_size):
                if cancelled and cancelled():
                    break
                await self._run_batch_program(
                    program,
                    spec,
                    ctx,
                    row_ids=row_ids[start : start + batch_size],
                    col_map=col_map,
                    out_cols=out_cols,
                    column_types=column_types,
                    persist_result_batch=persist_result_batch,
                    progress=progress,
                    required_field_names=required_field_names,
                    admitted_work_scope=admitted_work_scope,
                    cancelled=cancelled,
                )
            return progress
        field_names = list(out_cols)
        from frisket.engine.store.consumed_sources import read_consumed_source
        from frisket.engine.runner.row_inputs import RowSourcePage

        staged_capture = getattr(program, "staged_input_capture", None)
        if staged_capture is not None:
            # The existing inferred-batch spool owns both classifications and
            # the source capture that produced its saved results.
            batch_capture = staged_capture
            values_by_row = {row_id: {} for row_id in row_ids}
        else:
            source_ids = [
                col_map[name]
                for name in program.source_columns(spec)
                if name in col_map
            ]
            refs: dict[int, dict[int, dict[str, Any]]] = {}
            _, captured_values, batch_capture = read_consumed_source(
                self.project,
                spec["sheet_id"],
                source_ids,
                row_ids=row_ids,
                scope_intent="selected",
                refs_out=refs,
                preserve_invalid_column_ids=[
                    col_map[name]
                    for name in getattr(program, "preserve_invalid_source_columns", ())
                    if name in col_map
                ],
            )
            source_page = RowSourcePage(
                spec["sheet_id"],
                frozenset(row_ids),
                {cid: (captured_values[cid], refs[cid]) for cid in source_ids},
            )
            values_by_row = {
                row_id: row_values(
                    self.project,
                    program,
                    spec,
                    col_map,
                    row_id,
                    for_model=False,
                    column_types=column_types,
                    admitted_work_scope=admitted_work_scope,
                    source_page=source_page,
                )
                for row_id in row_ids
            }
        if cancelled and cancelled():
            raw_results: dict[int, Any] = {}
        else:
            try:
                ready = {
                    row: values
                    for row, values in values_by_row.items()
                    if not getattr(values, "unavailable_inputs", ())
                }
                raw_results = {}
                # An empty admitted batch is still a valid whole-column operation
                # (for example, clustering records an empty result). Do not confuse
                # it with a nonempty batch whose inputs are all unavailable.
                if ready or not values_by_row:
                    async with program.execution_scope(
                        spec, ctx, expected_rows=len(ready)
                    ):
                        raw_results = await program.execute_batch(ready, spec, ctx)  # type: ignore[attr-defined]
            except SandboxTeardownError:
                raise
            except StaleAttemptWriter:
                raise
            except (ValueError, RuntimeError) as e:
                raw_results = {
                    row_id: {
                        "__error__": safe_error("model_error", e, max_chars=500).detail
                    }
                    for row_id in row_ids
                }
        pending: list[dict[str, Any]] = []

        def flush(force: bool = False) -> None:
            if pending and (force or len({r["row_id"] for r in pending}) >= BATCH_SIZE):
                persist_result_batch(list(pending))
                pending.clear()
                if self.on_progress:
                    self.on_progress(progress)

        for row_id in row_ids:
            if cancelled and cancelled() and row_id not in raw_results:
                continue
            missing_is_error = not (bool(cancelled) and progress.cancelled)
            unavailable = getattr(values_by_row[row_id], "unavailable_inputs", ())
            if unavailable:
                from frisket.engine.store.input_availability import not_completed_result

                cells = {
                    name: not_completed_result(unavailable[0]) for name in field_names
                }
                failed, cost = True, 0.0
            else:
                cells, failed, cost = normalize_batch_row(
                    raw_results.get(row_id),
                    field_names,
                    missing_is_error=missing_is_error,
                    required_field_names=required_field_names,
                    managed_publication=True,
                )
            for name, cell in cells.items():
                payload = {"row_id": row_id, "column_id": out_cols[name], **cell}
                # One shared fingerprint for all outputs of this batch. Rows
                # depend on the whole batch, not just their own source cells.
                capture_row = getattr(program, "staged_input_first_row_id", row_ids[0])
                if row_id == capture_row and name == field_names[0]:
                    payload["__consumed_sources"] = batch_capture
                pending.append(payload)
            if failed:
                progress.failed += 1
            progress.completed += 1
            progress.cost += cost
            flush()

        flush(force=True)
        if cancelled:
            cancelled()
        return progress

    # ---------- helpers ----------

    def _row_count(self, spec: dict) -> int:
        return len(validation.target_rows(self.project, spec))
