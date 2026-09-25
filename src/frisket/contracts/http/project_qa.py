"""Project Ask: saved scope, durable turns, and bounded history pages."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from frisket.contracts.http.models import WireModel


class AskSheetSource(WireModel):
    kind: Literal["sheet"]
    sheet_id: int = Field(gt=0)


class AskRowsSource(WireModel):
    kind: Literal["rows"]
    sheet_id: int = Field(gt=0)
    row_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_rows(self) -> AskRowsSource:
        if len(set(self.row_ids)) != len(self.row_ids):
            raise ValueError("Choose each row only once.")
        return self


class AskFileSource(WireModel):
    kind: Literal["file"]
    sheet_id: int = Field(gt=0)
    row_id: int = Field(gt=0)
    column_id: int = Field(gt=0)


AskSource = Annotated[
    AskSheetSource | AskRowsSource | AskFileSource, Field(discriminator="kind")
]


class AskScope(WireModel):
    kind: Literal["project", "sources"]
    sources: list[AskSource] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def valid_sources(self) -> AskScope:
        if self.kind == "project" and self.sources is not None:
            raise ValueError("Project scope does not have a source selection.")
        if self.kind == "sources" and not self.sources:
            raise ValueError("Choose at least one source.")
        if sum(len(s.row_ids) for s in self.sources or [] if s.kind == "rows") > 1000:
            raise ValueError("Choose up to 1,000 rows, or use the whole sheet.")
        return self


class AskOptions(WireModel):
    scope: AskScope
    model: str | None = Field(default=None, min_length=1, max_length=200)
    web: bool = False
    suggest_actions: bool = True


class AskThreadCreate(AskOptions):
    title: str = Field(default="New conversation", min_length=1, max_length=200)


class AskThreadUpdate(WireModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    scope: AskScope | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    web: bool | None = None
    suggest_actions: bool | None = None

    @field_validator("title", "model")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Enter a non-empty value.")
        return value


class AskTurnRequest(AskOptions):
    request_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def nonblank_question(self) -> AskTurnRequest:
        if not self.question.strip():
            raise ValueError("Enter a question.")
        return self


class AskThread(AskOptions):
    id: str
    title: str
    revision: int
    created_by: str | None
    created_at: str
    updated_at: str


AskTurnStatus = Literal[
    "running", "stopping", "completed", "stopped", "failed", "interrupted"
]


class AskTurn(AskOptions):
    id: str
    thread_id: str
    request_id: str
    question: str
    status: AskTurnStatus
    submitted_by: str | None
    started_at: str
    finished_at: str | None
    usage: dict[str, JsonValue] | None
    cost_actual: float | None
    error_summary: str | None


class AskCellTarget(WireModel):
    kind: Literal["cell"]
    sheet_id: int
    row_id: int
    column_id: int


class AskEvidenceTarget(WireModel):
    kind: Literal["evidence"]
    sheet_id: int
    row_id: int
    column_id: int
    evidence_link_id: str
    artifact_id: str
    span_id: str


class AskQueryTarget(WireModel):
    kind: Literal["query"]
    sheet_id: int
    row_ids: list[int] | None = None
    filter: dict[str, JsonValue]
    sort: list[dict[str, JsonValue]] | None = None
    total: int


class AskWebTarget(WireModel):
    kind: Literal["web"]
    url: str
    retrieved_at: str
    fetched: bool


AskCitationTarget = Annotated[
    AskCellTarget | AskQueryTarget | AskEvidenceTarget | AskWebTarget,
    Field(discriminator="kind"),
]


class AskCitation(WireModel):
    id: str
    label: str
    source_kind: str
    excerpt: str | None
    status: Literal["current", "changed", "unverified", "unavailable"]
    message: str | None
    target: AskCitationTarget | None


class AskEvent(WireModel):
    citations: list[AskCitation] = Field(default_factory=list)
    thread_id: str
    turn_id: str
    seq: int
    kind: Literal[
        "question",
        "assistant",
        "tool_started",
        "tool_completed",
        "answer",
        "result_suggestion",
        "action_proposal",
        "usage",
        "status",
    ]
    payload: dict[str, JsonValue]
    created_at: str


class AskEventsQuery(WireModel):
    after: int = Field(default=0, ge=0)
    before: int | None = Field(default=None, gt=0)
    limit: int = Field(default=100, ge=1, le=200)


class AskEventsPage(WireModel):
    events: list[AskEvent]
    cursor: int
    has_more: bool
    active_turn: AskTurn | None


class AskThreadDetail(WireModel):
    thread: AskThread
    active_turn: AskTurn | None
    history: AskEventsPage


class AskReport(WireModel):
    markdown: str
