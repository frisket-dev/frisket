"""Match each selected source row against a whole target column."""

from typing import Any

from pydantic import Field, model_validator
from frisket.actions.core import ActionCategory, action, semantic_join

from frisket.actions.semantic_join_types import (
    DEFAULT_CONFIDENT_THRESHOLD,
    DEFAULT_MATCH_THRESHOLD,
    SemanticJoinMatch,
    SemanticMatcher,
)
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    Row,
    RowResult,
    SheetColumnRef,
)


class SemanticJoinParams(ActionParams):
    source: ColumnRef[Any]
    target: SheetColumnRef
    carry: list[ColumnRef[Any]] = Field(default_factory=list)
    match_threshold: float = Field(default=DEFAULT_MATCH_THRESHOLD, ge=0, le=1)
    confident_threshold: float = Field(default=DEFAULT_CONFIDENT_THRESHOLD, ge=0, le=1)

    @model_validator(mode="after")
    def valid_match(self):
        if self.match_threshold >= self.confident_threshold:
            raise ValueError("match_threshold must be below confident_threshold")
        names = [column.name for column in self.carry]
        if self.source.name in names or len(names) != len(set(names)):
            raise ValueError("carry columns must be distinct and exclude the source")
        return self


async def match_one(
    params: SemanticJoinParams, row: Row, matcher: SemanticMatcher
) -> RowResult[SemanticJoinMatch]:
    return RowResult(
        output=await matcher.match(
            params.source.read(row),
            target=params.target,
            match_threshold=params.match_threshold,
            confident_threshold=params.confident_threshold,
        )
    )


SEMANTIC_JOIN = action(
    name="semantic",
    title="Semantic join",
    description="Match a source column against a target column using embeddings, with a linked results sheet.",
    category=ActionCategory.CONVERT,
    run=semantic_join(match_one),
    examples=(
        SemanticJoinParams(
            source="organization",
            target=SheetColumnRef(sheet_id=2, column="name"),
        ),
    ),
)
