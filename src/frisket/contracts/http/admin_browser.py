"""Strict JSON contracts for the browser-facing administration API."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from frisket.contracts.http.models import QueryWireModel, WireModel


AdminOrgRole = Literal["owner", "admin", "member"]
AdminJobStatus = Literal["queued", "running", "done", "failed", "cancelled"]
AdminCancelOutcome = Literal[
    "cancelled",
    "terminalized",
    "already_terminal",
    "cancel_pending",
    "reconciliation_required",
    "conflict",
]


class AdminBrowserDatabaseHealth(WireModel):
    ok: bool
    dialect: str | None
    latency_ms: int | None = Field(ge=0)
    error: str | None


class AdminBrowserBlobStoreHealth(WireModel):
    ok: bool
    error: str | None


class AdminBrowserQueueCounts(WireModel):
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    done: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)


class AdminBrowserQueueHealth(WireModel):
    ok: bool
    counts: AdminBrowserQueueCounts
    error: str | None


class AdminBrowserHealthV1(WireModel):
    schema_version: Literal["frisket.admin_health.v1"]
    ok: bool
    db: AdminBrowserDatabaseHealth
    blob_store: AdminBrowserBlobStoreHealth
    active_runs: int = Field(ge=0)
    queue: AdminBrowserQueueHealth


class AdminBrowserUser(WireModel):
    user_id: int = Field(gt=0)
    email: str
    name: str | None
    role: AdminOrgRole
    created_at: str | None


class AdminBrowserPendingInvite(WireModel):
    email: str
    org_id: int = Field(gt=0)
    role: AdminOrgRole
    expires_at: str | None


class AdminBrowserOrganization(WireModel):
    id: int = Field(gt=0)
    name: str
    suspended: bool
    users: list[AdminBrowserUser]
    pending_invites: list[AdminBrowserPendingInvite]


class AdminBrowserUserCapabilities(WireModel):
    assignable_roles: list[AdminOrgRole]
    invite_ttl_days: int = Field(gt=0)
    magic_link_ttl_minutes: int = Field(gt=0)


class AdminBrowserUsersV1(WireModel):
    schema_version: Literal["frisket.admin_users.v1"]
    orgs: list[AdminBrowserOrganization]
    capabilities: AdminBrowserUserCapabilities


class AdminBrowserInviteRequest(WireModel):
    org_id: int = Field(gt=0)
    email: str = Field(min_length=3, max_length=320)


class AdminBrowserInviteResult(WireModel):
    ok: bool
    sent: bool
    org_id: int = Field(gt=0)
    email: str
    role: AdminOrgRole
    expires_at: str | None


class AdminBrowserUpdateRoleRequest(WireModel):
    org_id: int = Field(gt=0)
    role: AdminOrgRole


class AdminBrowserUpdateRoleResult(WireModel):
    ok: bool
    org_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    role: AdminOrgRole
    self: bool


class AdminBrowserTargetOrgQuery(QueryWireModel):
    org_id: int = Field(gt=0)


class AdminBrowserRemoveUserResult(WireModel):
    ok: bool
    org_id: int = Field(gt=0)
    user_id: int | None = Field(default=None, gt=0)
    email: str | None
    membership_removed: bool
    invite_revoked: bool
    self: bool


class AdminBrowserRevokeInviteResult(WireModel):
    ok: bool
    org_id: int = Field(gt=0)
    email: str
    revoked: bool


class AdminBrowserJob(WireModel):
    schema_version: Literal["frisket.admin_job.v1"]
    id: int = Field(gt=0)
    kind: str
    action_kind: str | None = None
    action_name: str | None = None
    status: AdminJobStatus
    attempts: int = Field(ge=0)
    max_attempts: int = Field(gt=0)
    locked_by: str | None
    locked_at: str | None
    lease_expires_at: str | None
    available_at: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    refs: dict[str, JsonValue]
    payload_ref: dict[str, JsonValue]
    diagnostics: dict[str, JsonValue]
    result_summary: dict[str, JsonValue]
    error: str | None
    stalled: bool


class AdminBrowserJobSummary(WireModel):
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    stalled: int = Field(ge=0)
    failed: int = Field(ge=0)
    done: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    live_workers: int = Field(ge=0)
    no_live_worker: bool


class AdminBrowserWorkers(WireModel):
    live: int = Field(ge=0)
    liveness_window_seconds: int = Field(gt=0)


class AdminBrowserJobsV1(WireModel):
    schema_version: Literal["frisket.admin_jobs.v1"]
    summary: AdminBrowserJobSummary
    workers: AdminBrowserWorkers
    jobs: list[AdminBrowserJob]


class AdminBrowserCancelV1(WireModel):
    ok: bool
    outcome: AdminCancelOutcome
    job_id: int = Field(gt=0)
    retryable: bool
    detail: str | None


class AdminBrowserAuditQuery(QueryWireModel):
    org_id: int | None = Field(default=None, gt=0)
    project_id: str | None = None
    user: str | None = None
    action: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class AdminBrowserAuditEvent(WireModel):
    id: str
    source: str
    category: str
    created_at: str | None
    actor_user_id: int | None
    actor_email: str | None
    org_id: int | None
    org_name: str | None
    project_id: str | int | None
    project_name: str | None
    action: str
    detail: str
    object_type: str | None
    object_id: str | None
    route: str | None
    context: dict[str, JsonValue]


class AdminBrowserAuditOrgOption(WireModel):
    id: int = Field(gt=0)
    name: str


class AdminBrowserAuditProjectOption(WireModel):
    org_id: int = Field(gt=0)
    project_id: str
    name: str


class AdminBrowserAuditFilterOptions(WireModel):
    orgs: list[AdminBrowserAuditOrgOption]
    projects: list[AdminBrowserAuditProjectOption]
    actions: list[str]


class AdminBrowserAuditV1(WireModel):
    schema_version: Literal["frisket.admin_audit.v1"]
    events: list[AdminBrowserAuditEvent]
    filters: AdminBrowserAuditFilterOptions


class AdminBrowserErrorsQuery(QueryWireModel):
    limit: int = Field(default=50, ge=1, le=500)


class AdminBrowserErrorEvent(WireModel):
    id: str
    record_id: int | None
    kind: str
    source: str
    severity: str
    name: str | None
    message: str
    stack: str | None
    route: str | None
    org_id: int | None
    user_id: int | None
    project_id: str | None
    sheet_id: str | None
    run_id: str | None
    job_id: str | None
    trace_id: str | None
    context: dict[str, JsonValue]
    bundle: dict[str, JsonValue] | None
    at: str | None


class AdminBrowserRetentionRule(WireModel):
    days: int = Field(gt=0)
    max_rows: int = Field(gt=0)


class AdminBrowserErrorRetention(WireModel):
    client_errors: AdminBrowserRetentionRule
    diagnostic_reports: AdminBrowserRetentionRule


class AdminBrowserErrorsV1(WireModel):
    schema_version: Literal["frisket.admin_errors.v1"]
    errors: list[AdminBrowserErrorEvent]
    retention: AdminBrowserErrorRetention | None = None


__all__ = [
    "AdminBrowserAuditQuery",
    "AdminBrowserAuditV1",
    "AdminBrowserCancelV1",
    "AdminBrowserErrorsQuery",
    "AdminBrowserErrorsV1",
    "AdminBrowserHealthV1",
    "AdminBrowserInviteRequest",
    "AdminBrowserInviteResult",
    "AdminBrowserJobsV1",
    "AdminBrowserRemoveUserResult",
    "AdminBrowserRevokeInviteResult",
    "AdminBrowserTargetOrgQuery",
    "AdminBrowserUpdateRoleRequest",
    "AdminBrowserUpdateRoleResult",
    "AdminBrowserUsersV1",
]
