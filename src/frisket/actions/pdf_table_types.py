"""PDF table values and the narrow source-bound local reader."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import Field, JsonValue, RootModel, field_validator

from frisket.actions.types import ActionParams, ColumnRef, Row


class PdfTableOptions(ActionParams):
    mode: Literal["extract_table"] = "extract_table"
    table_mode: Literal["auto", "stream", "lattice"] = Field(
        default="auto",
        description="Auto lets Natural PDF choose; stream reads whitespace-aligned tables and lattice follows ruled lines.",
    )
    extract_table: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("extract_table")
    @classmethod
    def _finite_json(cls, value):
        json.dumps(value, allow_nan=False)
        return value


class PdfTableRows(RootModel[list[dict[str, JsonValue]]]):
    """A JSON list whose PDF provenance is checked against its issuing reader."""


class PdfTablesReader(Protocol):
    async def read(
        self,
        row: Row,
        source: ColumnRef[Any],
        *,
        mode: str = "extract_table",
        table_mode: str = "auto",
        extract_table: dict[str, JsonValue] | None = None,
    ) -> PdfTableRows: ...
