"""Wire models for the browser project-search read."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ProjectSearchHit(BaseModel):
    """One ranked producer hit, retaining producer-owned extension fields."""

    model_config = ConfigDict(extra="allow", strict=True)

    sheet_id: int
    row_id: int
    column_id: int
    column_name: str
    ai_generated: bool
    snip: str
    score: float = None  # type: ignore[assignment]
    semantic: bool = None  # type: ignore[assignment]
    rerank_score: float = None  # type: ignore[assignment]


class ProjectSearchPage(BaseModel):
    """Ranked hits with explicit background-indexing status."""

    model_config = ConfigDict(strict=True)
    hits: list[ProjectSearchHit]
    indexing: bool
