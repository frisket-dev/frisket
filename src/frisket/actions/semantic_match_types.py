"""Completed semantic matches and their host-admitted contributor rows."""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, StrictStr, field_validator

from frisket.actions.types import ActionParams, RowSource


class SemanticJoinSource(ActionParams):
    kind: Literal["semantic_join"]
    receipt_id: StrictStr = Field(min_length=1)

    @field_validator("receipt_id")
    @classmethod
    def nonblank_receipt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("receipt_id must not be blank")
        return value


class SemanticMatchValues(BaseModel):
    source_row_id: int
    source_value: str | None
    target_row_id: int | None
    target_value: str | None
    match_score: float | None
    match_value: str | None


@dataclass(frozen=True)
class SemanticMatch:
    value: SemanticMatchValues
    source: RowSource
    target: RowSource | None


class SemanticMatchReader(Protocol):
    def read(
        self, source: SemanticJoinSource, *, include_unmatched: bool = False
    ) -> tuple[SemanticMatch, ...]: ...
