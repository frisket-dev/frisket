from __future__ import annotations

from frisket.engine.executor.action_specs import (
    ACCEPTED_EXECUTION_OUTLIERS,
    PlacementPolicy,
    execution_spec_for,
)


def placement_for_kind(kind: str) -> PlacementPolicy:
    """Declared placement for an action kind.

    Any explicitly accepted outlier without an execution spec runs inline.
    """

    spec = execution_spec_for(kind)
    if spec is None:
        if kind in ACCEPTED_EXECUTION_OUTLIERS:
            return PlacementPolicy.INLINE
        raise KeyError(f"no execution spec or accepted outlier for action kind {kind}")
    return spec.lifecycle.placement


def is_queued(kind: str) -> bool:
    """True when the action's declared placement runs through a queue."""

    return placement_for_kind(kind) in (
        PlacementPolicy.QUEUED_PROJECT_RUN,
        PlacementPolicy.QUEUED_ACTION_JOB,
    )
