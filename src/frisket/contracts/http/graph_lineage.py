"""Stable response contracts for the graph and lineage browser reads."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, JsonValue

from frisket.contracts.http.models import WireModel


class _OpenResponseModel(WireModel):
    """Typed product core with lossless additive JSON fields."""

    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class ProjectLineageNode(_OpenResponseModel):
    id: str
    kind: Literal["source", "sheet", "ai_column"]
    name: str


class ProjectLineageEdge(_OpenResponseModel):
    from_: str = Field(alias="from")
    to: str
    kind: Literal["source", "derive", "ai_column"]
    stale: bool


class ProjectLineageResponse(_OpenResponseModel):
    """The stable Monitor Lineage DAG with additive node metadata."""

    project_id: str
    op_cursor: int
    nodes: list[ProjectLineageNode]
    edges: list[ProjectLineageEdge]


class SheetGraphRowRef(_OpenResponseModel):
    sheet_id: int
    row_id: int


class SheetGraphNode(_OpenResponseModel):
    id: str
    label: str
    sheet_id: int
    row_id: int
    row_ref: SheetGraphRowRef
    degree: int


class SheetGraphEdge(_OpenResponseModel):
    id: str
    source: str
    target: str
    direction: Literal["directed", "undirected"]
    label: str
    sheet_id: int
    row_id: int
    row_ref: SheetGraphRowRef


class SheetGraphLimits(_OpenResponseModel):
    nodes: int
    edges: int
    returned_nodes: int
    returned_edges: int


class SheetGraphDiagnostic(_OpenResponseModel):
    code: str
    message: str


class SheetGraphResponse(_OpenResponseModel):
    """The stable sheet-graph envelope with additive diagnostics."""

    schema_version: Literal["frisket.sheet_graph.v1"]
    sheet_id: int
    materialized_kind: Literal["edge", "join"] | None
    direction: Literal["directed", "undirected"]
    nodes: list[SheetGraphNode]
    edges: list[SheetGraphEdge]
    truncated: bool
    limits: SheetGraphLimits
    diagnostics: list[SheetGraphDiagnostic]


__all__ = ["ProjectLineageResponse", "SheetGraphResponse"]
