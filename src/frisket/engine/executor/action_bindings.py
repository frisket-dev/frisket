"""Composition root for action-job executors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.core import GoogleSheetsExport, CreateSheet
from frisket.actions.transcript_types import TranscriptReader
from frisket.actions.temporal_types import TemporalMediaReader
from frisket.actions.find_types import FindScanner
from frisket.engine.executor.google_sheets_action import (
    run_google_sheets_action_job as execute_google_sheets_job,
)
from frisket.engine.executor.find_action import run_typed_find_action_job
from frisket.engine.executor.action_jobs import ActionJobEnvelope, ActionJobExecutor
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.action_specs import declared_queued_action_job_kinds
from frisket.engine.executor.table_action import run_typed_table_action_job
from frisket.engine.executor.temporal_extract_action import (
    run_typed_temporal_extract_job,
    supports_typed_temporal_extract_action,
)

ExecutorDepsFactory = Callable[[str, Any], ExecutorDeps | None]


def _contributed_action_job_executors(
    *,
    executor_deps_factory: ExecutorDepsFactory | None = None,
) -> dict[str, ActionJobExecutor]:
    def run_google_sheets_action_job(
        project: Any,
        envelope: ActionJobEnvelope,
    ):
        deps = (
            executor_deps_factory(envelope.project_id, None)
            if executor_deps_factory is not None
            else None
        )
        return execute_google_sheets_job(
            project,
            envelope,
            deps=deps if deps is not None else ExecutorDeps(),
        )

    def run_map_find_action_job(
        project: Any,
        envelope: ActionJobEnvelope,
    ):
        deps = (
            executor_deps_factory(envelope.project_id, None)
            if executor_deps_factory is not None
            else None
        )
        return run_typed_find_action_job(
            project,
            envelope,
            deps=deps if deps is not None else ExecutorDeps(),
        )

    return {
        **{
            registered.action_id: run_typed_table_action_job
            for registered in ACTION_REGISTRY.actions
            if isinstance(registered.definition.run, CreateSheet)
            and any(
                cap in registered.definition.run.capabilities
                for cap in (TranscriptReader, TemporalMediaReader)
            )
        },
        **{
            registered.action_id: run_typed_temporal_extract_job
            for registered in ACTION_REGISTRY.actions
            if supports_typed_temporal_extract_action(registered.definition.run)
        },
        **{
            registered.action_id: run_google_sheets_action_job
            for registered in ACTION_REGISTRY.actions
            if isinstance(registered.definition.run, GoogleSheetsExport)
        },
        **{
            registered.action_id: run_map_find_action_job
            for registered in ACTION_REGISTRY.actions
            if getattr(registered.definition.run, "capabilities", ()) == (FindScanner,)
        },
    }


def action_job_bindings(
    *,
    executor_deps_factory: ExecutorDepsFactory | None = None,
) -> Mapping[str, ActionJobExecutor]:
    """Return installed action-job kinds mapped directly to their executors."""

    bindings_by_kind = _contributed_action_job_executors(
        executor_deps_factory=executor_deps_factory
    )
    declared_kinds = set(declared_queued_action_job_kinds())
    contributed_kinds = set(bindings_by_kind)
    misplaced = sorted(contributed_kinds - declared_kinds)
    if misplaced:
        raise RuntimeError(
            f"action-job action bindings lack declared queued placement: {misplaced}"
        )

    undefined = sorted(contributed_kinds - set(ACTION_REGISTRY.action_ids))
    if undefined:
        raise RuntimeError(
            f"action-job action bindings lack canonical definitions: {undefined}"
        )

    return MappingProxyType(bindings_by_kind)
