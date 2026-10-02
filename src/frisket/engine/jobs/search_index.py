"""Bounded keyword-index maintenance on the existing project queue."""

from __future__ import annotations

from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.search_index_work import latest_revision, read_dirty_scopes
from frisket.project_identity import ProjectStorageKey
from frisket.search import index_batch, index_needs_work
from frisket.search_storage import reclaim_is_pending, reclaim_search_storage

from .ports import JobHandlerContext
from .project_opener import ProjectOpener, open_claimed_project
from .queue import (
    CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY,
    SEARCH_INDEX_KIND,
    JobQueue,
    claimed_project_location,
)
from .worker import HandlerRegistration, HandlerRegistry

INDEX_BATCH_SIZE = 1000
BATCHES_PER_CLAIM = 4


def process_index_batches(
    project: Project, *, stopped=lambda: False
) -> tuple[int, bool]:
    """Process one fair worker quantum of durable keyword-index work."""
    processed = 0
    complete = False
    for _ in range(BATCHES_PER_CLAIM):
        if stopped():
            break
        progress = index_batch(project, batch_size=INDEX_BATCH_SIZE)
        processed += progress.processed
        complete = progress.complete
        if complete:
            break
    return processed, complete


def enqueue_search_index(
    project: Project, queue: JobQueue, payload: dict
) -> int | None:
    """Schedule pending durable work; an enqueue failure cannot lose that work."""
    marker = index_work_marker(project)
    if marker is None:
        return None
    return queue.enqueue(
        SEARCH_INDEX_KIND,
        {**payload, "dedupe_key": f"search-index:{marker}"},
    )


def index_work_marker(project: Project) -> str | None:
    """Stable identity for the next durable indexing position."""
    scopes = read_dirty_scopes(project.db, limit=1)
    if scopes:
        first = scopes[0]
        return f"{first.id}:{first.scan_cursor}"
    if index_needs_work(project):
        return f"repair:{latest_revision(project.db)}"
    if reclaim_is_pending(project.path / "project.search.db"):
        return "reclaim"
    return None


def register_search_index_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
) -> HandlerRegistration:
    def handle(payload: dict, context: JobHandlerContext) -> dict:
        def stopped() -> bool:
            if context.job_id is None:
                return False
            job = queue.get(context.job_id)
            return job is None or job.status != "running"

        project_id, project_root, path = claimed_project_location(
            payload,
            workspace_root=workspace_root,
            workspace_root_storage_org_id=workspace_root_storage_org_id,
            require_storage_identity=project_opener is not None,
        )
        project = (
            Project(path)
            if project_opener is None
            else open_claimed_project(payload, path, project_opener)
        )
        processed = 0
        complete = False
        try:
            processed, complete = process_index_batches(project, stopped=stopped)
            if complete and not stopped():
                if not reclaim_search_storage(project.path / "project.search.db"):
                    raise RuntimeError("search index reclaim deferred")
            if not stopped():
                continuation = {
                    "project_id": project_id,
                    "workspace_root": str(project_root),
                }
                claimed = payload.get(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
                if isinstance(claimed, ProjectStorageKey):
                    continuation["storage_org_id"] = claimed.storage_org_id
                if isinstance(context.trusted_job_org_id, int):
                    continuation["org_id"] = context.trusted_job_org_id
                enqueue_search_index(project, queue, continuation)
            return {
                "project_id": project_id,
                "processed": processed,
                "complete": complete,
            }
        finally:
            project.close()

    return registry.add(
        SEARCH_INDEX_KIND, handle, origin="frisket.production.search_index"
    )
