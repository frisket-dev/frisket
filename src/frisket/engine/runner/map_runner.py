"""Execute a recipe over rows as a parallel,
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
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from frisket.ai.llm import ModelRouter
from frisket.execution.consent_coverage import (
    ConsentCoverage,
    effective_consent_coverage,
)
from frisket.ops.base import (
    RECIPE_INVOCATION_HALT_CODES,
    OpContext,
    Recipe,
    RecipeInvocationHalt,
    normalize_recipe_invocation_halt,
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
from frisket.engine.runner.row_inputs import row_values
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
from frisket.engine.store.output_claims import ClaimLeaseRenewalFailed
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
from frisket.execution.pricing_policy import PricingPolicy, default_pricing_policy
from frisket.execution.provider import (
    ExecutionComposition,
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.operability.trace import TraceWriter

DEFAULT_CONCURRENCY = 8
# Consecutive row failures before the run halts resumably (successes reset
# the streak).
DEFAULT_FAILURE_HALT_STREAK = 10
BATCH_SIZE = 25
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
    }
)


def _row_effect_identity(
    *,
    run_id: int,
    row_id: int,
    recipe: Recipe,
    spec: dict[str, Any],
    values: dict[str, Any],
    out_cols: dict[str, int],
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
    semantic_spec = {
        str(key): value
        for key, value in spec.items()
        if key not in _ROW_EFFECT_IDENTITY_IGNORED_SPEC_KEYS
    }
    identity_payload = {
        "schema_version": "frisket.map_row_effect_identity.v1",
        **logical,
        "action_kind": spec["action_kind"],
        "action_version": recipe.version,
        "spec": semantic_spec,
        "values": values,
        "output_columns": sorted(
            (str(name), int(column_id)) for name, column_id in out_cols.items()
        ),
    }
    encoded = json.dumps(
        identity_payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return checkpoint_id, hashlib.sha256(encoded).hexdigest()


def _returned_row_response(checkpoint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    response = checkpoint.get("response")
    if not isinstance(response, dict) or not all(
        isinstance(name, str) and isinstance(payload, dict)
        for name, payload in response.items()
    ):
        raise RecipeInvocationHalt(
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
            # A repeated outer cancellation must not detach recipe cleanup.
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
    # breakers and typed recipe halts retain their existing close paths.
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


@dataclass
class MapRunner:
    project: Project
    router: ModelRouter
    concurrency: int = DEFAULT_CONCURRENCY
    on_progress: Callable[[RunProgress], None] | None = None
    should_cancel: Callable[[int], bool] | None = None
    defer_cancel_terminal_close: bool = field(kw_only=True, default=False)
    op_context_extras: dict[str, Any] = field(default_factory=dict)
    allow_action_lifecycle_only_recipes: bool = False
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

    def _row_worker_count(self, recipe: Recipe, spec: dict) -> int:
        configured = max(1, int(self.concurrency or 1))
        # ``max_concurrency`` remains the established static recipe cap used
        # by existing temporal/media recipes. ``max_row_concurrency`` adds a
        # spec-dependent cap for run-scoped local workers (for example,
        # Parakeet). Both are independent safety limits.
        caps = [configured]
        if recipe.max_concurrency is not None:
            caps.append(max(1, int(recipe.max_concurrency)))
        dynamic_cap = recipe.max_row_concurrency(spec)
        if dynamic_cap is not None:
            caps.append(max(1, int(dynamic_cap)))
        return min(caps)

    def estimate(self, spec: dict, *, program: Recipe | None = None) -> dict:
        """Pre-run estimate for the cost gate. Facade over validation.estimate_run
        (public — server/services/action_previews.py and workbench call this)."""
        assert self.execution_composition is not None
        return validation.estimate_run(
            self.project,
            spec,
            program=program,
            composition=self.execution_composition,
            pricing_policy=self.pricing_policy,
            consent_coverage=self.consent_coverage,
        )

    def exact_replay_available(
        self,
        spec: dict[str, Any],
        *,
        program: Recipe | None = None,
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
        program: Recipe | None = None,
        confirmed: bool = False,
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
        )
        progress = RunProgress(run_id=prepared.run_id, total=prepared.row_count)
        if self.on_progress:
            self.on_progress(progress)
        return progress

    def _prepare(
        self,
        spec: dict,
        *,
        program: Recipe | None = None,
        confirmed: bool,
        resume_run_id: int | None,
        precomputed_output_fields: list[dict[str, Any]] | None = None,
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
        )
        if resume_run_id is None and (
            validated.recipe.atomic_output_columns
            or validated.recipe.consumes_resolution
        ):
            # A fresh atomic family is one lifecycle unit: output columns, op,
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
                    defer_commits=True,
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
            prepared = preparation.prepare_validated(
                self.project,
                self.run_store,
                spec,
                validated=validated,
                resume_run_id=resume_run_id,
                defer_commits=False,
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
                defer_commits=True,
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
        program: Recipe | None = None,
        confirmed: bool,
        resume_run_id: int | None,
        precomputed_output_fields: list[dict[str, Any]] | None = None,
    ) -> validation._ValidatedSpec:
        recipe = program if program is not None else validation.recipe_for_spec(spec)
        if (
            recipe.action_lifecycle_only
            and not self.allow_action_lifecycle_only_recipes
        ):
            raise ValueError(
                f"recipe '{recipe.name}' must be run through its supported action"
            )
        return validation.validate_spec(
            self.project,
            self.router,
            self.run_store,
            spec,
            program=recipe,
            confirmed=confirmed,
            resume_run_id=resume_run_id,
            pricing_policy=self.pricing_policy,
            composition=self.execution_composition,
            consent_coverage=self.consent_coverage,
            precomputed_output_fields=precomputed_output_fields,
        )

    def prepare_admitted_run(
        self,
        spec: dict[str, Any],
        *,
        program: Recipe | None = None,
        confirmed: bool,
        output_fields: list[dict[str, Any]],
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
        )
        prepared = self._prepare(
            spec,
            program=program,
            confirmed=confirmed,
            resume_run_id=None,
            precomputed_output_fields=output_fields,
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
        attempt = self.authority.mint(
            recipe=prepared.recipe,
            spec=spec,
            run_id=prepared.run_id,
            scope=self._attempt_scope(prepared, spec, output_column_ids),
            commit=False,
            prepared_binding=prepared_binding,
        )
        if prepared.recipe.consumes_resolution and not isinstance(
            attempt.admission,
            RoutedAdmission,
        ):
            raise DependentChoiceRefusal(
                f"recipe '{prepared.recipe.name}' consumes execution resolution "
                "but its attempt was admitted UNROUTED; routed dispatch never "
                "executes unverified"
            )
        return prepared, attempt

    async def run(
        self,
        spec: dict,
        *,
        program: Recipe | None = None,
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
    ) -> RunProgress:
        if admitted_attempt is not None and admitted_attempt_id is not None:
            raise ValueError(
                "supply either admitted_attempt or admitted_attempt_id, not both"
            )
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
            # direct backfill may intentionally reopen an operator-cancelled
            # run. Queue workers pass False, but an allowlisted internal halt
            # remains resumable. Admission reads current durable state under
            # the store transaction, never callback presence.
            admission = self.run_store.begin_run_resume(
                prepared.run_id,
                reopen_operator_cancelled=reopen_operator_cancelled,
                resumable_halt_codes=RECIPE_INVOCATION_HALT_CODES,
            )
            resume_admitted = admission.admitted
        recipe = prepared.recipe
        capture_result_evidence = getattr(recipe, "capture_result_evidence", None)
        write_result_evidence = getattr(recipe, "write_result_evidence", None)
        required_output_field_names = required_publication_fields(
            recipe,
            prepared.output_fields,
        )
        col_map = prepared.col_map
        row_ids = prepared.row_ids
        out_cols = prepared.out_cols
        output_column_ids = sorted(out_cols.values())
        op_id = prepared.op_id
        run_id = prepared.run_id
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
            if self.should_cancel is not None and self.should_cancel(run_id):
                progress.cancel_requested = True
                progress.cancel()
            return progress.cancelled

        pending: list[dict] = []
        lock = asyncio.Lock()
        source_column_types = {
            c["name"]: c["type"]
            for c in self.project.columns(prepared.sheet_id)
            if c["name"] in set(recipe.source_columns(spec))
        }

        def persist_result_batch(
            batch: list[dict[str, Any]],
            *,
            checkpoint: tuple[str, int, str] | None = None,
        ) -> None:
            writer_attempt_id = attempt.attempt_id if attempt is not None else None

            def write(commit: bool) -> None:
                if checkpoint is None:
                    self.run_store.write_results(
                        run_id,
                        batch,
                        writer_attempt_id=writer_attempt_id,
                        claim_token=claim_token,
                        authorized_attempt_id=writer_attempt_id,
                        claimless_direct_effect=self.claimless_direct_effect,
                        project_heads=(
                            not defer_generation_seal
                            and not callable(write_result_evidence)
                        ),
                        commit=commit,
                    )
                    return
                checkpoint_id, checkpoint_row_id, checkpoint_identity = checkpoint
                self.run_store.consume_returned_row_effect_checkpoint(
                    checkpoint_id,
                    run_id=run_id,
                    row_id=checkpoint_row_id,
                    action_kind=spec["action_kind"],
                    identity=checkpoint_identity,
                    batch=batch,
                    writer_attempt_id=writer_attempt_id,
                    claim_token=claim_token,
                    claimless_direct_effect=self.claimless_direct_effect,
                    project_heads=(
                        not defer_generation_seal
                        and not callable(write_result_evidence)
                    ),
                    commit=commit,
                )

            if not callable(write_result_evidence):
                write(True)
                return
            started_transaction = not self.project.db.in_transaction
            if started_transaction:
                self.project.db.execute("BEGIN IMMEDIATE")
            savepoint = f"map_result_evidence_{uuid.uuid4().hex}"
            self.project.db.execute(f"SAVEPOINT {savepoint}")
            try:
                write(False)
                try:
                    write_result_evidence(
                        self.project,
                        spec,
                        batch=batch,
                        sheet_id=prepared.sheet_id,
                        run_id=run_id,
                        op_id=op_id,
                        output_columns=out_cols,
                        input_column_ids=col_map,
                        claim_token=claim_token,
                    )
                except (StaleAttemptWriter, ClaimLeaseRenewalFailed):
                    raise
                except Exception as exc:
                    raise ResultEvidenceWriteFailed(run_id, writer_attempt_id) from exc
                # A recipe-owned evidence/final-value hook may normalize or
                # withhold result semantics.  Fresh create-mode heads become
                # visible only after that owning savepoint has finalized the
                # value and its sidecars; replacement generations still wait
                # for the all-column seal as before.
                from frisket.engine.store.result_generations import (
                    ResultGenerationStore,
                )

                ResultGenerationStore(
                    self.project
                )._project_written_results_uncommitted(
                    run_id,
                    batch,
                    claim_token=claim_token,
                )
            except BaseException:
                self.project.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self.project.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                if started_transaction:
                    self.project.db.rollback()
                raise
            self.project.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            if started_transaction:
                self.project.db.commit()

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
            if admission is not None:
                self.run_store.revert_run_resume(run_id, admission)
                return
            code, reason = pre_dispatch_refusal_markers(exc)
            finalize_pre_dispatch_run(
                self.project,
                self.run_store,
                op_id=op_id,
                run_id=run_id,
                status="failed",
                out_cols=(
                    out_cols if finalization_out_cols is None else finalization_out_cols
                ),
                generation_claim_token=claim_token,
                carry_forward_atomic_family=recipe.atomic_output_columns,
                keep_zero_success_output_columns=(
                    recipe.keep_zero_success_output_columns
                ),
                halted_code=code,
                halted_reason=reason,
                terminal_attempt_id=(
                    attempt.attempt_id if attempt is not None else None
                ),
                terminal_attempt_state=("halted" if attempt is not None else None),
                never_point_column_ids=never_point_column_ids,
            )

        def terminalize_halt(exc: RecipeInvocationHalt) -> RunProgress:
            """A ``promise_violation`` (or any allowlisted invocation halt)
            raised before a single row dispatched: the run terminalizes as
            CANCELLED with its persisted markers. Recovery is a fresh scoped
            run. Identical to the halt handling on both dispatch branches
            below — the mint simply reaches it first."""
            progress.halted_code, progress.halted_reason = (
                normalize_recipe_invocation_halt(exc.code, exc.detail)
            )
            progress.cancel()
            finalize_pre_dispatch_run(
                self.project,
                self.run_store,
                op_id=op_id,
                run_id=run_id,
                status="cancelled",
                out_cols=out_cols,
                generation_claim_token=claim_token,
                carry_forward_atomic_family=recipe.atomic_output_columns,
                keep_zero_success_output_columns=(
                    recipe.keep_zero_success_output_columns
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
        run_extras = dict(self.op_context_extras)
        try:
            declare_prepared_outputs(
                self.project,
                self.run_store,
                prepared,
                claim_token=claim_token,
                defer_publication=defer_generation_seal,
            )
        except BaseException as exc:
            close_out_pre_dispatch_failure(
                exc,
                finalization_out_cols={
                    name: column_id
                    for name, column_id in out_cols.items()
                    if int(column_id) in prepared.created_output_column_ids
                },
                never_point_column_ids=frozenset(
                    int(column_id) for column_id in prepared.out_cols.values()
                ),
            )
            raise
        if not cancelled():
            try:
                if attempt is None:
                    if admitted_attempt_id is not None:
                        attempt = self.authority.load_admitted(
                            attempt_id=admitted_attempt_id,
                            recipe=recipe,
                            spec=spec,
                            run_id=run_id,
                        )
                    else:
                        attempt = self.authority.mint(
                            recipe=recipe,
                            spec=spec,
                            run_id=run_id,
                            scope=self._attempt_scope(
                                prepared,
                                spec,
                                output_column_ids,
                            ),
                        )
                if recipe.consumes_resolution and not isinstance(
                    attempt.admission, RoutedAdmission
                ):
                    raise DependentChoiceRefusal(
                        f"recipe '{recipe.name}' consumes execution resolution "
                        "but its attempt was admitted UNROUTED; routed "
                        "dispatch never executes unverified"
                    )
            except RecipeInvocationHalt as exc:
                return terminalize_halt(exc)
            except BaseException as exc:
                close_out_pre_dispatch_failure(exc)
                raise
            run_extras[ATTEMPT_EXTRA] = attempt
            progress.writer_attempt_id = attempt.attempt_id

        claimed = [False]

        def claim_the_attempt() -> None:
            """The compare-and-swap of §1.3, taken exactly once before the
            first effect — the earliest operation that discloses user data
            externally or incurs provider cost.

            ``RecipeInvocationHalt`` never originates here; a lost CAS is the
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
                    claim_token=claim_token,
                    output_column_ids=frozenset(output_column_ids),
                    claimless_direct_effect=self.claimless_direct_effect,
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

        # remote-engine ops (ocr's VLM tier) reach the router via extras
        ctx = OpContext(
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
                "source_column_types": source_column_types,
                "output_columns": dict(out_cols),
                # Shared only by row contexts in this run; recipe singletons
                # must not hold mutable execution state.
                "run_state": {},
            },
        )
        if not recipe.is_llm(spec) and hasattr(recipe, "execute_batch"):
            # Batch recipes intentionally keep whole-run semantics here rather
            # than any page-local execution shape: clean_column needs global
            # canonical grouping, and census demographics needs global dedupe.
            if prepared.run_scope_run_id is not None and not row_ids:
                row_ids = self._pending_run_scope_row_ids(
                    prepared.run_scope_run_id, output_column_ids
                )
            try:
                if not cancelled():
                    # The claim happens here too, before the batch branch
                    # returns — a batch recipe that consumes resolution is
                    # authorized exactly like a row-oriented one.
                    claim_the_attempt()
                batch_task = asyncio.create_task(
                    self._run_batch_recipe(
                        recipe,
                        spec,
                        ctx,
                        row_ids=row_ids,
                        col_map=col_map,
                        out_cols=out_cols,
                        column_types=source_column_types,
                        op_id=op_id,
                        run_id=run_id,
                        writer_attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        claim_token=claim_token,
                        progress=progress,
                        required_field_names=required_output_field_names,
                        admitted_work_scope=(
                            attempt.routed.work_scope_snapshot
                            if attempt is not None and attempt.routed is not None
                            else None
                        ),
                        cancelled=cancelled,
                    )
                )
                try:
                    batch_progress = await asyncio.shield(batch_task)
                except asyncio.CancelledError:
                    cooperative_stop = progress.cancelled
                    await _cancel_and_join_owned_task(batch_task)
                    if not cooperative_stop or asyncio.current_task().cancelling():
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
                        op_id=op_id,
                        run_id=run_id,
                        status=(
                            "cancelled"
                            if batch_progress.cancelled
                            and batch_progress.completed < len(row_ids)
                            else "completed"
                        ),
                        out_cols=out_cols,
                        carry_forward_atomic_family=recipe.atomic_output_columns,
                        keep_zero_success_output_columns=(
                            recipe.keep_zero_success_output_columns
                        ),
                        auto_verify=bool(recipe.auto_verify),
                        writer_attempt_id=attempt.attempt_id,
                        claim_token=claim_token,
                        claimless_direct_effect=self.claimless_direct_effect,
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
                        op_id=op_id,
                        run_id=run_id,
                        status=(
                            "cancelled"
                            if batch_progress.cancelled
                            and batch_progress.completed < len(row_ids)
                            else "completed"
                        ),
                        out_cols=out_cols,
                        generation_claim_token=claim_token,
                        carry_forward_atomic_family=recipe.atomic_output_columns,
                        keep_zero_success_output_columns=(
                            recipe.keep_zero_success_output_columns
                        ),
                        auto_verify=bool(recipe.auto_verify),
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
                        op_id=op_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        carry_forward_atomic_family=recipe.atomic_output_columns,
                        keep_zero_success_output_columns=(
                            recipe.keep_zero_success_output_columns
                        ),
                        writer_attempt_id=attempt.attempt_id,
                        claim_token=claim_token,
                        claimless_direct_effect=self.claimless_direct_effect,
                        terminal_attempt_state=None,
                    )
                else:
                    finalize_pre_dispatch_run(
                        self.project,
                        self.run_store,
                        op_id=op_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        generation_claim_token=claim_token,
                        carry_forward_atomic_family=recipe.atomic_output_columns,
                        keep_zero_success_output_columns=(
                            recipe.keep_zero_success_output_columns
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
            except RecipeInvocationHalt as exc:
                # The batch path's twin of the row path's halt handling: a
                # promise_violation (or any allowlisted invocation halt)
                # terminalizes this generation as cancelled with its
                # persisted markers. Recovery gets a fresh scoped run.
                progress.halted_code, progress.halted_reason = (
                    normalize_recipe_invocation_halt(exc.code, exc.detail)
                )
                progress.cancel()
                if attempt is not None and claimed[0]:
                    finalize_dispatched_run(
                        self.project,
                        self.run_store,
                        op_id=op_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        carry_forward_atomic_family=recipe.atomic_output_columns,
                        keep_zero_success_output_columns=(
                            recipe.keep_zero_success_output_columns
                        ),
                        halted_code=progress.halted_code,
                        halted_reason=progress.halted_reason,
                        writer_attempt_id=attempt.attempt_id,
                        claim_token=claim_token,
                        claimless_direct_effect=self.claimless_direct_effect,
                        terminal_attempt_state=(
                            None if defer_attempt_close else "halted"
                        ),
                        defer_generation_seal=defer_generation_seal,
                    )
                else:
                    finalize_pre_dispatch_run(
                        self.project,
                        self.run_store,
                        op_id=op_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        generation_claim_token=claim_token,
                        carry_forward_atomic_family=recipe.atomic_output_columns,
                        keep_zero_success_output_columns=(
                            recipe.keep_zero_success_output_columns
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
                    normalize_recipe_invocation_halt(
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
                        op_id=op_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        carry_forward_atomic_family=recipe.atomic_output_columns,
                        keep_zero_success_output_columns=(
                            recipe.keep_zero_success_output_columns
                        ),
                        halted_code=progress.halted_code,
                        halted_reason=progress.halted_reason,
                        writer_attempt_id=attempt.attempt_id,
                        claim_token=claim_token,
                        claimless_direct_effect=self.claimless_direct_effect,
                        terminal_attempt_state="halted",
                    )
                else:
                    finalize_pre_dispatch_run(
                        self.project,
                        self.run_store,
                        op_id=op_id,
                        run_id=run_id,
                        status="cancelled",
                        out_cols=out_cols,
                        generation_claim_token=claim_token,
                        carry_forward_atomic_family=recipe.atomic_output_columns,
                        keep_zero_success_output_columns=(
                            recipe.keep_zero_success_output_columns
                        ),
                        halted_code=progress.halted_code,
                        halted_reason=progress.halted_reason,
                    )
                progress.done = True
                if self.on_progress:
                    self.on_progress(progress)
                exc.run_id = run_id
                raise

        async def flush() -> None:
            # Persist each completed row so queued progress remains observable.
            batch: list[dict] | None = None
            async with lock:
                if pending:
                    batch = list(pending)
                    pending.clear()
            if batch is None:
                return
            persist_result_batch(batch)
            if self.on_progress:
                self.on_progress(progress)

        streak_outcomes: dict[int, bool] = {}
        streak_next = [0]

        async def one_row(row_id: int, seq: int) -> None:
            if cancelled():
                return
            await self._throttle.wait()
            if cancelled():
                return
            input_capture: dict[str, dict[str, Any]] | None = (
                {} if callable(capture_result_evidence) else None
            )
            values = row_values(
                self.project,
                recipe,
                spec,
                col_map,
                row_id,
                for_model=recipe.is_llm(spec),
                column_types=source_column_types,
                capture=input_capture,
                admitted_work_scope=(
                    attempt.routed.work_scope_snapshot
                    if attempt is not None and attempt.routed is not None
                    else None
                ),
            )
            checkpoint_id: str | None = None
            checkpoint_identity: str | None = None
            checkpoint_created = False
            recovered_response = False
            results: dict[str, dict[str, Any]]
            if (
                row_effect_spends_or_meters(recipe, spec, self.router)
                and getattr(recipe, "row_effect_checkpoint_owner", "runner") == "runner"
            ):
                checkpoint_id, checkpoint_identity = _row_effect_identity(
                    run_id=run_id,
                    row_id=row_id,
                    recipe=recipe,
                    spec=spec,
                    values=values,
                    out_cols=out_cols,
                )
                checkpoint = self.run_store.row_effect_checkpoint(run_id, row_id)
                if checkpoint is None:
                    checkpoint_created = self.run_store.reserve_row_effect_checkpoint(
                        checkpoint_id,
                        run_id=run_id,
                        row_id=row_id,
                        action_kind=spec["action_kind"],
                        identity=checkpoint_identity,
                        authorized_attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        writer_attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        claim_token=claim_token,
                        claimless_direct_effect=self.claimless_direct_effect,
                    )
                    if not checkpoint_created:
                        checkpoint = self.run_store.row_effect_checkpoint(
                            run_id, row_id
                        )
                if not checkpoint_created:
                    if checkpoint is None:
                        raise RecipeInvocationHalt(
                            "external_effect_reconciliation_required",
                            "A row-effect reservation raced or disappeared; refusing "
                            "another provider call until it is reconciled.",
                        )
                    if (
                        checkpoint.get("action_kind") != spec["action_kind"]
                        or checkpoint.get("identity") != checkpoint_identity
                    ):
                        raise RecipeInvocationHalt(
                            "external_effect_reconciliation_required",
                            "A prior row effect was reserved for a different "
                            "request or source value; refusing to substitute the "
                            "current row or call the provider again.",
                        )
                    if checkpoint.get("state") == "reserved":
                        raise RecipeInvocationHalt(
                            "external_effect_reconciliation_required",
                            "A prior process may have reached the provider for this "
                            "row, but no response was durably recorded; refusing "
                            "another call until the effect is reconciled.",
                        )
                    if checkpoint.get("state") != "returned":
                        raise RecipeInvocationHalt(
                            "external_effect_reconciliation_required",
                            "A row-effect checkpoint has an invalid state; refusing "
                            "another provider call until it is reconciled.",
                        )
                    checkpoint_id = str(checkpoint["id"])
                    results = _returned_row_response(checkpoint)
                    if callable(capture_result_evidence) and input_capture is not None:
                        capture_result_evidence(
                            values,
                            input_capture,
                            results,
                            spec,
                            run_id=run_id,
                            op_id=op_id,
                            row_id=row_id,
                            output_columns=out_cols,
                        )
                    recovered_response = True

            if not recovered_response:
                if checkpoint_created and cancelled():
                    assert checkpoint_id is not None
                    assert checkpoint_identity is not None
                    self.run_store.discard_reserved_row_effect_checkpoint(
                        checkpoint_id,
                        run_id=run_id,
                        row_id=row_id,
                        action_kind=spec["action_kind"],
                        identity=checkpoint_identity,
                        writer_attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        claim_token=claim_token,
                        claimless_direct_effect=self.claimless_direct_effect,
                    )
                    return
                try:
                    results = await execute_row(
                        self.project,
                        self.router,
                        self._throttle,
                        recipe,
                        values,
                        spec,
                        ctx,
                        row_id=row_id,
                        recorder=recorder,
                        output_field_names=tuple(out_cols),
                        required_output_field_names=required_output_field_names,
                        managed_publication=True,
                    )
                except NetworkDisabled:
                    if checkpoint_created:
                        assert checkpoint_id is not None
                        assert checkpoint_identity is not None
                        self.run_store.discard_reserved_row_effect_checkpoint(
                            checkpoint_id,
                            run_id=run_id,
                            row_id=row_id,
                            action_kind=spec["action_kind"],
                            identity=checkpoint_identity,
                            writer_attempt_id=(
                                attempt.attempt_id if attempt is not None else None
                            ),
                            claim_token=claim_token,
                            claimless_direct_effect=self.claimless_direct_effect,
                        )
                    raise
                except BaseException:
                    # Only statically local rows prove no external effect occurred.
                    if checkpoint_created and row_effect_cannot_egress(recipe, spec):
                        assert checkpoint_id is not None
                        assert checkpoint_identity is not None
                        self.run_store.discard_reserved_row_effect_checkpoint(
                            checkpoint_id,
                            run_id=run_id,
                            row_id=row_id,
                            action_kind=spec["action_kind"],
                            identity=checkpoint_identity,
                            writer_attempt_id=(
                                attempt.attempt_id if attempt is not None else None
                            ),
                            claim_token=claim_token,
                            claimless_direct_effect=self.claimless_direct_effect,
                        )
                    raise
                if callable(capture_result_evidence) and input_capture is not None:
                    capture_result_evidence(
                        values,
                        input_capture,
                        results,
                        spec,
                        run_id=run_id,
                        op_id=op_id,
                        row_id=row_id,
                        output_columns=out_cols,
                    )
                if checkpoint_created:
                    assert checkpoint_id is not None
                    assert checkpoint_identity is not None
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
                    progress.cost += self.run_store.complete_row_effect_checkpoint(
                        checkpoint_id,
                        run_id=run_id,
                        row_id=row_id,
                        action_kind=spec["action_kind"],
                        identity=checkpoint_identity,
                        batch=returned_batch,
                        replay_response=replay_response,
                        writer_attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        claim_token=claim_token,
                        claimless_direct_effect=self.claimless_direct_effect,
                    )
                    results = replay_response
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
                # Preserve returned provider accounting even when cancellation
                # seals the generation without publishing this result cell.
                if cancelled():
                    if checkpoint_id is None:
                        progress.cost += self.run_store.write_returned_call_accounting(
                            run_id,
                            returned_batch,
                            writer_attempt_id=(
                                attempt.attempt_id if attempt is not None else None
                            ),
                            claim_token=claim_token,
                            authorized_attempt_id=(
                                attempt.attempt_id if attempt is not None else None
                            ),
                            claimless_direct_effect=self.claimless_direct_effect,
                        )
                    else:
                        assert checkpoint_identity is not None
                        progress.cost += (
                            self.run_store.account_returned_row_effect_checkpoint(
                                checkpoint_id,
                                run_id=run_id,
                                row_id=row_id,
                                action_kind=spec["action_kind"],
                                identity=checkpoint_identity,
                                batch=returned_batch,
                                replay_response=_accounted_replay_response(results),
                                writer_attempt_id=(
                                    attempt.attempt_id if attempt is not None else None
                                ),
                                claim_token=claim_token,
                                claimless_direct_effect=self.claimless_direct_effect,
                            )
                        )
                    return
                if checkpoint_id is None:
                    pending.extend(returned_batch)
                else:
                    assert checkpoint_identity is not None
                    persist_result_batch(
                        returned_batch,
                        checkpoint=(checkpoint_id, row_id, checkpoint_identity),
                    )
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
                    if recipe.disable_failure_halt
                    else self.halt_after_consecutive_failures
                )
                if threshold is not None:
                    streak_outcomes[seq] = any(
                        p.get("outcome") != EXPECTED_ROW_ERROR for p in failed_results
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
                await flush()
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

        worker_count = self._row_worker_count(recipe, spec)
        # Small prefetch buffer: enough to keep workers fed without letting the
        # event loop accumulate row tasks proportional to the run size.
        queue: asyncio.Queue[tuple[int, int] | None] = asyncio.Queue(
            maxsize=worker_count * 2
        )

        async def producer() -> None:
            seq = 0
            async for row_id in source_rows():
                if cancelled():
                    break
                await queue.put((row_id, seq))
                seq += 1
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
                    await one_row(*item)
                finally:
                    queue.task_done()

        # Give an already-latched operator cancellation precedence over recipe
        # admission.  Tasks are created inside the scope so task-local recipe
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
                    async with recipe.execution_scope(
                        spec, ctx, expected_rows=prepared.row_count
                    ):
                        try:
                            await _await_owned_tasks(
                                producer(), *(worker() for _ in range(worker_count))
                            )
                        finally:
                            # Normal rows flush individually; this final drain
                            # keeps any accepted concurrent sibling result
                            # durable while scope cleanup remains guaranteed.
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
                cooperative_stop = progress.cancelled
                progress.cancel()
                if not cooperative_stop or asyncio.current_task().cancelling():
                    outer_cancellation = exc
            except RecipeInvocationHalt as exc:
                progress.halted_code, progress.halted_reason = (
                    normalize_recipe_invocation_halt(exc.code, exc.detail)
                )
                progress.cancel()
            except SandboxTeardownError as exc:
                # The process tree may still be alive, so this infrastructure
                # exception continues escaping after every sibling is joined.
                # First terminalize the durable run resumably so direct and
                # queued action boundaries can preserve a typed receipt.
                progress.halted_code, progress.halted_reason = (
                    normalize_recipe_invocation_halt(
                        "local_session_failed",
                        "The local model session could not be verified as "
                        "stopped; restart Frisket before resuming the run.",
                    )
                )
                progress.cancel()
                teardown_error = exc
        # Preserve the deterministic-recipe review behavior, but never mark
        # results verified after an invocation/session halt or an unprovable
        # sandbox teardown.
        # a cancelled run keeps its partial results and lands in
        # status='cancelled' (resumable later via backfill, which re-runs
        # only the rows without results). A cancel that lands AFTER every row
        # already ran is a no-op: the run completed.
        # progress.completed counts every finished row (failures included —
        # failed is a subset), so it alone says whether work remained
        cancelled()
        truly_cancelled = progress.halted_code is not None or (
            progress.cancelled and progress.completed < prepared.row_count
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
                op_id=op_id,
                run_id=run_id,
                status="cancelled" if truly_cancelled else "completed",
                out_cols=out_cols,
                carry_forward_atomic_family=recipe.atomic_output_columns,
                keep_zero_success_output_columns=(
                    recipe.keep_zero_success_output_columns
                ),
                halted_code=progress.halted_code,
                halted_reason=progress.halted_reason,
                auto_verify=bool(
                    recipe.auto_verify
                    and progress.halted_code is None
                    and teardown_error is None
                ),
                writer_attempt_id=attempt.attempt_id,
                claim_token=claim_token,
                claimless_direct_effect=self.claimless_direct_effect,
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
                op_id=op_id,
                run_id=run_id,
                status="cancelled" if truly_cancelled else "completed",
                out_cols=out_cols,
                generation_claim_token=claim_token,
                carry_forward_atomic_family=recipe.atomic_output_columns,
                keep_zero_success_output_columns=(
                    recipe.keep_zero_success_output_columns
                ),
                halted_code=progress.halted_code,
                halted_reason=progress.halted_reason,
                auto_verify=bool(
                    recipe.auto_verify
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
        program: Recipe | None = None,
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
            composition=self.execution_composition,
            consent_coverage=self.consent_coverage,
            allow_empty_scope=allow_empty_scope,
            accounted=accounted,
        )

    def preview_precheck(
        self,
        spec: dict,
        *,
        program: Recipe | None = None,
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
                composition=self.execution_composition,
                consent_coverage=getattr(self, "consent_coverage", None),
                allow_empty_scope=allow_empty_scope,
            ).row_ids
        )

    async def preview(
        self,
        spec: dict,
        *,
        program: Recipe | None = None,
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
            self.op_context_extras,
            self._row_worker_count,
            spec,
            program=program,
            pricing_policy=self.pricing_policy,
            composition=self.execution_composition,
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

    async def _run_batch_recipe(
        self,
        recipe: Recipe,
        spec: dict,
        ctx: OpContext,
        *,
        row_ids: list[int],
        col_map: dict[str, int],
        out_cols: dict[str, int],
        column_types: dict[str, str],
        op_id: int,
        run_id: int,
        writer_attempt_id: str | None,
        claim_token: str | None,
        progress: RunProgress,
        required_field_names: frozenset[str],
        admitted_work_scope: Mapping[str, Any] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> RunProgress:
        """Execute a deterministic recipe that can batch its own external I/O.

        Most recipes stay row-oriented. This hook exists for ops like Census
        demographics where correctness and cost depend on deduping a whole run
        before making external API calls.
        """
        # ``out_cols`` is the persisted preparation/claim enumeration.  A
        # resumed worker must not recompute dynamic descriptors from changed
        # recipe code after authority was published.
        field_names = list(out_cols)
        values_by_row = {
            row_id: row_values(
                self.project,
                recipe,
                spec,
                col_map,
                row_id,
                for_model=False,
                column_types=column_types,
                admitted_work_scope=admitted_work_scope,
            )
            for row_id in row_ids
        }
        if cancelled and cancelled():
            raw_results: dict[int, Any] = {}
        else:
            try:
                async with recipe.execution_scope(
                    spec, ctx, expected_rows=len(row_ids)
                ):
                    raw_results = await recipe.execute_batch(values_by_row, spec, ctx)  # type: ignore[attr-defined]
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
                self.run_store.write_results(
                    run_id,
                    list(pending),
                    writer_attempt_id=writer_attempt_id,
                    claim_token=claim_token,
                    authorized_attempt_id=writer_attempt_id,
                    claimless_direct_effect=self.claimless_direct_effect,
                )
                pending.clear()
                if self.on_progress:
                    self.on_progress(progress)

        for row_id in row_ids:
            if cancelled and cancelled() and row_id not in raw_results:
                continue
            missing_is_error = not (bool(cancelled) and progress.cancelled)
            cells, failed, cost = normalize_batch_row(
                raw_results.get(row_id),
                field_names,
                missing_is_error=missing_is_error,
                required_field_names=required_field_names,
                managed_publication=True,
            )
            for name, cell in cells.items():
                pending.append({"row_id": row_id, "column_id": out_cols[name], **cell})
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
