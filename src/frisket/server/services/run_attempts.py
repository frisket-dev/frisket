"""Run-keyed attempt-receipt reader.

The run-scoped half of the attempt receipt: every attempt this run made,
in one payload. Its sibling ``ProjectAttemptsService`` asks the same
question without the run key, so it can also reach a compaction-orphaned
attempt (compaction clears ``run_id``); this one is the reader a run detail
surface wants.
"""

from __future__ import annotations

from typing import Any

from frisket.server.workspace import Workspace


class RunAttemptsService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def receipts(self, project_id: str, run_id: int) -> dict[str, Any]:
        """Preserve Workspace.get's canonical missing-project behavior."""
        from frisket.execution.attempt import run_attempt_receipts

        project = self._workspace.get(project_id)
        return {
            "run_id": run_id,
            "attempts": run_attempt_receipts(
                project,
                run_id,
            ),
        }
