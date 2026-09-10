"""A semantic match supplied by the admitted cross-sheet matcher."""

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from frisket.actions.types import Outcome, SheetColumnRef

DEFAULT_MATCH_THRESHOLD = 0.70
DEFAULT_CONFIDENT_THRESHOLD = 0.85


class SemanticJoinMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    match_value: Outcome[str | None]
    match_score: Outcome[float | None]
    matched_row_id: Outcome[int | None]


class SemanticMatcher(Protocol):
    async def match(
        self,
        value: Any,
        *,
        target: SheetColumnRef,
        match_threshold: float = DEFAULT_MATCH_THRESHOLD,
        confident_threshold: float = DEFAULT_CONFIDENT_THRESHOLD,
    ) -> SemanticJoinMatch: ...
