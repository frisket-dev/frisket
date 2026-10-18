"""Bounded source diagnostics shared by import status and job projections."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _ImportErrorWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class ImportSourceErrorExample(_ImportErrorWireModel):
    source_ordinal: int
    line_start: int | None = None
    line_end: int | None = None
    column_name: str
    original: str


class ImportSourceErrorGroup(_ImportErrorWireModel):
    code: str
    message: str
    count: int
    examples: list[ImportSourceErrorExample] = Field(default_factory=list)


__all__ = ["ImportSourceErrorExample", "ImportSourceErrorGroup"]
