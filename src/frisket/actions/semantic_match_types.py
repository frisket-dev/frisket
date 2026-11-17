"""Completed semantic matches and their host-admitted contributor rows."""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, Field, StrictStr

from frisket.actions.types import ActionParams, RowSource
from frisket.contracts.actions.schemas._validators import nonblank


class SemanticJoinSource(ActionParams):
    kind: Literal["semantic_join"]
    receipt_id: Annotated[
        StrictStr, nonblank("receipt_id must not be blank", strip=False)
    ] = Field(min_length=1)


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
