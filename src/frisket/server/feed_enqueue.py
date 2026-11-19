"""Queue manual feed polls using the same worker as scheduled feeds."""

from __future__ import annotations

import json
from typing import Any

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import ActionIdentity, ActionOutput, ActionResult
from frisket.engine.executor.feed_poll_action import _feed_poll_sheet_id_in_txn
from frisket.engine.store.feeds import FeedStore
from frisket.feeds import ensure_rss_feed_checker, get_feed_checker


def queue_feed_poll(
    project: Any, project_id: str, body: dict, ctx: Any
) -> ActionResult | None:
    bound = typed_action_for_request(body)
    feed_id = bound.params.source
    if type(feed_id) is not int:
        return None
    feed = FeedStore(project).get_feed(feed_id)
    if feed is None:
        raise ValueError("Feed not found")
    feed = dict(feed)
    if not feed["enabled"]:
        raise ValueError("Enable this feed before importing it")
    feed["config"] = json.loads(feed.get("config") or "{}")
    ensure_rss_feed_checker()
    feed_checker = get_feed_checker(feed["kind"])
    if feed_checker is None:
        raise ValueError("Frisket cannot check this feed type")
    error = feed_checker.validate_config(feed)
    if error:
        raise ValueError(error)
    # The source-bound destination is durable before any provider request starts.
    with project.db:
        sheet_id = _feed_poll_sheet_id_in_txn(project, project.db.cursor(), feed)
    existing = [
        job
        for status in ("queued", "running")
        for job in ctx.queue.list_project_jobs(
            project_id,
            storage_org_id=ctx.queue_payload_extra.get("storage_org_id"),
            status=status,
            kind="source.poll",
            source_id=feed_id,
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
                "source_id": feed_id,
                "sheet_id": sheet_id,
                "source_name": feed["name"],
                "action_kind": "source.poll",
                "poll_id": bound.request.idempotency_key,
            },
            max_attempts=1,
        )
    )
    return ActionResult(
        action=ActionIdentity(
            kind="source.poll", action_id=bound.request.idempotency_key
        ),
        status="queued",
        project_id=project_id,
        job_id=job_id,
        outputs=[ActionOutput(kind="sheet", name=feed["name"], sheet_id=sheet_id)],
    )
