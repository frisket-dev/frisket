"""Product-side terminalization for queue terminal/retry events.

The queue (``frisket.jobs.queue``) is infrastructure: when a job goes
terminal (cancel, retry exhaustion, lease exhaustion) or an admin retry
requeues a terminal job, it fires a registered
``TerminalizationHook`` and knows nothing else. THIS module is the product
side of that seam: it drives the paired v1 receipt (``action.run`` jobs)
terminal and, on admin retry, reopens the finalized run row / receipt so the
retried job's handler actually executes instead of hitting its own
idempotency guards. ``register_queue_terminalization`` is called from
``jobs.worker.register_production_handlers`` — the ONE product registration
every edition composes — so every composition root (server workspace, worker
CLI, team app, cloud compose) gets the hook with its declared storage roots.

Storage routing is DECLARED registration config, never ambient: the hosted
branch resolves a claimed ``ProjectStorageKey`` against the registered
``workspace_root`` (+ optional ``workspace_root_storage_org_id``) using the
same three-way rule as ``claimed_project_location`` — org-scoped root and a
matching claim resolves directly under the root, a mismatched claim fails
closed, an unscoped root appends the claim's org id. (This replaced a
``FRISKET_DATA_DIR`` env read that contradicted the queue module's own
declared-not-ambient doctrine.) Payload/workspace_root fields stay
caller-controlled diagnostics that must never select a hosted tenant's
physical project.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from frisket.engine.jobs.queue import (
    ACTION_RUN_KIND,
    Job,
    JobQueue,
    _coerce_str,
    claimed_project_root,
)
from frisket.project_opener import ProjectOpener
from frisket.project_identity import ProjectStorageKey

LOG = logging.getLogger("frisket.executor.queue_terminalization")


class RunTerminalization:
    """The registered ``TerminalizationHook`` implementation."""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        workspace_root_storage_org_id: int | None = None,
        project_opener: ProjectOpener | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._storage_org_id = workspace_root_storage_org_id
        self._project_opener = project_opener

    def __call__(self, event: str, job: Job, *, hosted: bool) -> None:
        self._terminalize_action_run_job(job, hosted=hosted, reason=event)

    # ---------- routing ----------

    def _trusted_job_project_path(
        self, job: Job, *, hosted: bool
    ) -> tuple[str, Path, ProjectStorageKey | None] | None:
        """Resolve a durable project for retry/terminalization (or ``None``
        to leave the row untouched — fail closed)."""
        if hosted:
            storage_key = job.project_storage_key
            if storage_key is None:
                return None
            try:
                project_root = claimed_project_root(
                    storage_key,
                    workspace_root=self._workspace_root,
                    workspace_root_storage_org_id=self._storage_org_id,
                )
            except ValueError:
                return None  # claim belongs to another org's storage root
            return (
                storage_key.project_slug,
                project_root / f"{storage_key.project_slug}.frisket",
                storage_key,
            )
        if self._project_opener is not None:
            # An injected opener may only ever be handed a CLAIMED identity;
            # the unhosted branch below derives its path from caller-controlled
            # payload fields, so there is nothing safe to open here.
            return None
        payload = job.payload if isinstance(job.payload, Mapping) else {}
        project_id = _coerce_str(payload.get("project_id") or job.project_id)
        workspace_root = _coerce_str(
            payload.get("workspace_root") or job.workspace_root
        ) or str(self._workspace_root)
        if project_id is None:
            return None
        return project_id, Path(workspace_root) / f"{project_id}.frisket", None

    # ---------- terminal receipts ----------

    def _terminalize_action_run_job(
        self, job: Job, *, hosted: bool, reason: str
    ) -> None:
        if job.kind != ACTION_RUN_KIND:
            return
        payload = job.payload if isinstance(job.payload, Mapping) else {}
        trusted_project = self._trusted_job_project_path(job, hosted=hosted)
        if trusted_project is None:
            return
        project_id, project_path, storage_key = trusted_project
        receipt_id = _coerce_str(
            payload.get("v1_receipt_id") or payload.get("receipt_id") or job.receipt_id
        )
        action_kind = _coerce_str(payload.get("action_kind") or job.action_kind)
        if not receipt_id or not action_kind:
            return

        from frisket.engine.executor.action_jobs import (
            action_job_cancelled_result,
            action_job_exhausted_lease_result,
            action_job_retry_exhausted_result,
        )
        from frisket.engine.store import Project

        terminalize = {
            "cancelled": action_job_cancelled_result,
            "lease_exhausted": action_job_exhausted_lease_result,
        }.get(reason, action_job_retry_exhausted_result)

        try:
            project = (
                Project(project_path)
                if self._project_opener is None
                else self._project_opener(storage_key, project_path)
            )
        except FileNotFoundError:
            return
        try:
            terminalize(
                project,
                project_id=project_id,
                receipt_id=receipt_id,
                action_kind=action_kind,
                job_id=job.id,
            )
        finally:
            project.close()


def register_queue_terminalization(
    queue: JobQueue,
    *,
    workspace_root: str | Path,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> None:
    """Register the product terminalization hook on an open queue.

    A queue implementation without the hook seam (test fakes) is left alone —
    it then performs pure job-row transitions, which is the infrastructure
    contract anyway.
    """
    set_hook = getattr(queue, "set_terminalization_hook", None)
    if set_hook is None:
        return
    set_hook(
        RunTerminalization(
            workspace_root=workspace_root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
            project_opener=project_opener,
        )
    )
