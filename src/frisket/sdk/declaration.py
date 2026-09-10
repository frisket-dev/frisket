"""Legacy plugin `@op` declarations consumed by the generated plugin lifecycle.

Plugin operations declare their own schemas, execution metadata, and errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frisket.contracts.action import (
    ActionErrorSpec,
    CostPolicy,
    IdempotencyPolicy,
    RetryPolicy,
    SheetRowScopePolicy,
)


@dataclass(frozen=True)
class Op:
    # --- identity + prose (declared; irreducible) ---
    kind: str
    title: str
    description: str
    # --- I/O contract (schemas DERIVED from these models) ---
    params_model: type
    output_model: type
    # --- catalog policy/metadata ---
    errors: tuple[ActionErrorSpec, ...] | None
    side_effects: tuple[str, ...]
    cost: CostPolicy
    idempotency: IdempotencyPolicy
    retry: RetryPolicy
    examples: tuple[dict[str, Any], ...]
    primary_fields: tuple[str, ...]
    row_scope_policy: SheetRowScopePolicy | None = None
    execution_mode: str = "per_row"
    async_mode: str = "sync"
    writes_project: bool = True
    receipt_policy: str = "writes_receipt"
    form: str | None = None  # ui_hints form name; defaults to the kind suffix

    # --- execution wiring (consumed by the reserved-maprunner generator) ---
    # params passed through to the runner_spec as (param_name, include_policy):
    # "always" | "truthy" | "not_none" | "supplied". Drives the generic
    # runner_spec_fn. "supplied" includes the value only when the author
    # actually set the field (pydantic model_fields_set): a materialized
    # default is spec shape, not an authored option — declaration-driven
    # per-target ability checking must never see, and
    # so never refuse over, a knob the author never wrote.
    passthrough: tuple[tuple[str, str], ...] = ()
    # param holding the generated output column name; and the optional row-scope param.
    output_name_attr: str = "output_name"
    row_ids_attr: str = "row_ids"
    # multi-output ops derive their columns from recipe.output_fields and use plural
    # precheck prose; whether the input-column receipt ref carries the column type.
    multi_output: bool = False
    input_ref_includes_type: bool = False
    # model-backed ops: capture pulls the model_calls store (provider_use, counts).
    model_backed: bool = False
    # --- LLM-archetype structural knobs (defaults preserve deterministic behaviour) ---
    # run statuses the write skeleton accepts as a successful run; the error code it
    # raises otherwise (deterministic: only "completed" / "map_run_failed").
    accepted_run_statuses: tuple[str, ...] = ("completed",)
    map_error_code: str = "map_run_failed"
    # callable(params) -> dict merged into the runner_spec (op-specific transforms
    # the passthrough can't express, e.g. classify's `fields`).
    runner_spec_extra: Any = None
    # PROJECT-AWARE runner_spec augmentation: callable(project, params, resolved,
    # runner_spec) -> dict that the generic resolve merges into the runner_spec, for spec
    # data needing the store at resolve time (e.g. map.judge tracing the judged column's
    # upstream prompt). Unlike runner_spec_extra (params-only, pure), this runs in the
    # resolve/executor layer with DB access; build_resolve_fn does the merge explicitly,
    # and the executor threads that same runner_spec to the runner in
    # action_lifecycle.py. Not a resolve_override (resolve stays generic).
    runner_spec_resolve_extra: Any = None
    # omit the input_columns key from the runner_spec when it would be empty (role-named
    # / template ops whose recipe spec names columns by role, not a flat list).
    omit_empty_input_columns: bool = False
    # passed to the reserved spec (classify sets False).
    log_missed_delete: bool = True
    # external-API capability added to the catalog (e.g. "geocode" -> external:geocode).
    external_capability: str | None = None
    # external cost confirmation: a needs_confirmation_error_fn(action, params) the
    # reserved spec wires (distinct from the model cost_gate path).
    needs_confirmation_error: Any = None
    # declarative input-column descriptor (frisket.sdk.inputs: Column/Columns/Template/
    # OneOf). When set, the generic resolve interprets it (no resolve_override) and
    # build_catalog projects it into ui_hints for the frontend column pickers.
    inputs: Any = None
    # --- output axis: "columns" (reserved-maprunner) | "child_sheet" (direct seam) ---
    # child_sheet ops produce a new sheet with parent provenance (derive/reduce/join);
    # the generic child-sheet orchestrator (frisket.sdk.childsheet) runs them.
    output_kind: str = "columns"
    # param holding the target child-sheet name (for the duplicate_sheet_name precheck).
    target_sheet_param: str | None = None
    # child-sheet op's source resolve. The reserved-model generator calls it as
    # (project, params, router); the deterministic generator calls it as (project, params).
    # Returns a resolved payload {parent_sheet_id, columns: [MaterializedColumnSpec],
    # rows: [SingleParentMaterializedRow], ...present/op_spec data} or an ActionError, and
    # carries the op-unique transform. The router lets a model/embedding-backed op validate
    # its backend pre-reservation (join.semantic); router-free ops (reduce) ignore it.
    child_sheet_resolve: Any = None
    # callable(resolved) -> dict merged into the persisted op_spec (op-specific provenance).
    op_spec_extra: Any = None
    # OPTIONAL child-sheet write hook: callable
    # (project, resolved, write) -> None, run inside the SAME write transaction
    # immediately after write_single_parent_child_sheet, before the receipt is
    # built. The deterministic child-sheet body's analog of `write_override`
    # above (an op-unique write step irreducible to the generic skeleton) --
    # used only by derive.table_from_list, to repoint/copy each derived row's
    # source item's evidence link onto that row (row-to-row lineage, distinct
    # from the generic blob/cell lineage the body already writes). None
    # (default) runs no extra write; every other child-sheet op is unaffected.
    child_sheet_row_evidence: Any = None
    # --- reserved-model child-sheet axes (reduce/join: model-backed, reservation-backed,
    # cost-gated child sheets). When `child_sheet_model` is True the family wires the
    # reserved-model child-sheet generator (frisket.sdk.childsheet.
    # build_reserved_model_child_sheet_run_fn) instead of the deterministic one. The op
    # owns the write + materialization (reduce uses write_aggregate_sheet, not the single-
    # parent writer), so the generator orchestrates only: idempotency/replay -> resolve ->
    # duplicate precheck -> cost-gate -> reserve running receipt -> async model compute ->
    # write_result (owns receipt finalize) -> reservation cleanup.
    child_sheet_model: bool = False
    # the running-receipt reservation ref kind; defaults to f"{underscored}_idempotency_reservation".
    reservation_kind: str | None = None
    # (params, resolved) -> dict with a "cost" key (None or float).
    child_sheet_estimate_cost: Any = None
    # async (project, params, resolved, router) -> computations | ActionError. The project
    # lets compute materialize for ops whose work lives there (join.semantic runs+writes the
    # join during compute); pure-compute ops (reduce) ignore it.
    child_sheet_compute: Any = None
    # the write_result hook: owns materialization + receipt finalize -> ActionResult.
    child_sheet_write: Any = None
    # (project, receipt) -> ActionError | None; optional replay validation (None = ok).
    child_sheet_replay_validate: Any = None
    # TRANSITIONAL escape hatches for the genuinely-divergent external archetype
    # (geocode): the op's own resolve/replay. Not the permanent design.
    resolve_override: Any = None
    replay_override: Any = None
    # transitional like resolve/replay above: an op whose precheck diverges from the
    # generic "refuse to overwrite an existing column" (media ops allow reuse of an
    # ai_generated output column still pointed at a prior run of the same recipe).
    precheck_override: Any = None
    # OP-UNIQUE write (NOT a transitional escape hatch like the two above): a factory
    # (decl, holder) -> write_fn for an op whose write is genuinely irreducible to the
    # generic skeleton. Used only by map.extract, whose citation grounding is a
    # transactional, side-effecting pass (record source artifacts/spans/evidence-links;
    # withhold uncited cells; named-result outputs) that cannot live in pure present().
    # This is the legitimate op-unique 5% (like receipt=), not a missing axis.
    write_override: Any = None
    # RESUME seam (run.backfill): callable(project, params, resolved) -> int | None returning
    # the existing run id to RESUME rather than create. None (default) keeps the create path:
    # the reserved-maprunner body allocates a fresh run + output columns. When set, the body
    # threads it into MapRunner.run(resume_run_id=...) (skips the create-path estimate/cost-gate,
    # extends the existing run's row scope). build_reserved_spec passes it straight through.
    resume_run_id_fn: Any = None
    # OP-UNIQUE cost-gate error (run.backfill): callable(action, CostGate) -> ActionError
    # installed as the reserved-maprunner body's cost_gate_error_fn. requires_confirmation
    # hard-wires the shared _model_cost_requires_confirmation_error (status="failed" on the
    # direct path); backfill instead extends a resume and must surface the 402
    # needs_confirmation envelope, so it marks the error needs_confirmation=True. Pairs with
    # threads_confirmed (confirmed_fn + params_hash_without_confirmed) WITHOUT the model gate.
    cost_gate_error_override: Any = None
    # capture rich input-column metadata (ai_generated / source_run_id /
    # source_receipt_id) for the input refs.
    rich_input_columns: bool = False
    # model-metered ops gate on cost confirmation: the reserved spec wires
    # params_hash_without_confirmed + confirmed_fn + the cost-gate error.
    requires_confirmation: bool = False
    # local confirm-gated ops (e.g. media to_markdown/video_frames/extract_faces) thread
    # `confirmed` to the runner + strip it from the idempotency hash, but have NO model cost
    # gate (a CostGate stays the op's own map_error_code). Decoupled from requires_confirmation.
    threads_confirmed: bool = False
    # whether the finalize result_from_existing_fn is invoked with {"params": params}
    # on replay (deterministic ops) vs no kwargs (e.g. summarize).
    finalize_passes_params: bool = True
    # the op's receipt callable: present(project, facts, base_output_refs) -> Provenance
    # (B′). Loosely typed to avoid an import cycle.
    receipt: Any = None
    # extra required capabilities beyond the policy-derived ones (project:write /
    # model:complete / external:*). map.python adds "unsafe:local_code".
    extra_capabilities: tuple[str, ...] = ()
    # Capabilities required only for one authored parameter choice.
    conditional_capabilities: tuple[dict[str, Any], ...] = ()
    # extra ui_hints keys merged onto the derived {form, primary_fields}. No op
    # declares any today -- map.python's requires_unsafe_confirmation was
    # deleted for having zero readers in web/.
    extra_ui_hints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def slug(self) -> str:
        return self.kind.split(".")[-1]

    @property
    def underscored(self) -> str:
        # "map.clean_column" -> "map_clean_column"; basis for reservation_kind and
        # the receipt ref-kind discriminators.
        return self.kind.replace(".", "_")


def op(
    *,
    kind: str,
    title: str,
    description: str,
    params_model: type,
    output_model: type,
    errors: tuple[ActionErrorSpec, ...] | list[ActionErrorSpec] | None,
    side_effects: tuple[str, ...] | list[str],
    cost: CostPolicy,
    idempotency: IdempotencyPolicy,
    retry: RetryPolicy,
    examples: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    primary_fields: tuple[str, ...] | list[str],
    row_scope_policy: SheetRowScopePolicy | None = None,
    execution_mode: str = "per_row",
    async_mode: str = "sync",
    writes_project: bool = True,
    receipt_policy: str = "writes_receipt",
    form: str | None = None,
    passthrough: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    output_name_attr: str = "output_name",
    row_ids_attr: str = "row_ids",
    multi_output: bool = False,
    input_ref_includes_type: bool = False,
    model_backed: bool = False,
    accepted_run_statuses: tuple[str, ...] = ("completed",),
    map_error_code: str = "map_run_failed",
    runner_spec_extra: Any = None,
    runner_spec_resolve_extra: Any = None,
    omit_empty_input_columns: bool = False,
    log_missed_delete: bool = True,
    external_capability: str | None = None,
    needs_confirmation_error: Any = None,
    inputs: Any = None,
    output_kind: str = "columns",
    target_sheet_param: str | None = None,
    child_sheet_resolve: Any = None,
    op_spec_extra: Any = None,
    child_sheet_row_evidence: Any = None,
    child_sheet_model: bool = False,
    reservation_kind: str | None = None,
    child_sheet_estimate_cost: Any = None,
    child_sheet_compute: Any = None,
    child_sheet_write: Any = None,
    child_sheet_replay_validate: Any = None,
    resolve_override: Any = None,
    replay_override: Any = None,
    precheck_override: Any = None,
    write_override: Any = None,
    resume_run_id_fn: Any = None,
    cost_gate_error_override: Any = None,
    rich_input_columns: bool = False,
    requires_confirmation: bool = False,
    threads_confirmed: bool = False,
    finalize_passes_params: bool = True,
    receipt: Any = None,
    extra_capabilities: tuple[str, ...] | list[str] = (),
    conditional_capabilities: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    extra_ui_hints: dict[str, Any] | None = None,
) -> Op:
    """Build the `Op` declaration. Call it directly: `X_OP = op(...)`.

    Execution is registered independently by canonical `kind`; this declaration
    supplies the contract consumed by the SDK generators.
    """
    normalized_extra_ui_hints = dict(extra_ui_hints or {})
    if inputs is not None:
        if "source_requirements" in normalized_extra_ui_hints:
            raise ValueError(
                f"{kind} inputs is the source authority; remove competing source "
                "declaration: ui_hints.source_requirements"
            )

        owned_params = inputs.owned_params()
        unknown_params = sorted(set(owned_params) - set(params_model.model_fields))
        if unknown_params:
            raise ValueError(
                f"{kind} inputs names params absent from {params_model.__name__}: "
                f"{', '.join(unknown_params)}"
            )

    return Op(
        kind=kind,
        title=title,
        description=description,
        params_model=params_model,
        output_model=output_model,
        errors=None if errors is None else tuple(errors),
        side_effects=tuple(side_effects),
        cost=cost,
        idempotency=idempotency,
        retry=retry,
        examples=tuple(examples),
        primary_fields=tuple(primary_fields),
        row_scope_policy=row_scope_policy,
        execution_mode=execution_mode,
        async_mode=async_mode,
        writes_project=writes_project,
        receipt_policy=receipt_policy,
        form=form,
        passthrough=tuple(passthrough),
        output_name_attr=output_name_attr,
        row_ids_attr=row_ids_attr,
        multi_output=multi_output,
        input_ref_includes_type=input_ref_includes_type,
        model_backed=model_backed,
        accepted_run_statuses=accepted_run_statuses,
        map_error_code=map_error_code,
        runner_spec_extra=runner_spec_extra,
        runner_spec_resolve_extra=runner_spec_resolve_extra,
        omit_empty_input_columns=omit_empty_input_columns,
        log_missed_delete=log_missed_delete,
        external_capability=external_capability,
        needs_confirmation_error=needs_confirmation_error,
        inputs=inputs,
        output_kind=output_kind,
        target_sheet_param=target_sheet_param,
        child_sheet_resolve=child_sheet_resolve,
        op_spec_extra=op_spec_extra,
        child_sheet_row_evidence=child_sheet_row_evidence,
        child_sheet_model=child_sheet_model,
        reservation_kind=reservation_kind,
        child_sheet_estimate_cost=child_sheet_estimate_cost,
        child_sheet_compute=child_sheet_compute,
        child_sheet_write=child_sheet_write,
        child_sheet_replay_validate=child_sheet_replay_validate,
        resolve_override=resolve_override,
        replay_override=replay_override,
        precheck_override=precheck_override,
        write_override=write_override,
        resume_run_id_fn=resume_run_id_fn,
        cost_gate_error_override=cost_gate_error_override,
        rich_input_columns=rich_input_columns,
        requires_confirmation=requires_confirmation,
        threads_confirmed=threads_confirmed,
        finalize_passes_params=finalize_passes_params,
        receipt=receipt,
        extra_capabilities=tuple(extra_capabilities),
        conditional_capabilities=tuple(dict(item) for item in conditional_capabilities),
        extra_ui_hints=normalized_extra_ui_hints,
    )
