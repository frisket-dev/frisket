"""HTTP contracts for the alert browser routes.

An Alert is a saved condition on a View; selection belongs to that View.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, JsonValue, RootModel, model_validator

from frisket.contracts.http.models import WireModel
from frisket.contracts.actions.schemas._validators import nonblank

# The schema_version literals below are the ones the producers emit
# (server/services/alerts.py and
# frisket.features.watchlists.events.WATCH_RUN_EVENT_SCHEMA_VERSION). Restated
# here because the contracts package stays out of the service/runtime layers;
# the backend contract test pins the events literal against the producer's own
# constant.


class _ClosedRequest(WireModel):
    """The greenfield Alert mutation boundary is closed and strict."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class WatchCreateRequest(_ClosedRequest):
    name: Annotated[str, nonblank("name is required")]
    view_id: int
    detection_policy: dict[str, JsonValue] | None = None
    enabled: bool = True
    schedule: str | None = "@hourly"


class WatchPatchRequest(_ClosedRequest):
    name: str | None = None
    enabled: bool | None = None
    schedule: str | None = None
    detection_policy: dict[str, JsonValue] | None = None

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
        if (
            "detection_policy" in self.model_fields_set
            and self.detection_policy is None
        ):
            raise ValueError("detection_policy must be an object")
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
    view_id: int
    sheet_id: int | None
    detection_policy: dict[str, JsonValue]
    enabled: bool
    schedule: str | None
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
    schema_version: Literal["frisket.watch_run.v1"]
    watch: Watch
    run: WatchRun
    hits: list[WatchHit]


class WatchRunsPage(WireModel):
    schema_version: Literal["frisket.watch_runs_page.v1"]
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
    schema_version: Literal["frisket.watch_run_events_page.v1"]
    order: Literal["asc"]
    offset: int
    limit: int
    total: int
    has_more: bool
    next_offset: int | None
    events: list[WatchRunEvent]


__all__ = [
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
