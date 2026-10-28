"""An admitted, bounded web researcher and its plain answer."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from frisket.actions.extraction_types import ExtractField
from frisket.actions.types import DynamicOutput, Row


class ResearchAnswer(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list)
    unverified_memory: bool = False


class SearchResult(BaseModel):
    title: str | None
    url: str | None
    snippet: str | None


class QuickSearchAnswer(BaseModel):
    answer: str | None
    search_results: list[SearchResult]


class Researcher(Protocol):
    async def answer(
        self, row: Row, *, goal: str, context: dict[str, Any]
    ) -> ResearchAnswer: ...

    async def quick_search(
        self, row: Row, *, query: str, max_results: int
    ) -> QuickSearchAnswer: ...

    async def extract(
        self,
        row: Row,
        *,
        goal: str,
        context: dict[str, Any],
        fields: list[ExtractField],
        include_confidence: bool = False,
        include_sources: bool = True,
    ) -> DynamicOutput: ...


class WebSearcher(Protocol):
    async def search(self, query: str, *, max_results: int) -> list[SearchResult]: ...
