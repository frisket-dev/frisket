"""Publish typed update requests through the normal table import worker."""

from __future__ import annotations

from typing import Any

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import ActionResult
from frisket.engine.executor.action_jobs import launch_queued_action_job
from frisket.engine.executor.import_update_admission import admit_typed_update
from frisket.engine.jobs.import_table import table_import_dedupe_key
from frisket.engine.jobs.queue import IMPORT_TABLE_STREAM_KIND


def queue_import_update(
    project: Any, project_id: str, body: dict, ctx: Any
) -> ActionResult:
    admitted = admit_typed_update(
        project, project_id, typed_action_for_request(body), ctx.request_executor_deps()
    )
    if isinstance(admitted, ActionResult):
        return admitted
    frozen, envelope = admitted
    return launch_queued_action_job(
        envelope=envelope,
        queue=ctx.queue,
        job_kind=IMPORT_TABLE_STREAM_KIND,
        project=project,
        payload_extra={
            **ctx.queue_payload_extra,
            "workspace_root": str(ctx.workspace_root),
            "import_ref": frozen.import_ref,
            "plan_hash": frozen.plan_hash,
            "receipt_id": envelope.receipt_id,
            "dedupe_key": table_import_dedupe_key(
                project_id, frozen.import_ref, frozen.plan_hash
            ),
        },
    )
