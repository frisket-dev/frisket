"""Strict HTTP response contracts for project-feed read routes."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, JsonValue, RootModel

from frisket.contracts.http.models import WireModel


class ProjectFeed(WireModel):
    id: int
    name: str
    kind: str
    url: str | None
    config: JsonValue
    sheet_id: int | None
    schedule: str | None
    enabled: bool
    cursor: str | None
    last_checked_at: str | None
    last_status: str | None
    new_rows_total: int
    created_at: str


class ProjectFeedCheck(WireModel):
    id: int
    feed_id: int
    op_id: int | None
    receipt_id: str | None
    status: str
    new_rows: int
    skipped_rows: int
    changed_rows: int
    revisions: int
    error: str | None
    cursor_before: str | None
    cursor_after: str | None
    duration_ms: int | None
    warning_count: int
    cost_micro: int
    summary_json: str
    started_at: str
    finished_at: str | None


class ProjectFeedChecksPage(WireModel):
    schema_version: Literal["frisket.feed_checks_page.v1"]
    order: Literal["desc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None
    latest_run: ProjectFeedCheck | None
    latest_run_loaded: bool


class ProjectFeedDetail(ProjectFeed):
    runs: list[ProjectFeedCheck]
    runs_page: ProjectFeedChecksPage


class ProjectFeedList(RootModel[list[ProjectFeed]]):
    model_config = ConfigDict(strict=True)


class ProjectFeedHealthFeed(WireModel):
    id: int
    name: str
    kind: str
    url: str | None
    enabled: bool
    schedule: str | None
    sheet_id: int | None
    created_at: str
    redactions: list[str]


class ProjectFeedHealthSummary(WireModel):
    status: Literal["disabled", "never_run", "failing", "stale", "healthy"]
    last_success_at: str | None
    last_failure_at: str | None
    consecutive_failures: int
    new_rows_total: int
    new_rows_recent: int
    changed_rows_recent: int
    skipped_rows_recent: int
    revisions_recent: int
    recent_run_count: int
    last_cursor_summary: Literal[
        "stored",
        "run_cursor_available",
        "run_cursor_before_only",
        "not_recorded",
    ]


class ProjectFeedHealthChecksPage(WireModel):
    schema_version: Literal["frisket.feed_checks_page.v1"]
    order: Literal["desc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None


class ProjectFeedHealthCheck(WireModel):
    id: int
    feed_id: int
    status: str
    started_at: str
    finished_at: str | None
    receipt_id: str | None
    op_id: int | None
    new_rows: int
    skipped_rows: int
    changed_rows: int
    revisions: int
    duration_ms: int | None
    warning_count: int
    cost_micro: int
    error_summary: str | None
    cursor_before_present: bool
    cursor_after_present: bool
    summary_present: bool


class ProjectFeedHealthJobRefs(WireModel):
    feed_id: int
    feed_check_id: int | None = None
    sheet_id: int | None = None


class ProjectFeedHealthJob(WireModel):
    job_id: int
    kind: str
    status: str
    attempts: int
    max_attempts: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    refs: ProjectFeedHealthJobRefs
    result_summary: dict[str, JsonValue]
    error_summary: str | None
    stalled: bool


class ProjectFeedHealthCosts(WireModel):
    recent_actual_micro: int
    recent_estimated_micro: int
    basis: str


class ProjectFeedHealthSparseCheckNotice(WireModel):
    code: Literal["sparse_feed_check_metadata"]
    message: str
    run_ids: list[int]


class ProjectFeedHealthUnsupportedScheduleNotice(WireModel):
    code: Literal["unsupported_feed_schedule"]
    message: str


class ProjectFeedHealth(WireModel):
    schema_version: Literal["frisket.feed_health.v1"]
    feed: ProjectFeedHealthFeed
    summary: ProjectFeedHealthSummary
    runs_page: ProjectFeedHealthChecksPage
    runs: list[ProjectFeedHealthCheck]
    downstream_jobs: list[ProjectFeedHealthJob]
    costs: ProjectFeedHealthCosts
    alerts: list[
        ProjectFeedHealthSparseCheckNotice | ProjectFeedHealthUnsupportedScheduleNotice
    ]
    warnings: list[
        ProjectFeedHealthSparseCheckNotice | ProjectFeedHealthUnsupportedScheduleNotice
    ]


__all__ = [
    "ProjectFeed",
    "ProjectFeedCheck",
    "ProjectFeedChecksPage",
    "ProjectFeedDetail",
    "ProjectFeedHealth",
    "ProjectFeedHealthCheck",
    "ProjectFeedHealthChecksPage",
    "ProjectFeedHealthCosts",
    "ProjectFeedHealthFeed",
    "ProjectFeedHealthJob",
    "ProjectFeedHealthJobRefs",
    "ProjectFeedHealthSparseCheckNotice",
    "ProjectFeedHealthSummary",
    "ProjectFeedHealthUnsupportedScheduleNotice",
    "ProjectFeedList",
]
