"""HTTP contracts for the watchlist browser routes.

These DTOs freeze the executable wire truth the watch service/store producers
emit. A Watch owns one normalized query captured at creation time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, JsonValue, RootModel, field_validator, model_validator

from frisket.contracts.http.models import WireModel

# The page/result literals the producers emit (server/services/watches.py and
# frisket.features.watchlists.events.WATCH_RUN_EVENT_SCHEMA_VERSION). Restated
# here because the contracts package stays out of the service/runtime layers;
# the backend contract test pins the events literal against the producer's own
# constant.
WATCH_RUN_RESULT_SCHEMA_VERSION = "frisket.watch_run.v1"
WATCH_RUNS_PAGE_SCHEMA_VERSION = "frisket.watch_runs_page.v1"
WATCH_RUN_EVENTS_PAGE_SCHEMA_VERSION = "frisket.watch_run_events_page.v1"


class _ClosedRequest(WireModel):
    """The greenfield Watch mutation boundary is closed and strict."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class WatchCreateRequest(_ClosedRequest):
    name: str
    query: dict[str, JsonValue]
    scope: dict[str, JsonValue] | None = None
    detection_policy: dict[str, JsonValue] | None = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name_is_nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name is required")
        return value


class WatchPatchRequest(_ClosedRequest):
    name: str | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _is_nonempty_and_nonnull(self) -> "WatchPatchRequest":
        if not self.model_fields_set:
            raise ValueError("at least one watch field is required")
        if "name" in self.model_fields_set:
            if self.name is None or not (name := self.name.strip()):
                raise ValueError("name is required")
            self.name = name
        if "enabled" in self.model_fields_set and self.enabled is None:
            raise ValueError("enabled must be a boolean")
        return self


class WatchRun(WireModel):
    id: int
    watch_id: int
    status: str
    op_cursor_before: int
    op_cursor_after: int
    matched_rows: int
    new_rows: int
    error: str | None
    error_code: str | None
    resolved_query_hash: str | None
    resolved_query: dict[str, JsonValue]
    started_at: str
    finished_at: str | None


class WatchHit(WireModel):
    run_id: int
    sheet_id: int
    row_id: int
    column_id: int | None
    rank: int
    snippet: str | None
    is_new: bool


class WatchRunWithHits(WatchRun):
    hits: list[WatchHit]
    hits_limit: int
    hits_truncated: bool


class Watch(WireModel):
    id: int
    name: str
    scope: str
    sheet_id: int | None
    query: dict[str, JsonValue]
    query_version: str | None
    query_hash: str | None
    detection_policy: dict[str, JsonValue]
    enabled: bool
    last_evaluated_op: int
    last_run_id: int | None
    last_status: str | None
    created_at: str
    updated_at: str
    latest_run: WatchRun | None


class WatchList(RootModel[list[Watch]]):
    model_config = ConfigDict(strict=True)


class WatchDelete(WireModel):
    ok: bool
    deleted: int


class WatchRunResult(WireModel):
    schema_version: Literal[WATCH_RUN_RESULT_SCHEMA_VERSION]
    watch: Watch
    run: WatchRun
    hits: list[WatchHit]


class WatchRunsPage(WireModel):
    schema_version: Literal[WATCH_RUNS_PAGE_SCHEMA_VERSION]
    order: Literal["desc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None
    hits_limit: int
    runs: list[WatchRunWithHits]


class WatchRunEvent(WireModel):
    id: int
    run_id: int
    watch_id: int
    event_kind: str
    subject_kind: str
    subject_ref: dict[str, JsonValue]
    before_json: dict[str, JsonValue] | None
    after_json: dict[str, JsonValue] | None
    delta_json: dict[str, JsonValue] | None
    severity: str
    rank: int
    snippet: str | None
    created_at: str


class WatchRunEventsPage(WireModel):
    schema_version: Literal[WATCH_RUN_EVENTS_PAGE_SCHEMA_VERSION]
    order: Literal["asc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None
    events: list[WatchRunEvent]


__all__ = [
    "WATCH_RUN_EVENTS_PAGE_SCHEMA_VERSION",
    "WATCH_RUN_RESULT_SCHEMA_VERSION",
    "WATCH_RUNS_PAGE_SCHEMA_VERSION",
    "Watch",
    "WatchCreateRequest",
    "WatchDelete",
    "WatchHit",
    "WatchList",
    "WatchPatchRequest",
    "WatchRun",
    "WatchRunEvent",
    "WatchRunEventsPage",
    "WatchRunResult",
    "WatchRunWithHits",
    "WatchRunsPage",
]
