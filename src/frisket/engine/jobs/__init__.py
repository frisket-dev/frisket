"""Unified job queue + worker.

One JobQueue interface, two backends (SQLite BEGIN IMMEDIATE for the local
workspace, Postgres SKIP LOCKED for the hosted run queue), one worker loop.
The sidecar gets no queue.
"""

from .queue import (
    ACTION_RUN_KIND,
    QUEUE_DB_NAME,
    STATUSES,
    HandlerAuthorityAbandonResult,
    Job,
    JobQueue,
    PostgresJobQueue,
    SqliteJobQueue,
    WorkerHeartbeat,
    jobs_metadata,
    jobs_table,
    open_queue,
    worker_heartbeats_table,
)
from .queue_health import queue_health_payload
from .embeddings import (
    EMBEDDING_REFRESH_ENQUEUE_KIND,
    EMBEDDING_REFRESH_KIND,
    enqueue_due_scheduled_refreshes,
    enqueue_source_append_refreshes,
    find_on_source_append_indexes,
    find_scheduled_indexes,
    refresh_dedupe_key,
    register_embedding_refresh_handler,
)
from .enclosures import register_enclosure_download_handler
from .watches import (
    WATCH_EVALUATE_KIND,
    enqueue_index_watch_evaluations,
    register_watch_evaluate_handler,
)
from .ports import (
    AdmissionPort,
    CredentialPort,
    JobHandlerContext,
    OrgKeyCredentialPort,
    SettlementPort,
    TrustedJobOrg,
    TrustedJobOrgId,
    TrustedJobOrgUnavailable,
    WorkerPorts,
)
from .runs import (
    RUN_PROJECT_KIND,
    register_action_run_handler,
    register_project_run_handler,
)
from .sources import (
    SOURCE_POLL_KIND,
    enqueue_enclosure_downloads,
    enqueue_due_source_polls,
    register_source_poll_handler,
)
from .notifications_delivery import (
    NOTIFICATION_DELIVER_KIND,
    enqueue_notification_delivery,
    enqueue_notification_emit_result,
    reconcile_notification_delivery_requests,
    register_notification_handlers,
)
from .notifications_digest import (
    NOTIFICATION_DIGEST_KIND,
    enqueue_due_notification_digests,
    register_notification_digest_handlers,
)
from frisket.ops.enclosures import ENCLOSURE_DOWNLOAD_KIND
from .project_opener import (
    ProjectOpener,
    open_claimed_project,
    require_opener_storage_org_id,
)
from .worker import (
    ECHO_KIND,
    STRICT_STORAGE_HANDLER_ORIGIN,
    HandlerRegistration,
    HandlerRegistry,
    Worker,
    default_registry,
    register_hosted_handlers,
    register_production_handlers,
)

__all__ = [
    "ECHO_KIND",
    "EMBEDDING_REFRESH_ENQUEUE_KIND",
    "EMBEDDING_REFRESH_KIND",
    "ENCLOSURE_DOWNLOAD_KIND",
    "ACTION_RUN_KIND",
    "QUEUE_DB_NAME",
    "RUN_PROJECT_KIND",
    "SOURCE_POLL_KIND",
    "NOTIFICATION_DELIVER_KIND",
    "NOTIFICATION_DIGEST_KIND",
    "STATUSES",
    "AdmissionPort",
    "CredentialPort",
    "JobHandlerContext",
    "OrgKeyCredentialPort",
    "SettlementPort",
    "TrustedJobOrg",
    "TrustedJobOrgId",
    "TrustedJobOrgUnavailable",
    "WorkerPorts",
    "HandlerRegistry",
    "HandlerRegistration",
    "STRICT_STORAGE_HANDLER_ORIGIN",
    "HandlerAuthorityAbandonResult",
    "ProjectOpener",
    "open_claimed_project",
    "require_opener_storage_org_id",
    "Job",
    "JobQueue",
    "PostgresJobQueue",
    "SqliteJobQueue",
    "Worker",
    "WorkerHeartbeat",
    "default_registry",
    "register_hosted_handlers",
    "register_production_handlers",
    "jobs_metadata",
    "jobs_table",
    "open_queue",
    "queue_health_payload",
    "worker_heartbeats_table",
    "enqueue_enclosure_downloads",
    "enqueue_due_scheduled_refreshes",
    "enqueue_due_source_polls",
    "enqueue_source_append_refreshes",
    "find_scheduled_indexes",
    "find_on_source_append_indexes",
    "refresh_dedupe_key",
    "register_embedding_refresh_handler",
    "register_enclosure_download_handler",
    "register_watch_evaluate_handler",
    "enqueue_index_watch_evaluations",
    "WATCH_EVALUATE_KIND",
    "register_action_run_handler",
    "register_project_run_handler",
    "register_source_poll_handler",
    "enqueue_notification_delivery",
    "enqueue_notification_emit_result",
    "enqueue_due_notification_digests",
    "reconcile_notification_delivery_requests",
    "register_notification_handlers",
    "register_notification_digest_handlers",
]
