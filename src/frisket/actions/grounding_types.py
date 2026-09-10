"""Unverified evidence claims returned by actions; the host resolves their source."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EvidenceBox(BaseModel):
    """A claimed rectangle; coordinate normalization and verification are host work."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    x0: float
    y0: float
    x1: float
    y1: float
    space: str = "page_normalized"
    page_width: float | None = None
    page_height: float | None = None


class EvidenceClaim(BaseModel):
    """A citation hint, never permission to choose a stored source or evidence ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_index: int | None = Field(default=None, ge=0)
    segment_indices: tuple[int, ...] = ()
    page: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    bbox: EvidenceBox | None = None
    quote: str | None = None
    snippet: str | None = None
    grounding_method: str | None = None
