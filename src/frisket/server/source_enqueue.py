"""Queue manual feed polls using the same worker as scheduled sources."""

from __future__ import annotations

import json
from typing import Any

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import ActionOutput, ActionResult
from frisket.engine.executor.source_poll_action import _source_poll_sheet_id_in_txn
from frisket.engine.store.sources import SourceStore
from frisket.server.sources import ensure_rss_poller, get_source_poller


def queue_source_poll(
    project: Any, project_id: str, body: dict, ctx: Any
) -> ActionResult | None:
    bound = typed_action_for_request(body)
    source_id = bound.params.source
    if type(source_id) is not int:
        return None
    source = SourceStore(project).get_source(source_id)
    if source is None:
        raise ValueError("Source not found")
    source = dict(source)
    if not source["enabled"]:
        raise ValueError("Enable this source before importing it")
    source["config"] = json.loads(source.get("config") or "{}")
    ensure_rss_poller()
    poller = get_source_poller(source["kind"])
    if poller is None:
        raise ValueError("No registered poller supports this source kind")
    error = poller.validate_config(source)
    if error:
        raise ValueError(error)
    # The source-bound destination is durable before any provider request starts.
    with project.db:
        sheet_id = _source_poll_sheet_id_in_txn(project, project.db.cursor(), source)
    existing = [
        job
        for status in ("queued", "running")
        for job in ctx.queue.list_project_jobs(
            project_id,
            storage_org_id=ctx.queue_payload_extra.get("storage_org_id"),
            status=status,
            kind="source.poll",
            source_id=source_id,
            limit=1,
        )
    ]
    job_id = (
        existing[0].id
        if existing
        else ctx.queue.enqueue(
            "source.poll",
            {
                **ctx.queue_payload_extra,
                "project_id": project_id,
                "workspace_root": str(ctx.workspace_root),
                "source_id": source_id,
                "sheet_id": sheet_id,
                "source_name": source["name"],
                "action_kind": "source.poll",
                "poll_id": bound.request.idempotency_key,
            },
            max_attempts=1,
        )
    )
    return ActionResult(
        action={"kind": "source.poll", "action_id": bound.request.idempotency_key},
        status="queued",
        project_id=project_id,
        job_id=job_id,
        outputs=[ActionOutput(kind="sheet", name=source["name"], sheet_id=sheet_id)],
    )
