"""Fill a project's response cache with fixture replies for one typed map action.

Usage:
    python scripts/e2e/prime_replies.py <workspace> <project id> < {"action": ..., "replies": [...]}

The action is the exact request the run endpoint will receive. Each reply is
the model output for one row of its scope, in row order, and must satisfy the
response schema the action renders for that row. Keys come from the
production plan (build_typed_map_rows_plan, the row inputs the runner reads,
request_key), so a run under FRISKET_CACHE_MODE=replay_strict replays them
without a provider key and fails if any call misses.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema.validators import validator_for

from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import LLMRequest, LLMResponse, ResponseCache, request_key
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.runner.row_inputs import row_values
from frisket.engine.store import Project


def prime_map_rows_replies(
    project: Project,
    action: Mapping[str, Any],
    replies: Sequence[Mapping[str, Any]],
) -> int:
    """Store one cached reply per scoped row; return how many were stored."""
    plan = build_typed_map_rows_plan(project, typed_action_for_request(dict(action)))
    spec = plan.spec_dict()
    sheet_id = spec["sheet_id"]
    scope = action["scope"]
    row_ids = list(scope.get("row_ids") or project.visible_row_ids(sheet_id))
    if len(row_ids) != len(replies):
        raise ValueError(
            f"{action['action_id']}: {len(row_ids)} rows in scope, "
            f"{len(replies)} replies given"
        )
    columns = {column["name"]: column["id"] for column in project.columns(sheet_id)}
    entries = []
    for row_id, reply in zip(row_ids, replies, strict=True):
        values = row_values(project, plan.program, dict(spec), columns, row_id)
        call = plan.program.render(values, spec)
        if call.schema is not None:
            validator_for(call.schema)(call.schema).validate(dict(reply))
        request = LLMRequest(
            model=spec["model"],
            messages=call.messages,
            schema=call.schema,
            max_tokens=call.max_tokens,
        )
        entries.append((request_key(request, plan.program.version), reply))
    cache = ResponseCache(project.path / "project.cache.db")
    try:
        for key, reply in entries:
            cache.put(
                key,
                LLMResponse(
                    content=json.dumps(reply),
                    data=dict(reply),
                    tokens_in=24,
                    tokens_out=8,
                    cost=0.0,
                    model=spec["model"],
                ),
            )
    finally:
        cache.close()
    return len(row_ids)


def main() -> None:
    workspace, project_id = sys.argv[1], sys.argv[2]
    body = json.load(sys.stdin)
    project = Project(Path(workspace) / f"{project_id}.frisket")
    try:
        prime_map_rows_replies(project, body["action"], body["replies"])
    finally:
        project.close()


if __name__ == "__main__":
    main()
