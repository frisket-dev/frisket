"""Action executor dependency carriers, lifecycle specs, and handler inventory."""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Integral
from typing import Any, BinaryIO, Callable, Literal, Mapping, Protocol

from pydantic import BaseModel

from frisket.actions.types import EmailInput
from frisket.contracts.action import (
    ActionError,
    ActionOutput,
    ActionResult,
    ActionSpec,
    Receipt,
    ReceiptIO,
)
from frisket.engine.runner import (
    CostGate,
)
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.provider import (
    ExecutionComposition,
)


_MapRunnerFactory = Callable[[Any, Any | None], Any]
_ReservedMaprunnerWriteOverrides = Mapping[str, Callable[..., ActionResult]]
_ConnectedAccountResolver = Callable[[str, str], Mapping[str, Any] | None]


class _ActionExecutionEnvelope(Protocol):
    """Minimal identity consumed by the shared durable action lifecycle.

    Both the v1 ``ActionSpec`` and the code-first typed adapter satisfy this
    protocol.  Keeping the executor seam structural prevents a direct typed
    request from being translated into a legacy action declaration merely to
    reuse reservation, claim, and receipt machinery.
    """

    kind: str
    idempotency_key: str | None
    params: Mapping[str, Any]
    row_scope: Any | None


@dataclass(frozen=True)
class _TypedProjectEnvelope:
    kind: str
    idempotency_key: str
    params: Mapping[str, Any]
    row_scope: None = None
    confirmation: str | None = None


@dataclass(frozen=True)
class UrlImportLimits:
    """Optional deployment policy for the total URLs accepted per import.

    ``None`` intentionally means unlimited so direct library execution has no
    deployment quota. Hosts that need a quota pass an instance through their
    request dependency bag.
    """

    max_urls: int | None = None

    def __post_init__(self) -> None:
        if self.max_urls is not None and (
            isinstance(self.max_urls, bool)
            or not isinstance(self.max_urls, int)
            or self.max_urls < 0
        ):
            raise ValueError("max_urls must be a non-negative integer or None")


@dataclass(frozen=True)
class ImportWorkloadLimits:
    """Optional deployment policy for a fully parsed import workload.

    Direct and Solo execution leave this unset. Hosted compositions may inject
    a finite total-row ceiling without changing the public action contract.
    """

    max_rows: int | None = None

    def __post_init__(self) -> None:
        if self.max_rows is not None and (
            isinstance(self.max_rows, bool)
            or not isinstance(self.max_rows, int)
            or self.max_rows < 0
        ):
            raise ValueError("max_rows must be a non-negative integer or None")


@dataclass(frozen=True)
class CellEditQueryLimits:
    """Optional deployment policy for rows selected by ``cell.edit_query``.

    ``None`` preserves the selector-backed action's direct-library behavior,
    including query edits larger than the inline edit representation threshold.
    """

    max_rows: int | None = None

    def __post_init__(self) -> None:
        if self.max_rows is not None and (
            isinstance(self.max_rows, bool)
            or not isinstance(self.max_rows, int)
            or self.max_rows < 0
        ):
            raise ValueError("max_rows must be a non-negative integer or None")


@dataclass(frozen=True)
class BoundLocalFile:
    """Borrowed, verified binary source; its caller retains close ownership.

    The dependency map key is the actual admitted path. SHA-256 is the original
    admission digest, not a producer claim or a newly computed replacement.
    """

    stream: BinaryIO
    sha256: str


@dataclass(frozen=True)
class ExecutorDeps:
    router: Any | None = None
    # Request-scoped funding/provider/credential carrier.  It is a value on
    # the request's dependency bag, never installed process-wide.
    execution_composition: ExecutionComposition | None = None
    # Request-scoped consent authority. Managed editions bind this to the
    # authenticated user; None retains local installation-owned coverage.
    consent_coverage: ConsentCoverage | None = None
    rss_fetcher: Any | None = None
    enclosure_fetcher: Any | None = None
    url_capture_fetcher: Any | None = None
    url_capture_browser: Any | None = None
    map_runner_factory: _MapRunnerFactory | None = None
    reserved_maprunner_write_overrides: _ReservedMaprunnerWriteOverrides | None = None
    connected_account_resolver: _ConnectedAccountResolver | None = None
    google_sheets_client: Any | None = None
    # Embedding gateway for embedding.index_refresh. Duck-typed (.embed(texts,
    # provider=, model=, modality=) -> EmbeddingBatchResult); tests inject a fake,
    # production defaults to EmbeddingGateway(router=...) at the family edge.
    embedding_gateway: Any | None = None
    local_file_sources: Mapping[str, BoundLocalFile] | None = None
    # Request-local trusted ingress resolves opaque email refs to borrowed
    # streams. Missing/unknown refs never fall back to opening a client path.
    email_sources: Mapping[str, EmailInput] | None = None
    # Deployment-owned limits, deliberately opaque to public executor wiring.
    sheet_export_limits: Any | None = None
    url_import_limits: UrlImportLimits | None = None
    import_workload_limits: ImportWorkloadLimits | None = None
    cell_edit_query_limits: CellEditQueryLimits | None = None
    cancelled: Callable[[], bool] | None = None


@dataclass(frozen=True)
class ExecutorContext:
    project_id: str
    deps: ExecutorDeps
    # Opaque deployment-owned snapshot carried from the typed request
    # composition context to the direct action lifecycles that durably pin it.
    # Public execution never reads its nested fields.
    edition_run_context: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _ReservedMaprunnerActionSpec:
    kind: str
    params_model: type[BaseModel]
    runner_spec_fn: Callable[[Any], dict[str, Any]]
    resolve_fn: Callable[[Any, Any, dict[str, Any]], dict[str, Any] | ActionError]
    precheck_fn: Callable[[Any, Any, dict[str, Any]], ActionError | None]
    write_fn: Callable[..., ActionResult]
    reservation_kind: str
    # optional resume seam: (project, params, resolved) -> the existing run id to RESUME
    # (run.backfill) instead of creating a new run. None (default) keeps the create path:
    # the map runner allocates a fresh run + output columns. When set, the body threads it
    # into MapRunner.run(resume_run_id=...), which skips the create-path estimate/cost-gate
    # and extends the existing run's row scope.
    resume_run_id_fn: Callable[[Any, Any, dict[str, Any]], int | None] | None = None
    # Optional private-program reconstruction for stored runner specs whose
    # executable is intentionally absent from the legacy Recipe registry.
    program_fn: Callable[[Any, dict[str, Any]], Any | ActionError | None] | None = None
    replay_error_fn: (
        Callable[[Any, Any, Receipt, ActionSpec], ActionError | None] | None
    ) = None
    params_hash_fn: Callable[[ActionSpec], str] | None = None
    confirmed_fn: Callable[[Any], bool] | None = None
    needs_confirmation_error_fn: Callable[[ActionSpec, Any], ActionError] | None = None
    cost_gate_error_fn: Callable[[ActionSpec, CostGate], ActionError] | None = None
    map_error_code: str = "map_run_failed"
    log_missed_delete: bool = True


@dataclass(frozen=True)
class _ActionCoreSpec:
    """The one executor-side action lifecycle envelope.

    The core (`_run_action_core_spec`) runs idempotency -> confirmation gate -> the
    execution body once, parameterized by these seams; each lifecycle shape
    (reserved-maprunner today; child-sheet/queued/plain in later steps) becomes a thin
    preset that fills only the seams it uses. Reserve/delete/result_from_existing arrive
    pre-built so the core never reconstructs an owner spec.
    """

    kind: str
    params_model: type[BaseModel]

    # which execution body the core runs after the shared idempotency + confirmation
    # gate. Default keeps step-1 reserved-maprunner specs unchanged.
    body_kind: Literal[
        "reserved_maprunner",
        "child_sheet_deterministic",
        "child_sheet_deterministic_owned_write",
        "child_sheet_model",
        "plain",
    ] = "reserved_maprunner"

    # lifecycle policy
    params_hash_fn: Callable[[ActionSpec], str] | None = None
    needs_confirmation_error_fn: Callable[[ActionSpec, Any], ActionError] | None = None
    confirmed_fn: Callable[[Any], bool] | None = None
    result_from_existing_fn: Callable[..., ActionResult] | None = None
    reserve_fn: Callable[..., dict[str, str] | ActionResult] | None = None
    delete_reservation_fn: Callable[[Any, str], None] | None = None

    # reserved-maprunner execution seams
    runner_spec_fn: Callable[[Any], dict[str, Any]] | None = None
    resolve_fn: (
        Callable[[Any, Any, dict[str, Any]], dict[str, Any] | ActionError] | None
    ) = None
    precheck_fn: Callable[[Any, Any, dict[str, Any]], ActionError | None] | None = None
    reservation_kind: str | None = None
    write_fn: Callable[..., ActionResult] | None = None
    # optional resume seam (see _ReservedMaprunnerActionSpec.resume_run_id_fn): threaded into
    # MapRunner.run(resume_run_id=...). Default None keeps the create path byte-equal.
    resume_run_id_fn: Callable[[Any, Any, dict[str, Any]], int | None] | None = None
    program_fn: Callable[[Any, dict[str, Any]], Any | ActionError | None] | None = None
    cost_gate_error_fn: Callable[[CostGate], ActionError] | None = None
    map_error_code: str = "map_run_failed"
    log_missed_delete: bool = True

    # child-sheet execution seams (set only by child-sheet presets). Shared by both
    # the deterministic (derive.table_from_list) and model (reduce/join) bodies.
    child_sheet_target_name_fn: Callable[[Any], Any] | None = None
    child_sheet_duplicate_error_fn: Callable[[Any, Any], ActionError | None] | None = (
        None
    )
    child_sheet_replay_validate_fn: (
        Callable[[Any, Receipt], ActionError | None] | None
    ) = None
    # deterministic child-sheet body seams
    child_sheet_resolve_fn: (
        Callable[[Any, Any], dict[str, Any] | ActionError] | None
    ) = None
    child_sheet_present_fn: Callable[[dict[str, Any], Any], Any] | None = None
    child_sheet_op_spec_fn: Callable[..., dict[str, Any]] | None = None
    child_sheet_action_outputs_fn: Callable[[Any, Any], list[ActionOutput]] | None = (
        None
    )
    child_sheet_receipt_outputs_fn: (
        Callable[[Any, int, Any], list[ReceiptIO]] | None
    ) = None
    # OPTIONAL op-unique post-write hook:
    # callable(project, resolved, write) -> None, run inside the SAME write
    # transaction right after write_single_parent_child_sheet, before the
    # receipt is built. The child-sheet analog of the reserved-maprunner
    # `write_override` seam -- used only by derive.table_from_list to
    # repoint/copy each derived row's source item's evidence link. None
    # (default) for every other child-sheet op.
    child_sheet_row_evidence_fn: Callable[[Any, dict[str, Any], Any], None] | None = (
        None
    )
    # deterministic op-owned-write child-sheet body seam: the op owns the in-txn write
    # (aggregate materialization + ops-spec update + receipt) and returns the result; the
    # core owns the BEGIN IMMEDIATE/recheck/commit envelope and the action/receipt ids.
    child_sheet_deterministic_write_in_txn_fn: Callable[..., ActionResult] | None = None
    # plain body seams (set only by the plain preset). The plain body has no target-name
    # or duplicate concepts: an optional best-effort pre-txn resolve, then the op-owned
    # in-txn perform that does the in-txn reads/validation + any project writes + the
    # receipt insert and returns the result. The core owns the BEGIN IMMEDIATE/recheck/
    # commit envelope and the action/receipt ids; the outer result_from_existing_fn owns
    # idempotency replay + staleness.
    plain_resolve_fn: Callable[[Any, Any], Any] | None = None
    # When True the pre-txn plain_resolve_fn is called with a keyword ``router``
    # (ctx.deps.router) so it can resolve/warm an embedding backend BEFORE the
    # write txn (no network inside BEGIN IMMEDIATE). Default False keeps the
    # 2-arg (project, params) resolve signature every other plain action uses.
    plain_resolve_needs_router: bool = False
    # Only a lifecycle that persists the opaque edition snapshot opts in.
    plain_resolve_needs_edition_context: bool = False
    plain_perform_in_txn_fn: Callable[..., ActionResult] | None = None
    # commit policy for the plain body. Default False keeps the deterministic plain semantics:
    # commit only a `completed` result, roll back receiptless on any non-completed result.
    # True is for background/scheduled ops (source.poll) that want to PERSIST a failed receipt
    # as a durable trace (no live caller observes the error). The op's perform is responsible,
    # on the persist-failure path, for writing ONLY the failure record (the failed receipt + an
    # error trace) and NOT partial project mutations, so committing the `failed` result is safe.
    plain_persist_failure: bool = False
    # optional seam for the plain body's in-txn exception error. When set, the shared
    # in-txn helper builds the rollback error via this callable instead of the fixed
    # project_write_failed ActionError; lets a plain op preserve its own pre-migration
    # exception code/message. Owned-write leaves it unset (fixed project_write_failed).
    plain_exception_error_fn: Callable[[ActionSpec], ActionError] | None = None
    # optional cleanup-on-rollback seam for plain ops whose perform stages out-of-txn side
    # effects (filesystem artifacts: a pre-txn tmp write, an in-txn os.replace into place).
    # The shared in-txn helper calls it with the resolved value on EVERY path where the
    # perform's writes do NOT commit: the in-txn idempotency-race short-circuit (perform
    # skipped), the exception/rollback path, and the non-completed-result rollback path. It
    # is NOT called on the committed-success path. Owned-write leaves it unset (no out-of-txn
    # side effects to undo). The perform records what it has moved on the (mutable) resolved
    # value so the cleanup undoes exactly the staged side effects.
    plain_cleanup_resolved_fn: Callable[[Any], None] | None = None
    # optional post-commit success-finalize seam for plain ops whose in-txn perform stages an
    # out-of-txn side effect that needs a final cleanup AFTER the receipt commits (the local
    # exports' .bak removal: the in-txn deliver moves dest -> .bak + tmp -> dest, and only a
    # committed receipt should drop the .bak). The shared in-txn helper calls it with the
    # resolved value ONLY on the committed-success path, after the commit succeeds. It is never
    # called on any non-commit path (those run `plain_cleanup_resolved_fn` instead). Ops with no
    # post-commit finalize (plain receipt-only ops, owned-write) leave it unset.
    plain_post_commit_fn: Callable[[Any], None] | None = None
    # model child-sheet body seams
    child_sheet_model_resolve_fn: (
        Callable[[Any, Any, Any], dict[str, Any] | ActionError] | None
    ) = None
    child_sheet_estimate_cost_fn: Callable[[Any, dict[str, Any]], dict] | None = None
    child_sheet_compute_fn: Callable[..., Any] | None = None
    child_sheet_write_result_fn: Callable[..., ActionResult] | None = None


@dataclass(frozen=True)
class _QueuedPayloadCodec:
    key: str
    decode_fn: Callable[[Any], Any] | None = None
    encode_fn: Callable[[Any], Any] | None = None

    def encode(self, value: Any) -> Any:
        if self.encode_fn is None:
            return value
        return self.encode_fn(value)

    def decode(self, value: Any) -> Any:
        if self.decode_fn is None:
            return value
        return self.decode_fn(value)


def _decode_queued_payload_input_column_ids(value: Any) -> dict[str, int]:
    return {str(name): int(column_id) for name, column_id in dict(value or {}).items()}


def _decode_queued_payload_dict(value: Any) -> dict[str, Any]:
    return dict(value or {})


def _decode_queued_payload_int_list(value: Any) -> list[int]:
    items = value if isinstance(value, (list, tuple)) else []
    decoded: list[int] = []
    for item in items:
        if isinstance(item, bool):
            continue
        if isinstance(item, Integral):
            item_int = int(item)
            if item_int > 0:
                decoded.append(item_int)
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if stripped and stripped.isdigit():
                item_int = int(stripped)
                if item_int > 0:
                    decoded.append(item_int)
    return decoded


def _decode_queued_payload_dict_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in list(value or []) if isinstance(item, dict)]


def _queued_payload_codecs(*keys: str) -> tuple[_QueuedPayloadCodec, ...]:
    codecs: list[_QueuedPayloadCodec] = []
    for key in keys:
        decode_fn: Callable[[Any], Any] | None = None
        if key == "input_column_ids":
            decode_fn = _decode_queued_payload_input_column_ids
        elif key in {"input_column", "judged_output", "evaluation_context"}:
            decode_fn = _decode_queued_payload_dict
        elif key == "row_ids":
            decode_fn = _decode_queued_payload_int_list
        elif key in {
            "input_columns",
            "blob_refs",
            "input_refs",
            "source_url_refs",
            "invalid_source_url_refs",
        }:
            decode_fn = _decode_queued_payload_dict_list
        codecs.append(_QueuedPayloadCodec(key=key, decode_fn=decode_fn))
    return tuple(codecs)


@dataclass(frozen=True)
class _QueuedActionSpec:
    kind: str
    params_model: type[BaseModel]
    completed_spec: _ReservedMaprunnerActionSpec | None
    runner_spec_fn: Callable[[Any], dict[str, Any]]
    resolve_fn: Callable[[Any, Any, dict[str, Any]], dict[str, Any] | ActionError]
    precheck_fn: Callable[[Any, Any, dict[str, Any]], ActionError | None]
    reservation_kind: str
    queue_job_kind: str
    payload_codecs: tuple[_QueuedPayloadCodec, ...]
    finalize_action: Callable[..., ActionResult] | None = None
    pre_run_guard: (
        Callable[[Any, Mapping[str, Any], BaseModel], ActionError | None] | None
    ) = None
    params_hash_fn: Callable[[ActionSpec], str] | None = None
    confirmed_fn: Callable[[Any], bool] | None = None
    needs_confirmation_error_fn: Callable[[ActionSpec, Any], ActionError] | None = None
    cost_gate_error_fn: Callable[[ActionSpec, CostGate], ActionError] | None = None
    map_error_code: str = "map_run_failed"
    output_claim_error_field: str = "params.output_name"
    completed_result_from_existing_fn: Callable[..., ActionResult] | None = None
    # The child_sheet_model archetype's
    # resolve seam is ALWAYS model-backed (it resolves the embedding backend),
    # unlike reserved-maprunner resolve_fns, which never need a router at
    # reserve time (MapRunner re-resolves its own router-dependent bits
    # per-row, inside the worker). When True, `resolve_fn` is called with an
    # extra positional `router` argument: `resolve_fn(project, params,
    # runner_spec, router)`. False (the default) keeps every existing
    # reserved-maprunner-derived queued spec's 3-arg call byte-identical.
    resolve_needs_router: bool = False


@dataclass(frozen=True)
class _QueuedActionInventoryEntry:
    spec: _QueuedActionSpec
    finalize_action: Callable[..., ActionResult] | None = None


def _plain_action_runner(
    run: Callable[..., ActionResult],
) -> Callable[..., ActionResult]:
    def adapter(
        project: Any,
        action: ActionSpec,
        params: BaseModel,
        *,
        ctx: ExecutorContext,
    ) -> ActionResult:
        return run(
            project,
            action,
            params,
            project_id=ctx.project_id,
        )

    return adapter


def _router_action_runner(
    run: Callable[..., ActionResult],
) -> Callable[..., ActionResult]:
    def adapter(
        project: Any,
        action: ActionSpec,
        params: BaseModel,
        *,
        ctx: ExecutorContext,
    ) -> ActionResult:
        return run(
            project,
            action,
            params,
            project_id=ctx.project_id,
            router=ctx.deps.router,
        )

    return adapter


def _maprunner_action_runner(
    run: Callable[..., ActionResult],
) -> Callable[..., ActionResult]:
    def adapter(
        project: Any,
        action: ActionSpec,
        params: BaseModel,
        *,
        ctx: ExecutorContext,
    ) -> ActionResult:
        map_runner_factory = ctx.deps.map_runner_factory
        if map_runner_factory is None:
            raise RuntimeError("ExecutorContext.deps.map_runner_factory is required")
        return run(
            project,
            action,
            params,
            project_id=ctx.project_id,
            router=ctx.deps.router,
            map_runner_factory=map_runner_factory,
        )

    return adapter


def _rss_fetch_action_runner(
    run: Callable[..., ActionResult],
) -> Callable[..., ActionResult]:
    def adapter(
        project: Any,
        action: ActionSpec,
        params: BaseModel,
        *,
        ctx: ExecutorContext,
    ) -> ActionResult:
        return run(
            project,
            action,
            params,
            project_id=ctx.project_id,
            rss_fetcher=ctx.deps.rss_fetcher,
        )

    return adapter


def _group_label_from_output_intent(action: _ActionExecutionEnvelope) -> str | None:
    for intent in getattr(action, "output_intent", ()):
        if not isinstance(intent, dict):
            continue
        if intent.get("kind") != "column_group":
            continue
        label = intent.get("group_label")
        if isinstance(label, str) and label.strip():
            return label.strip()
    return None


def _overwrite_existing_from_output_intent(action: _ActionExecutionEnvelope) -> bool:
    """True when the caller declared an ``overwrite_existing`` output intent:
    reuse a colliding AI output column instead of
    rejecting. Only AI-generated columns are reused (build_precheck_fn); source
    columns stay protected."""
    for intent in getattr(action, "output_intent", ()):
        if isinstance(intent, dict) and intent.get("kind") == "overwrite_existing":
            return True
    return False


def _apply_action_envelope_to_runner_spec(
    action: _ActionExecutionEnvelope,
    runner_spec: dict[str, Any],
) -> dict[str, Any]:
    if "recipe" in runner_spec:
        raise ValueError(
            "runner spec builder emitted retired top-level recipe identity"
        )
    runner_action_kind = runner_spec.get("action_kind")
    if runner_action_kind != action.kind:
        raise ValueError(
            "runner spec action_kind must exactly match the ActionSpec kind: "
            f"{runner_action_kind!r} != {action.kind!r}"
        )
    if action.row_scope is not None:
        runner_spec["sheet_id"] = action.row_scope.sheet_id
        selector = action.row_scope.selector
        if selector.kind == "exact_membership":
            runner_spec["row_ids"] = list(selector.membership.row_ids)
        else:
            runner_spec.pop("row_ids", None)
    group_label = _group_label_from_output_intent(action)
    if group_label is not None:
        runner_spec["group_label"] = group_label
    if _overwrite_existing_from_output_intent(action):
        runner_spec["overwrite"] = True
    return runner_spec
