"""Strict HTTP response contracts for project-source read routes."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, JsonValue, RootModel

from frisket.contracts.http.models import WireModel


class ProjectSource(WireModel):
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


class ProjectSourceRun(WireModel):
    id: int
    source_id: int
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


class ProjectSourceRunsPage(WireModel):
    schema_version: Literal["frisket.source_runs_page.v1"]
    order: Literal["desc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None
    latest_run: ProjectSourceRun | None
    latest_run_loaded: bool


class ProjectSourceDetail(ProjectSource):
    runs: list[ProjectSourceRun]
    runs_page: ProjectSourceRunsPage


class ProjectSourceList(RootModel[list[ProjectSource]]):
    model_config = ConfigDict(strict=True)


class ProjectSourceHealthSource(WireModel):
    id: int
    name: str
    kind: str
    url: str | None
    enabled: bool
    schedule: str | None
    sheet_id: int | None
    created_at: str
    redactions: list[str]


class ProjectSourceHealthSummary(WireModel):
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


class ProjectSourceHealthRunsPage(WireModel):
    schema_version: Literal["frisket.source_runs_page.v1"]
    order: Literal["desc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None


class ProjectSourceHealthRun(WireModel):
    id: int
    source_id: int
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


class ProjectSourceHealthJobRefs(WireModel):
    source_id: int
    source_run_id: int | None = None
    sheet_id: int | None = None


class ProjectSourceHealthJob(WireModel):
    job_id: int
    kind: str
    status: str
    attempts: int
    max_attempts: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    refs: ProjectSourceHealthJobRefs
    result_summary: dict[str, JsonValue]
    error_summary: str | None
    stalled: bool


class ProjectSourceHealthCosts(WireModel):
    recent_actual_micro: int
    recent_estimated_micro: int
    basis: str


class ProjectSourceHealthSparseRunNotice(WireModel):
    code: Literal["sparse_source_run_metadata"]
    message: str
    run_ids: list[int]


class ProjectSourceHealthUnsupportedScheduleNotice(WireModel):
    code: Literal["unsupported_source_schedule"]
    message: str


class ProjectSourceHealth(WireModel):
    schema_version: Literal["frisket.source_health.v1"]
    source: ProjectSourceHealthSource
    summary: ProjectSourceHealthSummary
    runs_page: ProjectSourceHealthRunsPage
    runs: list[ProjectSourceHealthRun]
    downstream_jobs: list[ProjectSourceHealthJob]
    costs: ProjectSourceHealthCosts
    alerts: list[
        ProjectSourceHealthSparseRunNotice
        | ProjectSourceHealthUnsupportedScheduleNotice
    ]
    warnings: list[
        ProjectSourceHealthSparseRunNotice
        | ProjectSourceHealthUnsupportedScheduleNotice
    ]


__all__ = [
    "ProjectSource",
    "ProjectSourceDetail",
    "ProjectSourceHealth",
    "ProjectSourceHealthCosts",
    "ProjectSourceHealthJob",
    "ProjectSourceHealthJobRefs",
    "ProjectSourceHealthRun",
    "ProjectSourceHealthRunsPage",
    "ProjectSourceHealthSource",
    "ProjectSourceHealthSparseRunNotice",
    "ProjectSourceHealthSummary",
    "ProjectSourceHealthUnsupportedScheduleNotice",
    "ProjectSourceList",
    "ProjectSourceRun",
    "ProjectSourceRunsPage",
]
