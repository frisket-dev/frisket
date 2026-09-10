"""Project-level attempt-receipt reader.

The receipt answers three of the four product sentences for one attempt —
"did I say yes to that", "where did my data go", "what did it cost". The
only reader was keyed on ``run_id``, so a compaction-orphaned attempt whose
``run_id`` was cleared was a preserved record nothing could open. This
service lists receipts by PROJECT, with the run filter as an option rather
than the key.
"""

from __future__ import annotations

from typing import Any

from frisket.server.paging import offset_page_payload
from frisket.server.workspace import Workspace

ATTEMPT_RECEIPTS_SCHEMA_VERSION = "frisket.attempt_receipts.v1"


class ProjectAttemptsService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def page(
        self,
        project_id: str,
        *,
        run_id: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        """Preserve Workspace.get's canonical missing-project behavior."""
        from frisket.execution.attempt import project_attempt_receipts

        project = self._workspace.get(project_id)
        total, receipts = project_attempt_receipts(
            project,
            run_id=run_id,
            offset=offset,
            limit=limit,
        )
        payload = offset_page_payload(
            ATTEMPT_RECEIPTS_SCHEMA_VERSION,
            items=receipts,
            item_key="attempts",
            offset=offset,
            limit=limit,
            total=total,
            order="created_at DESC",
        )
        payload["run_id"] = run_id
        return payload
