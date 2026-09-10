"""Small shared helpers for action catalog definitions."""

from __future__ import annotations

from collections.abc import Sequence

from frisket.contracts.action import ActionErrorSpec, _idempotency_stale_running_error


TEMPORAL_PERSISTENCE_ERROR_CODES = frozenset(
    {
        "invalid_temporal_value",
        "timeline_not_found",
        "timeline_stale",
        "timeline_duration_required",
        "ambiguous_time_mapping",
        "range_out_of_bounds",
    }
)


def error_specs(**messages: str) -> list[ActionErrorSpec]:
    """Build fresh ordered catalog error specs from ``code=message`` data.

    Python preserves keyword order, and action error codes are lower-snake-case
    identifiers, so a declaration stays both compact and visibly ordered.
    """

    return [
        ActionErrorSpec(code=code, message=message)
        for code, message in messages.items()
    ]


def temporal_persistence_errors() -> list[ActionErrorSpec]:
    """Fresh catalog specs for any action that can persist temporal values."""

    return error_specs(
        invalid_temporal_value="The temporal value is malformed.",
        timeline_not_found="The temporal value refers to a missing timeline.",
        timeline_stale="The temporal value timeline anchor is stale.",
        timeline_duration_required="The temporal timeline duration is not finalized.",
        ambiguous_time_mapping="The temporal value timeline mapping is ambiguous.",
        range_out_of_bounds="The temporal value exceeds its timeline duration.",
    )


def output_column_busy_error() -> ActionErrorSpec:
    return ActionErrorSpec(
        code="output_column_busy",
        message="The target output column is claimed by a running action.",
    )


_CAP_PHRASE = {"project:write": "project write", "model:complete": "model completion"}


def _capability_phrase(capabilities: tuple[str, ...]) -> str:
    parts: list[str] = []
    for cap in capabilities:
        if cap in _CAP_PHRASE:
            parts.append(_CAP_PHRASE[cap])
        elif cap.startswith("external:"):
            parts.append(f"{cap.split(':', 1)[1]} external API")
        else:
            parts.append(cap)
    return " and ".join(parts)


def op_errors(
    kind: str,
    *,
    capabilities: tuple[str, ...] = ("project:write",),
    recipe: str | None = None,
    multi_output: bool = False,
    model_backed: bool = False,
    cost_confirmation: bool = False,
    external: bool = False,
    queued: bool = False,
    value_checked: bool = True,
    validation: Sequence[ActionErrorSpec] = (),
) -> list[ActionErrorSpec]:
    """The canonical, ordered error catalog for a reserved-maprunner op.

    Derives the standard error set + a kind/recipe/capability-templated message from
    archetype flags, so an op declares its archetype plus a few
    op-specific `validation` errors instead of ~11 hand-written `ActionErrorSpec`s. The
    standard messages combine the per-op variants (drift / kind-templating); the runtime
    `ActionError` still carries op-specific field/details, so only the catalog
    documentation text is canonicalized. `validation` is slotted after invalid_input_ref.

    Lives in the contracts layer (not sdk) so it reloads with `frisket.contracts` and the
    `ActionErrorSpec` class never diverges from the catalog's.
    """
    recipe = recipe or kind.split(".")[-1]
    errs: list[ActionErrorSpec] = [
        *error_specs(
            missing_capability=(
                f"{kind} requires {_capability_phrase(capabilities)} capability."
            ),
            missing_idempotency_key=(
                f"{kind} requires an idempotency key for safe retry."
            ),
            invalid_input_ref=(
                "The referenced sheet, rows, or input columns do not exist."
            ),
        ),
        *validation,
        *error_specs(
            output_column_exists=(
                "The requested output columns already exist."
                if multi_output
                else "The requested output column already exists."
            )
        ),
        output_column_busy_error(),
    ]
    # `cost_confirmation` is the non-model way to reach the same error: an op
    # whose cost is UNPRICEABLE (an authored URL has no readable meter) gates on
    # an unknown estimate and surfaces it through this same code, so the catalog
    # must document it without the op claiming to be model-backed.
    if model_backed or cost_confirmation:
        errs.extend(
            error_specs(
                model_cost_requires_confirmation=(
                    "The model run exceeded or could not compute the cost gate."
                )
            )
        )
    if external:
        errs.extend(
            error_specs(
                external_cost_requires_confirmation=(
                    "External provider calls require confirmed=true."
                )
            )
        )
    if model_backed:
        errs.extend(
            error_specs(
                model_run_failed=(
                    f"The model-backed {recipe} run failed to produce usable row output."
                )
            )
        )
    else:
        errs.extend(
            error_specs(map_run_failed=f"The {recipe} run or receipt lookup failed.")
        )
    if external:
        errs.extend(
            error_specs(
                project_write_failed=(
                    "The project receipt update failed after execution."
                )
            )
        )
    errs.extend(
        error_specs(
            idempotency_conflict=(
                "The idempotency key was previously used with different params."
            )
        )
    )
    if not queued:
        errs.extend(
            error_specs(
                idempotency_in_progress=(
                    f"A prior {kind} action with this key is still running."
                )
            )
        )
        errs.append(_idempotency_stale_running_error())
    if value_checked:
        errs.extend(
            error_specs(
                stale_replay=(
                    "The stored receipt no longer matches live output columns, "
                    "rows, or values."
                )
            )
        )
    return errs
