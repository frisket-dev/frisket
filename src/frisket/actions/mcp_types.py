"""Selected project-local MCP servers and bounded extraction capability."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field, RootModel, model_validator

from frisket.actions.types import DynamicOutput, Row


class McpServers(RootModel[list[str]]):
    root: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def _unique_ids(self):
        if any(not value.strip() for value in self.root) or len(set(self.root)) != len(
            self.root
        ):
            raise ValueError("MCP server IDs must be unique and nonblank")
        return self


class McpExtractor(Protocol):
    async def extract(
        self, row: Row, *, context: dict[str, Any], instruction: str
    ) -> DynamicOutput: ...
