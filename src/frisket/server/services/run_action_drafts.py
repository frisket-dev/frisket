"""Canonical typed action reconstruction shared by run-backed authoring flows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from frisket.engine.executor.action_families.runs import (
    typed_run_has_matching_producer_receipt,
)
from frisket.engine.executor.map_rows_action import (
    bound_typed_program_request_from_runner_spec,
    normalized_typed_request_identity,
)


@dataclass(frozen=True)
class ReconstructedRunActionDraft:
    runner_spec: dict[str, Any]
    bound: Any
    draft: dict[str, Any]


def reconstruct_run_action_draft(
    project: Any,
    run: Mapping[str, Any],
) -> ReconstructedRunActionDraft:
    runner_spec = json.loads(run["params"] or "{}")
    if not isinstance(runner_spec, dict):
        raise ValueError("stored run spec is not an object")
    bound = bound_typed_program_request_from_runner_spec(runner_spec, project=project)
    if bound is None:
        raise ValueError("unsupported action family")
    run_id = int(run["id"])
    if not typed_run_has_matching_producer_receipt(project, run_id=run_id, bound=bound):
        raise ValueError("stored action identity does not match its producer")
    return ReconstructedRunActionDraft(
        runner_spec=dict(runner_spec),
        bound=bound,
        draft=normalized_typed_request_identity(bound),
    )


__all__ = ["ReconstructedRunActionDraft", "reconstruct_run_action_draft"]
