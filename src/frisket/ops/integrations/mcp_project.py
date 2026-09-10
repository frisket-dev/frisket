"""Run-scoped bridge from saved project MCP settings to stdio sessions.

The settings service owns CRUD and the transport module owns MCP framing.  This
small adapter owns the only runtime question between them: select the current
saved definitions as an execution scope opens, resolve launch-only bindings,
and present their frozen tool inventory as one routed collection.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from mcp.client.stdio import get_default_environment

from frisket.contracts.http.project_mcp import McpServer
from frisket.ops.base import RecipeInvocationHalt
from frisket.ops.integrations.mcp_stdio import McpStdioClient, McpStdioError, McpTool
from frisket.project_settings import read_project_mcp_servers
from frisket.redaction import redact_value


def project_mcp_launch_config(
    project: Any, server: dict[str, Any]
) -> tuple[Any, tuple[str, ...]]:
    """Resolve one saved launch definition immediately before it is opened.

    The returned config retains plaintext only for the live child/client
    lifetime so transport errors can redact it.  Neither the config nor its
    secret values are written to a run, trace, or diagnostic.
    """
    from frisket.ops.integrations.mcp_stdio import McpStdioServerConfig

    env = get_default_environment()
    secret_values: list[str] = []
    for name, binding in server.get("env", {}).items():
        if not isinstance(name, str) or not isinstance(binding, dict):
            continue
        literal = binding.get("value")
        if isinstance(literal, str):
            env[name] = literal
            secret_values.append(literal)
            continue
        secret_name = binding.get("project_secret")
        if not isinstance(secret_name, str):
            continue
        plaintext = project.secret_plaintext(secret_name)
        if plaintext is None:
            raise ValueError("missing_project_secret")
        env[name] = plaintext
        secret_values.append(plaintext)

    return (
        McpStdioServerConfig(
            command=str(server["command"]),
            args=tuple(str(argument) for argument in server.get("args", [])),
            cwd=server.get("cwd"),
            env=env,
            secret_values=tuple(secret_values),
        ),
        tuple(secret_values),
    )


def _selected_servers(
    project: Any, selected_ids: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Return exactly the enabled persisted records selected for this run."""
    saved = read_project_mcp_servers(project)
    selected: list[dict[str, Any]] = []
    for server_id in selected_ids:
        matches = [
            server
            for server in saved
            if isinstance(server, dict) and server.get("id") == server_id
        ]
        if len(matches) != 1 or not matches[0].get("enabled", False):
            raise RecipeInvocationHalt(
                "local_artifact_unavailable",
                "A selected local MCP server is missing or disabled.",
            )
        try:
            # Validate direct/corrupt persisted records before consulting a
            # command or decrypting a binding.  Normal settings writes already
            # validate; this preserves the same fail-closed runtime boundary.
            McpServer.model_validate(matches[0])
        except ValueError as exc:
            raise RecipeInvocationHalt(
                "local_artifact_unavailable",
                "A selected local MCP server configuration is unavailable.",
            ) from exc
        selected.append(matches[0])
    return selected


class ProjectMcpSessionCollection:
    """One run's frozen selected sessions, inventory, and tool routing."""

    def __init__(self, project: Any, selected_ids: tuple[str, ...]) -> None:
        self._project = project
        self._selected_ids = selected_ids
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self._secret_values: dict[str, tuple[str, ...]] = {}
        self._tools: list[dict[str, Any]] = []

    async def __aenter__(self) -> "ProjectMcpSessionCollection":
        selected = _selected_servers(self._project, self._selected_ids)
        try:
            for server in selected:
                server_id = str(server["id"])
                try:
                    config, secret_values = project_mcp_launch_config(
                        self._project, server
                    )
                    session = await self._stack.enter_async_context(
                        McpStdioClient(config).session()
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A selected server is independently unavailable.  A
                    # surviving server still provides a useful, bounded tool
                    # surface for this run.
                    continue
                server_tools = _server_tools(server_id, session.tools)
                if _contains_known_value(server_tools, secret_values):
                    raise RecipeInvocationHalt(
                        "local_artifact_unavailable",
                        "A selected local MCP server exposed unsafe tool metadata.",
                    )
                self._sessions[server_id] = session
                self._secret_values[server_id] = secret_values
                self._tools.extend(server_tools)
            if not self._tools:
                raise RecipeInvocationHalt(
                    "local_artifact_unavailable",
                    "Selected local MCP servers exposed no usable tools.",
                )
            return self
        except BaseException:
            await self._close()
            raise

    async def __aexit__(self, *_exc: Any) -> None:
        await self._close()

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [dict(tool) for tool in self._tools]

    async def discover_tools(self) -> list[dict[str, Any]]:
        return self.tools

    async def call_tool(
        self, server_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session = self._sessions.get(server_id)
        if session is None:
            raise ValueError("selected_mcp_server_unavailable")
        try:
            result = await session.call_tool(name, arguments)
        except McpStdioError as exc:
            if exc.may_have_dispatched:
                raise RecipeInvocationHalt(
                    "external_effect_reconciliation_required",
                    "A selected local MCP tool may have run without a returned response.",
                ) from None
            raise
        return redact_value(
            {
                "isError": result.is_error,
                "content": list(result.content),
                "structuredContent": result.structured_content,
                "truncated": result.truncated,
            },
            secret_values=self._secret_values.get(server_id, ()),
        )

    async def _close(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()
        self._secret_values.clear()
        self._tools.clear()


def _server_tools(server_id: str, tools: tuple[McpTool, ...]) -> list[dict[str, Any]]:
    return [
        {
            "server_id": server_id,
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": dict(tool.input_schema),
        }
        for tool in tools
    ]


def _contains_known_value(value: Any, known_values: tuple[str, ...]) -> bool:
    """Fail closed before MCP-controlled inventory becomes model context."""
    needles = tuple(item for item in known_values if item)
    if not needles:
        return False
    if isinstance(value, str):
        return any(needle in value for needle in needles)
    if isinstance(value, dict):
        return any(
            _contains_known_value(key, needles) or _contains_known_value(item, needles)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_known_value(item, needles) for item in value)
    return False


__all__ = ["ProjectMcpSessionCollection", "project_mcp_launch_config"]
