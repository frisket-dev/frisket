"""Authored view of one host-admitted file inventory page."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from frisket.actions.types import StagedFile


@dataclass(frozen=True, slots=True)
class InventoryFile:
    """One occurrence whose canonical bytes were admitted by the host."""

    filename: str
    file: StagedFile

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str) or not self.filename:
            raise ValueError("inventory file requires a filename")
        if not isinstance(self.file, StagedFile):
            raise ValueError("inventory file requires a staged file")


class FileInventoryReader(Protocol):
    """Read the host-bounded page for one opaque admitted inventory ref."""

    def files(self, ref: str) -> Iterable[InventoryFile]:
        """Yield the admitted occurrences in their durable inventory order."""


__all__ = ["FileInventoryReader", "InventoryFile"]
