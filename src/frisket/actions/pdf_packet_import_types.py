"""Host-admitted input for the internal PDF-packet import producer."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol


@dataclass(frozen=True, slots=True)
class PacketOcrPage:
    page: int
    text: str
    blocks: tuple[dict[str, Any], ...]
    engine: str
    token_granularity: str


@dataclass(frozen=True, slots=True)
class PacketSplitChild:
    index: int
    filename: str
    page_start: int
    page_end: int
    ocr_pages: tuple[PacketOcrPage, ...] = ()


@dataclass(frozen=True, slots=True)
class PacketSplitManifest:
    filename: str
    page_count: int
    children: tuple[PacketSplitChild, ...]


class PacketSplitReader(Protocol):
    """Resolve one opaque, process-local packet preparation handle."""

    def manifest(self, ref: str) -> PacketSplitManifest: ...

    def open_source(self, ref: str) -> AbstractContextManager[BinaryIO]: ...

    def open_child(
        self, ref: str, index: int
    ) -> AbstractContextManager[BinaryIO]: ...


__all__ = [
    "PacketOcrPage",
    "PacketSplitChild",
    "PacketSplitManifest",
    "PacketSplitReader",
]
