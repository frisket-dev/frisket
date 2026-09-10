"""Strict local HTTP contracts for project-scoped stdio MCP servers."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from frisket.contracts.http.models import WireModel


MCP_SERVER_CATALOG_SCHEMA_VERSION = "frisket.project_mcp_servers.v1"

_NonEmptyText = Annotated[str, Field(min_length=1, max_length=4096)]
_EnvName = Annotated[str, Field(min_length=1, max_length=256)]


class McpLiteralEnvBinding(WireModel):
    """A deliberate, non-secret environment value supplied by the user."""

    value: str = Field(max_length=16_384)


class McpProjectSecretEnvBinding(WireModel):
    """A reference; the Project Secret plaintext is never configuration data."""

    project_secret: _NonEmptyText


McpEnvBinding = McpLiteralEnvBinding | McpProjectSecretEnvBinding


class McpServerCreateRequest(WireModel):
    name: _NonEmptyText
    command: _NonEmptyText
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str | None = Field(default=None, max_length=4096)
    env: dict[_EnvName, McpEnvBinding] = Field(default_factory=dict, max_length=128)
    enabled: bool = True


class McpServerUpdateRequest(WireModel):
    name: _NonEmptyText | None = None
    command: _NonEmptyText | None = None
    args: list[str] | None = Field(default=None, max_length=128)
    cwd: str | None = Field(default=None, max_length=4096)
    env: dict[_EnvName, McpEnvBinding] | None = Field(default=None, max_length=128)
    enabled: bool | None = None

    @model_validator(mode="after")
    def must_change_a_field(self) -> McpServerUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one MCP server field is required")
        for field_name in ("name", "command", "args", "env", "enabled"):
            if (
                field_name in self.model_fields_set
                and getattr(self, field_name) is None
            ):
                raise ValueError(f"{field_name} cannot be null")
        return self


class McpServerImportSpec(WireModel):
    """The local stdio subset of the common ``mcpServers`` config shape."""

    command: _NonEmptyText
    args: list[str] = Field(default_factory=list, max_length=128)
    cwd: str | None = Field(default=None, max_length=4096)
    # Importing a desktop config must not silently persist whatever value
    # happened to be pasted in its ``env`` map.  The caller explicitly marks
    # each import value as a literal or a Project Secret reference.
    env: dict[_EnvName, McpEnvBinding] = Field(default_factory=dict, max_length=128)


class McpServerImportRequest(WireModel):
    mcp_servers: dict[_NonEmptyText, McpServerImportSpec] = Field(
        alias="mcpServers", min_length=1, max_length=64
    )


class McpServerTest(WireModel):
    status: Literal["succeeded", "failed"]
    tested_at: str | None = Field(default=None, max_length=64)
    diagnostic: str | None = Field(default=None, max_length=500)


class McpServer(WireModel):
    id: str
    name: str
    command: str
    args: list[str]
    cwd: str | None
    env: dict[str, McpEnvBinding]
    enabled: bool
    revision: int = Field(ge=1)
    last_test: McpServerTest | None = Field(alias="lastTest")
    last_discovered_tool_count: int | None = Field(default=None, ge=0, le=512)


class McpServerCatalog(WireModel):
    schema_version: Literal["frisket.project_mcp_servers.v1"] = Field(
        alias="schemaVersion"
    )
    project_id: str = Field(alias="projectId")
    servers: list[McpServer]


class McpServerDeleteResponse(WireModel):
    ok: bool
    deleted: bool
    id: str
