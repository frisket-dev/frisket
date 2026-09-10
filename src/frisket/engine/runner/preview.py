"""In-memory preview for the map runner (extracted from ``MapRunner.preview``
/``MapRunner._preview_validated``): compute a small row sample and return it
without materializing outputs. Accounted previews retain actual-call facts
under an admitted receipt attempt. Reuses the same
persistence-free per-row (``row_execution.execute_row(recorder=None)``) /
batch (``execute_batch`` + ``normalize_batch_row``) compute path and the same
``validate_spec`` guards as ``run()``, plus the ``PREVIEW_MAX_ROWS`` hard cap.
Operates on explicit ``project``/``router``/``run_store`` rather than an
implicit ``self`` so this leaf need not import the engine."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from frisket.ai.llm import ModelRouter
from frisket.ops.base import OpContext, Recipe
from frisket.redaction import safe_error
from frisket.engine.runner import validation
from frisket.engine.runner.batch_normalization import normalize_batch_row
from frisket.engine.runner.confirmation_context import ConsentQuote
from frisket.engine.runner.network_policy import (
    row_effect_cannot_egress,
    row_effect_spends_or_meters,
)
from frisket.engine.runner.publication import required_publication_fields
from frisket.engine.runner.row_execution import (
    AdaptiveThrottle,
    _await_owned_tasks,
    execute_row,
)
from frisket.engine.runner.row_inputs import row_values
from frisket.engine.runner.validation import _ValidatedSpec
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.execution.pricing_policy import PricingPolicy
from frisket.execution.provider import ExecutionComposition
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.attempt import (
    ATTEMPT_EXTRA,
    AttemptCommitment,
    StaleAttemptWriter,
    require_receipt_attempt_writer,
)
from frisket.execution.attempt_authority import attempt_identity

# Preview values are a small sample computed for a look and thrown away.
# The client caps the sample to this many rows before requesting;
# the server enforces it as a HARD cap so a missing/empty/oversized row_ids
# can never fall back to "all rows" — derive/reduce Preview has previously
# omitted previewRows and run the whole sheet this way.
PREVIEW_MAX_ROWS = 20


class PreviewRowCapError(ValueError):
    """Preview requires an explicit, non-empty row_ids sample of at most
    ``PREVIEW_MAX_ROWS`` rows. Missing/empty/oversized is a hard error, never a
    silent fall back to every visible row."""


class PreviewEffectRequiresRun(ValueError):
    """An external or metered effect needs durable run authority."""

    def __init__(self) -> None:
        super().__init__(
            "This action can cause an external or metered effect, so it cannot "
            "run as a preview. Run the action instead."
        )


def _has_exact_free_local_terms(recipe: Recipe, spec: dict, estimate: Any) -> bool:
    """Whether typed static and rated facts prove the effect is locally free."""
    if not row_effect_cannot_egress(recipe, spec) or not isinstance(estimate, Mapping):
        return False
    try:
        quote = ConsentQuote.from_estimate(estimate)
    except (OverflowError, TypeError, ValueError):
        return False
    return (
        quote.cost_source == "free_local" and quote.cost == 0 and quote.billed_cost == 0
    )


def _caught_gate_is_safe(
    exc: validation.CostGate,
    recipe: Recipe,
    spec: dict,
) -> bool:
    """Whether this is exactly the retained free operator-LAN claims gate."""
    if not isinstance(exc, validation.ClaimsGate):
        return False
    claims = getattr(exc, "claims", None)
    if not isinstance(claims, list) or len(claims) != 1:
        return False
    [claim] = claims
    if not isinstance(claim, dict) or claim.get("field") != "egress_class":
        return False
    return _has_exact_free_local_terms(recipe, spec, exc.estimate_details)


@dataclass
class PreviewColumn:
    """A virtual output column the preview would produce. ``overwrites_column_id``
    is set when the output name collides with an existing column (the overlay
    replaces that column's values for the sampled rows); None means a brand-new
    virtual column the frontend appends to the grid."""

    name: str
    column_type: str
    format: str | None = None
    hidden: bool = False
    overwrites_column_id: int | None = None


@dataclass
class PreviewResult:
    """The in-memory sample returned to the frontend overlay. Nothing here is
    persisted: ``values`` are per-row/per-field cell payloads (same shape the
    grid uses), including per-cell error payloads for failed rows."""

    sheet_id: int
    columns: list[PreviewColumn]
    values: dict[int, dict[str, dict[str, Any]]]
    row_ids: list[int]
    sampled: int
    total: int


def _preview_validated(
    project: Project,
    router: ModelRouter,
    run_store: RunResultStore,
    spec: dict,
    *,
    program: Recipe | None = None,
    pricing_policy: PricingPolicy,
    composition: ExecutionComposition,
    consent_coverage: ConsentCoverage | None = None,
    allow_empty_scope: bool = False,
    accounted: bool = False,
) -> _ValidatedSpec:
    raw_row_ids = spec.get("row_ids")
    if raw_row_ids is None or (not raw_row_ids and not allow_empty_scope):
        raise PreviewRowCapError(
            "preview requires an explicit, non-empty row_ids sample"
        )
    if len(raw_row_ids) > PREVIEW_MAX_ROWS:
        raise PreviewRowCapError(
            f"preview sample size {len(raw_row_ids)} exceeds "
            f"PREVIEW_MAX_ROWS={PREVIEW_MAX_ROWS}"
        )
    recipe = program if program is not None else validation.recipe_for_spec(spec)
    effectful = row_effect_spends_or_meters(recipe, spec, router)
    try:
        validated = validation.validate_spec(
            project,
            router,
            run_store,
            spec,
            program=recipe,
            confirmed=bool(spec.get("confirmed")),
            resume_run_id=None,
            pricing_policy=pricing_policy,
            composition=composition,
            consent_coverage=consent_coverage,
            # An accounted preview must persist this ordinary exact quote and
            # route before admission. Unaccounted previews stay write-free.
            persistence="durable" if accounted else "ephemeral",
        )
    except validation.CostGate as exc:
        if accounted or not effectful or _caught_gate_is_safe(exc, recipe, spec):
            raise
        raise PreviewEffectRequiresRun() from None
    except validation.ProviderKeyRefusal:
        if effectful and not accounted:
            raise PreviewEffectRequiresRun() from None
        raise

    if accounted or not effectful:
        return validated
    if not _has_exact_free_local_terms(recipe, spec, validated.est):
        raise PreviewEffectRequiresRun()
    resolved = validated.resolved_execution
    if resolved is None:
        if getattr(recipe, "consumes_resolution", None) is not False:
            raise PreviewEffectRequiresRun()
    else:
        resolution = getattr(resolved, "resolution", None)
        facts = getattr(resolution, "facts", None)
        egress_class = getattr(facts, "egress_class", None)
        if not isinstance(egress_class, str) or egress_class not in {
            "none",
            "operator_lan",
        }:
            raise PreviewEffectRequiresRun()
    return validated


async def run_preview(
    project: Project,
    router: ModelRouter,
    run_store: RunResultStore,
    throttle: AdaptiveThrottle,
    op_context_extras: dict[str, Any],
    row_worker_count: Callable[[Recipe, dict], int],
    spec: dict,
    *,
    program: Recipe | None = None,
    pricing_policy: PricingPolicy,
    composition: ExecutionComposition,
    consent_coverage: ConsentCoverage | None = None,
    allow_empty_scope: bool = False,
    progress_cb: Callable[[int, int], None] | None = None,
    cancel_event: Any | None = None,
    attempt: AttemptCommitment | None = None,
) -> PreviewResult:
    """Compute ephemeral values using the ordinary validated row execution.
    Reuses the persistence-free per-row
    (``execute_row(recorder=None)``) / batch (``execute_batch`` +
    ``normalize_batch_row``) compute path, runs the same ``validate_spec``
    guards as ``run()``, and enforces the ``PREVIEW_MAX_ROWS`` hard cap.

    A preview never creates ops, runs, columns or cells. Paid execution
    requires its caller's running receipt and dispatching attempt; actual
    call facts survive failed or cancelled publication. Receipt lifecycle
    remains with the caller, after this function joins all owned work.
    ``progress_cb(done, total)`` fires per completed row; ``cancel_event``
    (a ``threading.Event``) is checked between rows and surfaced to recipes
    via ``OpContext.extras['cancelled']``. ``row_worker_count`` is the
    engine's ``_row_worker_count`` bound method — the recipe-dependent
    concurrency cap logic stays defined once, in the engine, and is invoked
    here only once ``validate_spec`` has resolved the recipe."""
    validated = _preview_validated(
        project,
        router,
        run_store,
        spec,
        program=program,
        pricing_policy=pricing_policy,
        composition=composition,
        consent_coverage=consent_coverage,
        allow_empty_scope=allow_empty_scope,
        accounted=attempt is not None,
    )
    recipe = validated.recipe
    sheet_id = validated.sheet_id
    col_map = validated.col_map
    row_ids = validated.row_ids  # visibility-resolved sample
    if attempt is not None:
        if attempt.run_id is not None or attempt.receipt_id is None:
            raise StaleAttemptWriter(
                "accounted preview requires a receipt-owned attempt"
            )
        persisted = require_receipt_attempt_writer(
            project, attempt.receipt_id, attempt.attempt_id
        )
        if (
            persisted["action_identity_hash"] != attempt_identity(recipe, spec)
            or json.loads(persisted["scope_json"]) != row_ids
            or attempt.identity != persisted["action_identity_hash"]
            or list(attempt.scope) != row_ids
        ):
            raise StaleAttemptWriter(
                "preview spec or sample differs from its admitted attempt"
            )
    total = len(project.visible_row_ids(sheet_id))
    output_fields = recipe.output_fields(spec)
    field_names = [f["name"] for f in output_fields]
    required_field_names = required_publication_fields(recipe, output_fields)
    managed_publication = True

    # Virtual output columns; mark which overwrite an existing column by name
    # (overlay replaces its sampled values) vs. a brand-new virtual column.
    preview_columns: list[PreviewColumn] = []
    for f in output_fields:
        existing = next((c for c in validated.columns if c["name"] == f["name"]), None)
        preview_columns.append(
            PreviewColumn(
                name=f["name"],
                column_type=f["column_type"],
                format=f.get("format"),
                hidden=bool(f.get("hidden", False)),
                overwrites_column_id=existing["id"] if existing else None,
            )
        )

    def is_cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    source_column_types = {
        c["name"]: c["type"]
        for c in validated.columns
        if c["name"] in set(recipe.source_columns(spec))
    }
    ctx = OpContext(
        project=project,
        http=router.client,
        credential_use_context=composition.credential_use_context,
        execution_limits=composition.limits,
        extras={
            **op_context_extras,
            "router": router,
            "cancelled": is_cancelled,
            "source_column_types": source_column_types,
            "preview": True,
            "preview_execution": validated.resolved_execution
            if attempt is None
            else None,
            ATTEMPT_EXTRA: attempt,
            "run_state": {},
        },
    )

    values: dict[int, dict[str, dict[str, Any]]] = {}
    total_sample = len(row_ids)
    done = 0
    accounted_rows: set[int] = set()

    def record_row(row_id: int, cells: Mapping[str, dict[str, Any]]) -> None:
        if attempt is None:
            return
        batch = [
            {**cell, "row_id": row_id, "column_id": None} for cell in cells.values()
        ]
        run_store.write_returned_call_accounting(
            None,
            batch,
            receipt_id=attempt.receipt_id,
            writer_attempt_id=attempt.attempt_id,
        )
        # The UI may discard a cancelled preview, but returned work keeps its
        # actual outcome for billing. Cancellation does not undo provider calls.
        outcomes = batch or [
            {
                "row_id": row_id,
                "outcome": "cancelled" if is_cancelled() else "empty",
            }
        ]
        run_store.write_receipt_row_outcomes(
            attempt.receipt_id,
            outcomes,
            writer_attempt_id=attempt.attempt_id,
        )
        accounted_rows.add(row_id)

    primary: BaseException | None = None
    try:
        if not recipe.is_llm(spec) and hasattr(recipe, "execute_batch"):
            # Batch recipes compute the whole sample at once; cancel is checked
            # before dispatch and while collecting per-row results.
            values_by_row = {
                row_id: row_values(
                    project,
                    recipe,
                    spec,
                    col_map,
                    row_id,
                    for_model=False,
                    column_types=source_column_types,
                )
                for row_id in row_ids
            }
            if is_cancelled():
                raw_results: dict[int, Any] = {}
            else:
                try:
                    async with recipe.execution_scope(
                        spec, ctx, expected_rows=len(row_ids)
                    ):
                        raw_results = await recipe.execute_batch(
                            values_by_row, spec, ctx
                        )  # type: ignore[attr-defined]
                except (SandboxTeardownError, StaleAttemptWriter):
                    raise
                except (ValueError, RuntimeError) as e:
                    raw_results = {
                        row_id: {
                            "__error__": safe_error(
                                "model_error", e, max_chars=500
                            ).detail
                        }
                        for row_id in row_ids
                    }
            for row_id in row_ids:
                if is_cancelled() and row_id not in raw_results:
                    break
                cells, _failed, _cost = normalize_batch_row(
                    raw_results.get(row_id),
                    field_names,
                    required_field_names=required_field_names,
                    managed_publication=managed_publication,
                )
                record_row(row_id, cells)
                values[row_id] = {
                    name: {
                        key: value
                        for key, value in cell.items()
                        if key != "publication_effect"
                    }
                    for name, cell in cells.items()
                }
                done += 1
                if progress_cb is not None:
                    progress_cb(done, total_sample)
        else:
            lock = asyncio.Lock()
            worker_count = row_worker_count(recipe, spec)
            semaphore = asyncio.Semaphore(worker_count)

            async def one_row(row_id: int) -> None:
                nonlocal done
                async with semaphore:
                    if is_cancelled():
                        return
                    row_input_values = row_values(
                        project,
                        recipe,
                        spec,
                        col_map,
                        row_id,
                        for_model=recipe.is_llm(spec),
                        column_types=source_column_types,
                    )
                    cells = await execute_row(
                        project,
                        router,
                        throttle,
                        recipe,
                        row_input_values,
                        spec,
                        ctx,
                        row_id=row_id,
                        recorder=None,
                        required_output_field_names=required_field_names,
                        managed_publication=managed_publication,
                    )
                    async with lock:
                        record_row(row_id, cells)
                        # Cancellation fence: mirror the durable path — a
                        # cancel observed by the time this row's cells are
                        # ready drops them, so a cancelled preview persists
                        # no row values or progress.
                        if is_cancelled():
                            return
                        values[row_id] = {
                            name: {
                                key: value
                                for key, value in cells[name].items()
                                if key != "publication_effect"
                            }
                            for name in field_names
                            if name in cells
                        }
                        done += 1
                        if progress_cb is not None:
                            progress_cb(done, total_sample)

            if total_sample > 0 and not is_cancelled():
                async with recipe.execution_scope(
                    spec, ctx, expected_rows=total_sample
                ):
                    await _await_owned_tasks(*(one_row(row_id) for row_id in row_ids))

    except BaseException as exc:
        primary = exc
        raise
    finally:
        if attempt is not None:
            remainder = [
                {
                    "row_id": row_id,
                    "outcome": "cancelled"
                    if isinstance(primary, asyncio.CancelledError) or is_cancelled()
                    else "model_error",
                }
                for row_id in row_ids
                if row_id not in accounted_rows
            ]
            if remainder:
                try:
                    run_store.write_receipt_row_outcomes(
                        attempt.receipt_id,
                        remainder,
                        writer_attempt_id=attempt.attempt_id,
                    )
                except BaseException as accounting_error:
                    if primary is not None:
                        primary.add_note("Preview row outcomes could not be finalized.")
                        raise primary from accounting_error
                    raise

    return PreviewResult(
        sheet_id=sheet_id,
        columns=preview_columns,
        values=values,
        row_ids=list(row_ids),
        sampled=total_sample,
        total=total,
    )
