"""Restore import authority and let the deployment admit each deferred child."""

from contextlib import nullcontext
from dataclasses import replace
import json
from typing import Any
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager

from frisket.execution.provider import ExecutionCompositionContext
from frisket.project_identity import ProjectStorageKey


def import_child_port(
    workspace: Any,
) -> Callable[..., AbstractContextManager[ExecutionCompositionContext]] | None:
    return getattr(
        getattr(workspace, "direct_action_receipt_settlement_port", None),
        "admit_import_child",
        None,
    )


def import_execution_context(
    workspace: Any, project_id: str, receipt_id: str
) -> ExecutionCompositionContext | None:
    if import_child_port(workspace) is None:
        return None
    project = workspace.get(project_id)
    row = project.db.execute(
        "SELECT action_kind,edition_run_context FROM receipts WHERE id=?",
        (receipt_id,),
    ).fetchone()
    if row is None or row["action_kind"] not in {"import.files", "import.packet_split"}:
        raise ValueError("PDF preparation has no admitted parent import")
    snapshot = json.loads(row["edition_run_context"] or "null")
    if not isinstance(snapshot, dict):
        raise ValueError("PDF preparation parent has no execution context")
    return replace(
        ExecutionCompositionContext.direct(),
        storage_key=ProjectStorageKey(
            storage_org_id=workspace.queue_storage_org_id, project_slug=project_id
        ),
        edition_snapshot=snapshot,
    )


def admit_import_child(
    workspace: Any,
    project_id: str,
    parent_receipt_id: str,
    child_key: str,
    quote: Mapping[str, Any],
    *,
    receipt_id: str | None = None,
) -> AbstractContextManager[ExecutionCompositionContext | None]:
    port = import_child_port(workspace)
    if port is None:
        return nullcontext(None)
    return port(
        project=workspace.get(project_id),
        project_id=project_id,
        parent_receipt_id=parent_receipt_id,
        child_key=child_key,
        quote=quote,
        receipt_id=receipt_id,
    )
