"""Six typed constructors over `Op` (the authoring surface, not the IR).

Each constructor exposes ONLY the parameters its execution archetype uses and
hard-wires that archetype's defaults; `Op` (`frisket.sdk.declaration`) stays the
single normalized form the generators consume. New first-party declarations
call one of these, never `op()` directly, so their execution defaults cannot
drift.
"""

from __future__ import annotations

from typing import Any

from frisket.contracts.action import (
    ActionErrorSpec,
    CostPolicy,
    IdempotencyPolicy,
    RetryPolicy,
    SheetRowScopePolicy,
)
from frisket.sdk.declaration import Op, op


def deterministic_map(
    *,
    # --- shared core (verbatim across all five constructors) ---
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
    form: str | None = None,
    receipt: Any = None,
    # --- source descriptor (orthogonal to archetype) ---
    inputs: Any = None,
    # --- runner wiring ---
    passthrough: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    omit_empty_input_columns: bool = False,
    runner_spec_extra: Any = None,
    async_mode: str = "sync",
    # --- multi-output / input-ref knobs (clean_column, find_*) ---
    multi_output: bool = False,
    input_ref_includes_type: bool = False,
    # --- statuses/error-code knobs (python, ner) ---
    model_backed: bool = False,
    accepted_run_statuses: tuple[str, ...] = ("completed",),
    map_error_code: str = "map_run_failed",
    # --- confirmation/write seams (python's unsafe-code gate; ner's span write) ---
    requires_confirmation: bool = False,
    write_override: Any = None,
    replay_override: Any = None,
    extra_capabilities: tuple[str, ...] | list[str] = (),
    extra_ui_hints: dict[str, Any] | None = None,
    conditional_capabilities: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
) -> Op:
    """Deterministic, locally-executed map op. Defaults reproduce `op()`'s own
    defaults; nothing here is model-backed or confirmation-gated unless the
    caller explicitly asks (python's unsafe-code gate)."""
    return op(
        kind=kind,
        title=title,
        description=description,
        params_model=params_model,
        output_model=output_model,
        errors=errors,
        side_effects=side_effects,
        cost=cost,
        idempotency=idempotency,
        retry=retry,
        examples=examples,
        primary_fields=primary_fields,
        row_scope_policy=row_scope_policy,
        form=form,
        receipt=receipt,
        inputs=inputs,
        passthrough=passthrough,
        omit_empty_input_columns=omit_empty_input_columns,
        runner_spec_extra=runner_spec_extra,
        async_mode=async_mode,
        multi_output=multi_output,
        input_ref_includes_type=input_ref_includes_type,
        model_backed=model_backed,
        accepted_run_statuses=accepted_run_statuses,
        map_error_code=map_error_code,
        requires_confirmation=requires_confirmation,
        write_override=write_override,
        replay_override=replay_override,
        extra_capabilities=extra_capabilities,
        extra_ui_hints=extra_ui_hints,
        conditional_capabilities=conditional_capabilities,
    )


def model_map(
    *,
    # --- shared core ---
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
    form: str | None = None,
    receipt: Any = None,
    # --- source descriptor ---
    inputs: Any = None,
    # --- runner wiring ---
    passthrough: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    runner_spec_extra: Any = None,
    runner_spec_resolve_extra: Any = None,
    # --- multi-output / rich-input knobs (classify/extract/judge/translate; summarize) ---
    multi_output: bool = False,
    rich_input_columns: bool = False,
    # --- LLM-archetype structural knobs, hard-wired defaults ---
    model_backed: bool = True,
    accepted_run_statuses: tuple[str, ...] = ("completed", "partial", "failed"),
    map_error_code: str = "model_run_failed",
    requires_confirmation: bool = True,
    finalize_passes_params: bool = True,
    log_missed_delete: bool = True,
    # --- write seam (extract's grounding pass) ---
    write_override: Any = None,
    # --- external-preflight + override seams (research.answer: an agent op is
    # --- model-cost-gated AND external-preflight-confirmed, with op-unique
    # --- resolve/precheck/replay riding hand-written functions) ---
    needs_confirmation_error: Any = None,
    resolve_override: Any = None,
    precheck_override: Any = None,
    replay_override: Any = None,
    conditional_capabilities: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
    extra_capabilities: tuple[str, ...] | list[str] = (),
) -> Op:
    """Model-backed map op (classify/extract/judge/summarize/translate). Cost-
    gated and confirmation-required by default; `accepted_run_statuses`
    tolerating "partial"/"failed" makes replay receipt-scoped (checks the
    receipt's own recorded rows, value included) rather than expected-scoped
    (sdk/replay.py)."""
    return op(
        kind=kind,
        title=title,
        description=description,
        params_model=params_model,
        output_model=output_model,
        errors=errors,
        side_effects=side_effects,
        cost=cost,
        idempotency=idempotency,
        retry=retry,
        examples=examples,
        primary_fields=primary_fields,
        form=form,
        receipt=receipt,
        inputs=inputs,
        passthrough=passthrough,
        runner_spec_extra=runner_spec_extra,
        runner_spec_resolve_extra=runner_spec_resolve_extra,
        multi_output=multi_output,
        rich_input_columns=rich_input_columns,
        model_backed=model_backed,
        accepted_run_statuses=accepted_run_statuses,
        map_error_code=map_error_code,
        requires_confirmation=requires_confirmation,
        finalize_passes_params=finalize_passes_params,
        log_missed_delete=log_missed_delete,
        write_override=write_override,
        needs_confirmation_error=needs_confirmation_error,
        resolve_override=resolve_override,
        precheck_override=precheck_override,
        replay_override=replay_override,
        conditional_capabilities=conditional_capabilities,
        extra_capabilities=extra_capabilities,
    )


def media_map(
    *,
    # --- shared core (verbatim across all six constructors) ---
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
    form: str | None = None,
    receipt: Any = None,
    # --- source descriptor ---
    inputs: Any = None,
    # --- runner wiring ---
    passthrough: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    runner_spec_extra: Any = None,
    runner_spec_resolve_extra: Any = None,
    # --- multi-output / input-ref knobs (extract_metadata only) ---
    multi_output: bool = False,
    input_ref_includes_type: bool = False,
    output_name_attr: str = "output_name",
    accepted_run_statuses: tuple[str, ...] = ("completed",),
    # --- error-code/status knob: every media op passes its own, no shared default ---
    map_error_code: str,
    log_missed_delete: bool = False,
    # --- confirmation gates (extract_faces/video_frames vs. ocr/to_markdown/transcribe) ---
    threads_confirmed: bool = False,
    requires_confirmation: bool = False,
    # --- override seams: every media op is override-heavy (resolve/precheck/
    # --- replay/write ride hand-written functions; the generic skeleton
    # --- supplies only the runner_spec from the declaration) ---
    precheck_override: Any = None,
    resolve_override: Any = None,
    replay_override: Any = None,
    write_override: Any = None,
) -> Op:
    """Media-family map op (capture_url/extract_faces/extract_metadata/
    extract_pdf_tables/fetch_url/ocr/to_markdown/transcribe/video_frames/
    ytdlp_download). Deterministic and locally-executed like `deterministic_map`,
    but override-heavy: each op's resolve/precheck/replay/write are irreducible
    hand-written functions, not generic skeleton behavior. `map_error_code` has
    no default because every op passes its own; `log_missed_delete` defaults to
    False because every media op passes it explicitly (unlike `op()`'s own
    default of True)."""
    return op(
        kind=kind,
        title=title,
        description=description,
        params_model=params_model,
        output_model=output_model,
        errors=errors,
        side_effects=side_effects,
        cost=cost,
        idempotency=idempotency,
        retry=retry,
        examples=examples,
        primary_fields=primary_fields,
        form=form,
        receipt=receipt,
        inputs=inputs,
        passthrough=passthrough,
        runner_spec_extra=runner_spec_extra,
        runner_spec_resolve_extra=runner_spec_resolve_extra,
        multi_output=multi_output,
        input_ref_includes_type=input_ref_includes_type,
        output_name_attr=output_name_attr,
        accepted_run_statuses=accepted_run_statuses,
        map_error_code=map_error_code,
        log_missed_delete=log_missed_delete,
        threads_confirmed=threads_confirmed,
        requires_confirmation=requires_confirmation,
        precheck_override=precheck_override,
        resolve_override=resolve_override,
        replay_override=replay_override,
        write_override=write_override,
    )


def external_map(
    *,
    # --- shared core ---
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
    form: str | None = None,
    receipt: Any = None,
    # --- source descriptor ---
    inputs: Any = None,
    # --- runner wiring ---
    passthrough: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    runner_spec_extra: Any = None,
    multi_output: bool = False,
    # --- error-code knob: web_search keeps its own run-failure code ---
    map_error_code: str = "map_run_failed",
    # --- transitional escape hatches for the genuinely-divergent external archetype ---
    external_capability: str | None = None,
    needs_confirmation_error: Any = None,
    resolve_override: Any = None,
    precheck_override: Any = None,
    replay_override: Any = None,
    write_override: Any = None,
) -> Op:
    """External-provider-metered map op (geocode's shape). Cost is preflight-
    confirmed via `needs_confirmation_error`, not the model cost gate."""
    return op(
        kind=kind,
        title=title,
        description=description,
        params_model=params_model,
        output_model=output_model,
        errors=errors,
        side_effects=side_effects,
        cost=cost,
        idempotency=idempotency,
        retry=retry,
        examples=examples,
        primary_fields=primary_fields,
        form=form,
        receipt=receipt,
        inputs=inputs,
        passthrough=passthrough,
        runner_spec_extra=runner_spec_extra,
        multi_output=multi_output,
        map_error_code=map_error_code,
        external_capability=external_capability,
        needs_confirmation_error=needs_confirmation_error,
        resolve_override=resolve_override,
        precheck_override=precheck_override,
        replay_override=replay_override,
        write_override=write_override,
    )


def child_sheet(
    *,
    # --- shared core ---
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
    form: str | None = None,
    receipt: Any = None,
    # --- child-sheet axes ---
    execution_mode: str = "whole_project",
    target_sheet_param: str | None = None,
    child_sheet_resolve: Any = None,
    op_spec_extra: Any = None,
    child_sheet_row_evidence: Any = None,
    child_sheet_replay_validate: Any = None,
) -> Op:
    """Deterministic child-sheet op (derive.table_from_list's shape):
    `output_kind="child_sheet"` is hard-wired, not exposed."""
    return op(
        kind=kind,
        title=title,
        description=description,
        params_model=params_model,
        output_model=output_model,
        errors=errors,
        side_effects=side_effects,
        cost=cost,
        idempotency=idempotency,
        retry=retry,
        examples=examples,
        primary_fields=primary_fields,
        form=form,
        receipt=receipt,
        execution_mode=execution_mode,
        output_kind="child_sheet",
        target_sheet_param=target_sheet_param,
        child_sheet_resolve=child_sheet_resolve,
        op_spec_extra=op_spec_extra,
        child_sheet_row_evidence=child_sheet_row_evidence,
        child_sheet_replay_validate=child_sheet_replay_validate,
    )


def model_child_sheet(
    *,
    # --- shared core ---
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
    form: str | None = None,
    receipt: Any = None,
    inputs: Any = None,
    # --- child-sheet axes ---
    execution_mode: str,
    target_sheet_param: str | None = None,
    # --- reserved-model child-sheet axes (join.semantic, reduce.group_summary) ---
    reservation_kind: str | None = None,
    child_sheet_resolve: Any = None,
    child_sheet_estimate_cost: Any = None,
    child_sheet_compute: Any = None,
    child_sheet_write: Any = None,
    child_sheet_replay_validate: Any = None,
) -> Op:
    """Model-backed, reservation-backed, cost-gated child-sheet op. Additionally
    hard-wires `child_sheet_model=True`, `model_backed=True`,
    `requires_confirmation=True` beyond the deterministic `child_sheet`
    archetype; `execution_mode` has no shared default (join.semantic and
    reduce.group_summary genuinely differ: "cross_sheet" vs "grouped")."""
    return op(
        kind=kind,
        title=title,
        description=description,
        params_model=params_model,
        output_model=output_model,
        errors=errors,
        side_effects=side_effects,
        cost=cost,
        idempotency=idempotency,
        retry=retry,
        examples=examples,
        primary_fields=primary_fields,
        form=form,
        receipt=receipt,
        inputs=inputs,
        execution_mode=execution_mode,
        model_backed=True,
        requires_confirmation=True,
        output_kind="child_sheet",
        child_sheet_model=True,
        target_sheet_param=target_sheet_param,
        reservation_kind=reservation_kind,
        child_sheet_resolve=child_sheet_resolve,
        child_sheet_estimate_cost=child_sheet_estimate_cost,
        child_sheet_compute=child_sheet_compute,
        child_sheet_write=child_sheet_write,
        child_sheet_replay_validate=child_sheet_replay_validate,
    )
