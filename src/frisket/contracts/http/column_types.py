"""Strict HTTP projections for the column-type catalog reads."""

from __future__ import annotations

from pydantic import ConfigDict, JsonValue, RootModel

from frisket.contracts.http.models import WireModel


class ColumnTypeEntry(WireModel):
    """One registry entry, with producer-owned presentation hints left open."""

    name: str
    core: bool
    plugin: str
    presentation: dict[str, JsonValue]
    has_validator: bool
    has_parser: bool
    description: str


class ColumnTypeList(RootModel[list[ColumnTypeEntry]]):
    """The catalog is a bare array on both global and project routes."""

    model_config = ConfigDict(strict=True)


__all__ = ["ColumnTypeEntry", "ColumnTypeList"]
