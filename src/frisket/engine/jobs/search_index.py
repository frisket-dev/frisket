"""Bounded keyword-index maintenance on the existing project queue."""

from __future__ import annotations

from pathlib import Path

from frisket.engine.store import Project
from frisket.engine.store.search_index_work import latest_revision, read_dirty_scopes
from frisket.project_identity import ProjectStorageKey
from frisket.retrieval.preparation import (
    AUDIO_CHANNEL,
    DEFAULT_TEXT_RECIPE,
    IMAGE_CHANNEL,
    OCR_TEXT_CHANNEL,
    PDF_PAGE_CHANNEL,
    TRANSCRIPT_CHANNEL,
    VIDEO_CHANNEL,
    PreparationEncoder,
    PreparationPolicy,
    preparation_work_marker,
    process_blob_image_preparation_batch,
    process_evidence_preparation_batch,
    process_text_preparation_batch,
    process_timed_media_preparation_batch,
    read_preparation_policy,
)
from frisket.search_index import analyze_retrieval_store, index_batch, index_needs_work

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
INDEX_BATCH_MAX_BYTES = 4 * 1024 * 1024
INDEX_BYTES_PER_CLAIM = INDEX_BATCH_MAX_BYTES * BATCHES_PER_CLAIM


def process_index_batches(
    project: Project,
    *,
    stopped=lambda: False,
    embedding_encoder: PreparationEncoder | None = None,
    preparation_policy: PreparationPolicy | None = None,
) -> tuple[int, bool]:
    """Process one fair worker quantum of lexical and local preparation work."""
    processed = processed_bytes = 0
    complete = False
    preparation_started = False
    policy = (
        read_preparation_policy(project)
        if preparation_policy is None
        else preparation_policy
    )
    for _ in range(BATCHES_PER_CLAIM):
        if stopped():
            break
        progress = index_batch(
            project,
            batch_size=INDEX_BATCH_SIZE,
            max_bytes=INDEX_BATCH_MAX_BYTES,
            semantic_available=(
                embedding_encoder is not None and embedding_encoder.available()
            ),
            preparation_policy=policy,
        )
        processed += progress.processed
        processed_bytes += progress.processed_bytes
        complete = progress.complete
        if complete and not stopped() and embedding_encoder is not None:
            preparation_started = preparation_work_marker(project.path) is not None
            preparation_steps = (
                lambda: process_text_preparation_batch(
                    project,
                    encoder=embedding_encoder,
                    recipe=(
                        policy.text_recipe
                        if policy.text_recipe is not None
                        else DEFAULT_TEXT_RECIPE
                    ),
                    cancelled=stopped,
                ),
                lambda: process_evidence_preparation_batch(
                    project,
                    channel=TRANSCRIPT_CHANNEL,
                    encoder=embedding_encoder,
                    cancelled=stopped,
                    policy=policy,
                ),
                lambda: process_evidence_preparation_batch(
                    project,
                    channel=OCR_TEXT_CHANNEL,
                    encoder=embedding_encoder,
                    cancelled=stopped,
                    policy=policy,
                ),
                lambda: process_blob_image_preparation_batch(
                    project,
                    channel=IMAGE_CHANNEL,
                    encoder=embedding_encoder,
                    cancelled=stopped,
                    policy=policy,
                ),
                lambda: process_blob_image_preparation_batch(
                    project,
                    channel=PDF_PAGE_CHANNEL,
                    encoder=embedding_encoder,
                    cancelled=stopped,
                    policy=policy,
                ),
                lambda: process_timed_media_preparation_batch(
                    project,
                    channel=VIDEO_CHANNEL,
                    encoder=embedding_encoder,
                    cancelled=stopped,
                    policy=policy,
                ),
                lambda: process_timed_media_preparation_batch(
                    project,
                    channel=AUDIO_CHANNEL,
                    encoder=embedding_encoder,
                    cancelled=stopped,
                    policy=policy,
                ),
            )
            preparation_complete = True
            for prepare in preparation_steps:
                if stopped():
                    preparation_complete = False
                    break
                step = prepare()
                processed += step.processed_parts
                preparation_complete = preparation_complete and step.complete
            complete = preparation_complete and not stopped()
            # One pass gives each channel one bounded batch. Further work gets
            # another durable claim instead of letting one project monopolize
            # the worker after its lexical scopes have drained.
            break
        if complete or processed_bytes >= INDEX_BYTES_PER_CLAIM:
            break
    if complete and not stopped() and (processed > 0 or preparation_started):
        analyze_retrieval_store(project)
    return processed, complete


def enqueue_search_index(
    project: Project,
    queue: JobQueue,
    payload: dict,
    *,
    include_preparation: bool = True,
) -> int | None:
    """Schedule pending durable work; an enqueue failure cannot lose that work."""
    marker = index_work_marker(project, include_preparation=include_preparation)
    if marker is None:
        return None
    return queue.enqueue(
        SEARCH_INDEX_KIND,
        {**payload, "dedupe_key": f"search-index:{marker}"},
    )


def index_work_marker(
    project: Project, *, include_preparation: bool = True
) -> str | None:
    """Stable identity for the next durable indexing position."""
    scopes = read_dirty_scopes(project.db, limit=1)
    if scopes:
        first = scopes[0]
        return f"{first.id}:{first.scan_cursor}"
    if index_needs_work(project):
        return f"repair:{latest_revision(project.db)}"
    if include_preparation and (marker := preparation_work_marker(project.path)):
        return f"prepare:{marker}"
    return None


def register_search_index_handler(
    registry: HandlerRegistry,
    *,
    workspace_root: str | Path,
    queue: JobQueue,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener | None = None,
    embedding_encoder: PreparationEncoder | None = None,
    preparation_policy: PreparationPolicy | None = None,
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
            processed, complete = process_index_batches(
                project,
                stopped=stopped,
                embedding_encoder=embedding_encoder,
                preparation_policy=preparation_policy,
            )
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
                if (
                    enqueue_search_index(
                        project,
                        queue,
                        continuation,
                        include_preparation=embedding_encoder is not None,
                    )
                    is not None
                ):
                    complete = False
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
