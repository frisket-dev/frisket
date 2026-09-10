"""An admitted, bounded web researcher and its plain answer."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from frisket.actions.types import Row


class ResearchAnswer(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list)
    unverified_memory: bool = False


class Researcher(Protocol):
    async def answer(
        self, row: Row, *, goal: str, context: dict[str, Any]
    ) -> ResearchAnswer: ...


class SearchResult(BaseModel):
    title: str | None
    url: str | None
    snippet: str | None


class WebSearcher(Protocol):
    async def search(self, query: str, *, max_results: int) -> list[SearchResult]: ...
