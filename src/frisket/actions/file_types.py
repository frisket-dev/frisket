"""Narrow actual-argument file acquisition capability."""

from typing import ClassVar, Protocol

from frisket.actions.types import ColumnRef, StagedFile


class UrlColumn(ColumnRef[str]):
    accepted_column_types: ClassVar[tuple[str, ...]] = ("link", "text")


class FileFetcher(Protocol):
    async def fetch(self, url: str) -> StagedFile: ...
