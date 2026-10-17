"""Project-scoped, bounded-memory bulk import staging."""

from __future__ import annotations

import shutil
import time
import logging
from typing import Any

from frisket.engine.executor import ExecutorDeps
from frisket.server.services import import_bulk_sources
from frisket.server.services.import_bulk_execute import BulkExecutor
from frisket.server.services.import_bulk_plan import BulkPlanLifecycle, open_lock_file
from frisket.server.services.import_bulk_types import (
    BulkImportLimits,
    BulkUpload,
    ImportBulkRouteError,
)
from frisket.server.workspace import Workspace

LOG = logging.getLogger(__name__)


class ImportBulkService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        limits: BulkImportLimits | None = None,
        clock=time.time,
        search_preparation: Any = None,
    ):
        self.ws, self.limits = workspace, limits or BulkImportLimits()
        self.plan_lifecycle = BulkPlanLifecycle(clock)
        self.executor = BulkExecutor(workspace)
        self.search_preparation = search_preparation

    def executor_deps_for_request(self, project_id: str, request: Any) -> ExecutorDeps:
        """Resolve the deployment composition once at the HTTP boundary."""

        factory = self.ws.executor_deps_factory
        return (
            factory(project_id, request) if factory is not None else None
        ) or ExecutorDeps()

    async def plan(
        self, project_id: str, *, uploads: list[BulkUpload], expand_archive: bool
    ) -> dict[str, Any]:
        project = self.ws.get(project_id)
        return await self.plan_lifecycle.create_plan(
            project,
            project_id,
            uploads=uploads,
            expand_archive=expand_archive,
            limits=self.limits,
        )

    def execute(
        self,
        project_id: str,
        plan_id: str,
        *,
        decisions: dict[str, str],
        deps: ExecutorDeps | None = None,
    ) -> dict[str, Any]:
        project = self.ws.get(project_id)
        executor_deps = deps or ExecutorDeps()
        max_rows = (
            executor_deps.import_workload_limits.max_rows
            if executor_deps.import_workload_limits is not None
            else None
        )
        root, plan, claim, outputs, staged = self.plan_lifecycle.claim_plan(
            project, project_id, plan_id, decisions
        )
        try:
            # From the first post-claim instruction onward, cleanup owns the
            # claimed manifest, staged files, and claim lock.
            with open_lock_file(root / ".execute.lock"):
                result = self.executor.execute(
                    project,
                    project_id,
                    plan,
                    outputs,
                    staged,
                    decisions,
                    deps=executor_deps,
                    max_rows=max_rows,
                )
                for created in result["created"]:
                    created["search_preparation_batch_id"] = None
                    if self.search_preparation is not None:
                        sheet_id = int(created["sheet_id"])
                        try:
                            created["search_preparation_batch_id"] = (
                                self.search_preparation.create_after_materialization(
                                    project_id,
                                    sheet_id=sheet_id,
                                    row_ids=project.visible_row_ids(sheet_id),
                                )
                            )
                        except Exception:
                            LOG.warning(
                                "search_preparation_import_hook_failed", exc_info=True
                            )
                return result
        finally:
            # Release before removing the plan: Windows does not allow deleting
            # a directory that contains an open lock file.
            claim.release()
            shutil.rmtree(plan, ignore_errors=True)
            import_bulk_sources.fsync_directory(root)


__all__ = [
    "BulkImportLimits",
    "BulkUpload",
    "ImportBulkRouteError",
    "ImportBulkService",
]
