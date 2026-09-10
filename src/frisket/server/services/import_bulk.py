"""Project-scoped, bounded-memory bulk import staging."""

from __future__ import annotations

import fcntl
import shutil
import time
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


class ImportBulkService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        limits: BulkImportLimits | None = None,
        clock=time.time,
    ):
        self.ws, self.limits = workspace, limits or BulkImportLimits()
        self.plan_lifecycle = BulkPlanLifecycle(clock)
        self.executor = BulkExecutor(workspace)

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
            write_lock = open_lock_file(root / ".execute.lock")
            fcntl.flock(write_lock.fileno(), fcntl.LOCK_EX)
            return self.executor.execute(
                project,
                project_id,
                plan,
                outputs,
                staged,
                decisions,
                deps=executor_deps,
                max_rows=max_rows,
            )
        finally:
            if "write_lock" in locals():
                fcntl.flock(write_lock.fileno(), fcntl.LOCK_UN)
                write_lock.close()
            shutil.rmtree(plan, ignore_errors=True)
            import_bulk_sources.fsync_directory(root)
            fcntl.flock(claim.fileno(), fcntl.LOCK_UN)
            claim.close()


__all__ = [
    "BulkImportLimits",
    "BulkUpload",
    "ImportBulkRouteError",
    "ImportBulkService",
]
