from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import ActionRequest
from frisket.actions.system import BoundTypedActionRequest
from frisket.ai.llm import ModelRouter
from frisket.contracts.action import ActionResult
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.runner import MapRunner
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


# Default for the first migrated map-action tests; action families that mutate
# sources, blobs, artifacts, or sheets should pass their own table list.
DEFAULT_COUNT_TABLES = ("columns", "runs", "results", "model_calls", "ops", "receipts")


def write_json(tmp_path: Path, name: str, payload: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def table_count(project: Project, table: str) -> int:
    return int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def counts(
    project: Project,
    tables: tuple[str, ...] = DEFAULT_COUNT_TABLES,
) -> dict[str, int]:
    return {table: table_count(project, table) for table in tables}


def typed_map_request(
    action_id: str,
    sheet_id: int,
    *,
    params: dict[str, Any],
    output_names: dict[str, str],
    idempotency_key: str,
    row_ids: list[int] | tuple[int, ...] | None = None,
) -> dict[str, Any]:
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = list(row_ids)
    return {
        "action_id": action_id,
        "scope": scope,
        "params": params,
        "output_names": dict(output_names),
        "idempotency_key": idempotency_key,
    }


def run_typed_map_request(
    project: Project,
    request_body: dict[str, Any],
    *,
    project_id: str,
    **run_kwargs: Any,
) -> ActionResult:
    if run_kwargs or "action_id" not in request_body:
        from frisket.engine.executor import run_action_spec

        return run_action_spec(
            project, request_body, project_id=project_id, **run_kwargs
        )
    request = ActionRequest.model_validate(request_body)
    registered = ACTION_REGISTRY.get(request.action_id)
    from frisket.actions.core import (
        CreateSheet,
        SemanticJoin,
        _ProjectAction,
        routed_capability,
    )

    if (
        isinstance(
            registered.definition.run, (CreateSheet, SemanticJoin, _ProjectAction)
        )
        or routed_capability(registered.definition.run) is not None
    ):
        from frisket.engine.executor.actions import run_action_spec

        return run_action_spec(project, request_body, project_id=project_id)

    def runner_factory(target: Project, router: Any | None) -> MapRunner:
        return MapRunner(
            target,
            router or ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(target),
        )

    return run_typed_map_rows_action(
        project,
        project_id,
        BoundTypedActionRequest.bind(registered, request),
        None,
        runner_factory,
    )
