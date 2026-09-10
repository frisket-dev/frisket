"""Installed Actions covering the scalar-param and source-selection shapes.

An installed plugin Action is an ordinary Action, so its parameters ARE its
typed Params model: scalar knobs and source columns are declared side by side
and the host binds them the same way it binds a builtin's. There is no
manifest-side param vocabulary to keep in sync, and no dispatch control key
(sheet_id, row_ids, output naming) can reach a handler, because those live in
the ActionRequest rather than in Params.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    Row,
    RowResult,
    Template,
)
from frisket.plugins.sdk import Plugin

# Dispatch control the host owns. A handler must never see one of these as a
# parameter; the tests read the `control` segment below to prove it.
CONTROL_PARAM_NAMES = frozenset(
    {"sheet_id", "row_ids", "output_fields", "output_name", "input_columns", "inputs"}
)


class FormatParams(ActionParams):
    text: ColumnRef[str]
    prefix: str = ""
    uppercase: bool = False
    repeat: int = 1


class FormatOutput(BaseModel):
    formatted: str | None


def _formatted(value: Any, params: FormatParams) -> str:
    text = "" if value is None else str(value)
    body = ",".join(
        [text.upper() if params.uppercase else text] * max(1, params.repeat)
    )
    leaked = sorted(CONTROL_PARAM_NAMES & set(type(params).model_fields))
    return f"{params.prefix}:{body}:control={leaked or 'none'}"


def format_text(params: FormatParams, row: Row) -> RowResult[FormatOutput]:
    return RowResult(
        output=FormatOutput(formatted=_formatted(params.text.read(row), params))
    )


class CombineParams(ActionParams):
    """Exactly two source columns, the typed form of the old manifest
    `cardinality: many` with min and max 2."""

    sources: list[ColumnRef[str]] = Field(min_length=2, max_length=2)
    separator: str = " / "


class CombineOutput(BaseModel):
    combined: str


def combine_columns(params: CombineParams, row: Row) -> RowResult[CombineOutput]:
    values = [
        "" if (v := source.read(row)) is None else str(v) for source in params.sources
    ]
    return RowResult(output=CombineOutput(combined=params.separator.join(values)))


class TemplateParams(ActionParams):
    """A template carries the columns it references, which is the typed form of
    the old `cardinality: template` input pointing at a scalar template param."""

    template: Template[str]


class TemplateOutput(BaseModel):
    template_summary: str


def template_sources(params: TemplateParams, row: Row) -> RowResult[TemplateOutput]:
    pairs = [
        f"{ref.column}={row.read(ref.column)}" for ref in params.template.references()
    ]
    return RowResult(
        output=TemplateOutput(
            template_summary=f"{params.template.text} -> {';'.join(pairs)}"
        )
    )


FORMAT_TEXT = action(
    name="format_text",
    title="Format text with params",
    description="Formats a text column using declared scalar params.",
    category=ActionCategory.TEXT,
    run=map_rows(format_text),
)

COMBINE_COLUMNS = action(
    name="combine_columns",
    title="Combine source columns",
    description="Combines multiple selected source columns into one output.",
    category=ActionCategory.TEXT,
    run=map_rows(combine_columns),
)

TEMPLATE_SOURCES = action(
    name="template_sources",
    title="Template-derived sources",
    description="Receives source columns derived from the template parameter.",
    category=ActionCategory.TEXT,
    run=map_rows(template_sources),
)

plugin = Plugin(
    id="demo.scalar_params",
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(FORMAT_TEXT, COMBINE_COLUMNS, TEMPLATE_SOURCES),
)
