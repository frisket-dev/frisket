"""Typed capability for reading native text from admitted PDF cells."""

from __future__ import annotations

from typing import Any, Protocol

from frisket.actions.media_types import OcrText
from frisket.actions.types import ColumnRef, Row


class NativePdfTextReader(Protocol):
    async def read(self, row: Row, source: ColumnRef[Any]) -> OcrText: ...


__all__ = ["NativePdfTextReader"]
