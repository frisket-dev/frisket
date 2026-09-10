"""Typed fields extracted with selected project-local MCP tools."""

from __future__ import annotations

from pydantic import Field, StrictStr, field_validator

from frisket.actions.core import (
    ActionCategory,
    _validate_model_rows_source,
    action,
    map_rows,
)
from frisket.actions.extraction_types import ExtractField, extraction_output_fields
from frisket.actions.mcp_types import McpExtractor, McpServers
from frisket.actions.model_rows import RichSource
from frisket.actions.row_research import source_values
from frisket.actions.types import ActionParams, DynamicOutput, ModelRef, Row, RowResult


class McpExtractParams(ActionParams):
    source: RichSource
    model: ModelRef
    instruction: StrictStr = ""
    context: StrictStr = ""
    fields: list[ExtractField] = Field(min_length=1, max_length=64)
    include_confidence: bool = False
    mcp_server_ids: McpServers

    @field_validator("source")
    @classmethod
    def _source(cls, value):
        return _validate_model_rows_source(value)

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, value):
        return value.strip()


def output_fields(params: McpExtractParams):
    return extraction_output_fields(
        params.fields, include_confidence=params.include_confidence
    )


async def extract(
    params: McpExtractParams, row: Row, extractor: McpExtractor
) -> RowResult[DynamicOutput]:
    instruction = params.instruction or "Extract the requested fields."
    if params.context:
        instruction = f"Dataset context: {params.context}\n\n{instruction}"
    return RowResult(
        output=await extractor.extract(
            row, context=source_values(row, params.source), instruction=instruction
        )
    )


MCP_EXTRACT = action(
    name="mcp_extract",
    title="Tool-assisted Extract",
    description="Extract typed fields per row using selected project-local MCP tools.",
    category=ActionCategory.TEXT,
    run=map_rows(extract, dynamic_outputs=output_fields),
    examples=(
        McpExtractParams(
            source=["company"],
            model="anthropic/claude-haiku-4-5",
            fields=[ExtractField(name="company_name", type="text")],
            mcp_server_ids=McpServers(["local-crm"]),
        ),
    ),
)
