"""Row-local source media materialization through an admitted host capability."""

from typing import Literal, Protocol

from pydantic import BaseModel


class MaterializedEnclosure(BaseModel):
    row_id: int
    status: Literal["downloaded", "already_downloaded", "error"]
    error: str | None = None


class MaterializedEnclosures(BaseModel):
    rows: tuple[MaterializedEnclosure, ...]


class EnclosureMaterializer(Protocol):
    def materialize(self, *, force: bool = False) -> MaterializedEnclosures:
        """Download the admitted enclosure rows, preserving individual outcomes."""
        ...
