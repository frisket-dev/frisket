"""Public reader signatures for immutable staged table sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from types import TracebackType
from typing import Iterator, Self

from .models import SourceColumn, TableReadSettings, TableRow, TableSourceInfo


class TableReadError(ValueError):
    """The immutable source cannot be read with the selected settings."""


class TableRecordCountRequired(TableReadError):
    """A footer cannot be excluded safely until the logical count is known."""


class TableReader(ABC):
    """One open source claim. Iteration is single-pass and closes explicitly."""

    @property
    @abstractmethod
    def columns(self) -> tuple[SourceColumn, ...]: ...

    @abstractmethod
    def __iter__(self) -> Iterator[TableRow]: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def inspect_source(path: Path) -> TableSourceInfo:
    """Inspect a bounded prefix or workbook metadata without retaining rows."""

    raise NotImplementedError


def count_records(path: Path, settings: TableReadSettings) -> int:
    """Count logical parser records with the same parser used for iteration."""

    raise NotImplementedError


def open_table(
    path: Path,
    settings: TableReadSettings,
    *,
    total_record_count: int | None = None,
) -> TableReader:
    """Open one single-pass configured reader over an immutable source path."""

    raise NotImplementedError
