"""Solo project-scoped stdio MCP server configuration and test service."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from frisket.engine.store.credentials import replace_secret_consumers
from frisket.contracts.http.project_mcp import (
    MCP_SERVER_CATALOG_SCHEMA_VERSION,
    McpServer,
    McpServerCreateRequest,
    McpServerImportRequest,
    McpServerUpdateRequest,
)
from frisket.project_settings import (
    read_project_mcp_servers,
    write_project_mcp_servers,
)
from frisket.ops.integrations.mcp_project import project_mcp_launch_config
from frisket.redaction import redact_text
from frisket.server.route_errors import RouteError
from frisket.server.workspace import Workspace


_MAX_ADVISORY_TOOL_NAMES = 20
_PUBLIC_PROBE_FAILURE_CODES = frozenset(
    {
        "mcp_frame_too_large",
        "mcp_protocol_error",
        "mcp_request_failed",
        "mcp_request_timeout",
        "mcp_session_eof",
        "mcp_spawn_failed",
        "mcp_tool_count_exceeded",
        "mcp_tool_page_limit_exceeded",
    }
)


class ProjectMcpService:
    """CRUD boundary for local executable configuration owned by one project."""

    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def list_servers(self, project_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        return self._catalog(project_id, self._servers(project))

    def create_server(
        self, project_id: str, body: McpServerCreateRequest
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        self._validate_secret_refs(project, body.env)
        servers = self._servers(project)
        server = {
            "id": f"mcp_{uuid.uuid4().hex}",
            "name": body.name,
            "command": body.command,
            "args": list(body.args),
            "cwd": body.cwd,
            "env": self._env_dump(body.env),
            "enabled": body.enabled,
            "revision": 1,
            "lastTest": None,
        }
        servers.append(server)
        self._write_with_secret_refs(project, servers, [server])
        return self._server(server)

    def update_server(
        self, project_id: str, server_id: str, body: McpServerUpdateRequest
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        servers = self._servers(project)
        server = self._find(servers, server_id)
        patch = body.model_dump(exclude_unset=True)
        if "env" in patch:
            self._validate_secret_refs(project, body.env or {})
            patch["env"] = self._env_dump(body.env or {})
        server.update(patch)
        # Configuration edits invalidate a previous inventory/health result.
        server["revision"] = int(server["revision"]) + 1
        server["lastTest"] = None
        server.pop("last_discovered_tool_count", None)
        self._write_with_secret_refs(project, servers, [server])
        return self._server(server)

    def delete_server(self, project_id: str, server_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        servers = self._servers(project)
        self._find(servers, server_id)
        self._write_with_secret_refs(
            project,
            [server for server in servers if server["id"] != server_id],
            [{"id": server_id, "env": {}}],
        )
        return {"ok": True, "deleted": True, "id": server_id}

    def import_servers(
        self, project_id: str, body: McpServerImportRequest
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        servers = self._servers(project)
        added: list[dict[str, Any]] = []
        for name, imported in body.mcp_servers.items():
            self._validate_secret_refs(project, imported.env)
            added.append(
                {
                    "id": f"mcp_{uuid.uuid4().hex}",
                    "name": name,
                    "command": imported.command,
                    "args": list(imported.args),
                    "cwd": imported.cwd,
                    "env": self._env_dump(imported.env),
                    "enabled": True,
                    "revision": 1,
                    "lastTest": None,
                }
            )
        servers.extend(added)
        self._write_with_secret_refs(project, servers, added)
        return self._catalog(project_id, servers)

    def test_server(self, project_id: str, server_id: str) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        servers = self._servers(project)
        server = self._find(servers, server_id)
        report, tool_count = _test_stdio_server(project, server)
        server["lastTest"] = report
        if tool_count is not None:
            server["last_discovered_tool_count"] = tool_count
        self._write(project, servers)
        return self._server(server)

    @staticmethod
    def _servers(project: Any) -> list[dict[str, Any]]:
        servers = read_project_mcp_servers(project)
        # Old/corrupt metadata is ignored by the reader; still reject a
        # malformed individual record here rather than reflecting it into a
        # typed response and turning a settings page into a 500.
        valid: list[dict[str, Any]] = []
        for server in servers:
            try:
                McpServer.model_validate(server)
            except ValueError:
                continue
            valid.append(server)
        return valid

    @staticmethod
    def _write(project: Any, servers: list[dict[str, Any]]) -> None:
        write_project_mcp_servers(project, servers)

    @classmethod
    def _write_with_secret_refs(
        cls,
        project: Any,
        servers: list[dict[str, Any]],
        replacements: list[dict[str, Any]],
    ) -> None:
        # ``set_meta(..., commit=False)`` and replace_secret_consumers share
        # the same SQLite connection.  Either the configuration plus its
        # grants commits together, or both roll back.
        try:
            write_project_mcp_servers(project, servers, commit=False)
            for server in replacements:
                replace_secret_consumers(
                    project,
                    kind="mcp_connector",
                    consumer_id=str(server["id"]),
                    names=cls._secret_refs(server.get("env", {})),
                    commit=False,
                )
            project.db.commit()
        except Exception:
            project.db.rollback()
            raise

    @staticmethod
    def _find(servers: list[dict[str, Any]], server_id: str) -> dict[str, Any]:
        for server in servers:
            if server["id"] == server_id:
                return server
        raise RouteError(404, f"no MCP server '{server_id}'")

    @staticmethod
    def _server(server: dict[str, Any]) -> dict[str, Any]:
        response = McpServer.model_validate(server).model_dump(by_alias=True)
        # This optional advisory is absent until a successful discovery rather
        # than a misleading ``null tools last discovered`` UI value.
        if response["last_discovered_tool_count"] is None:
            response.pop("last_discovered_tool_count")
        return response

    @classmethod
    def _catalog(cls, project_id: str, servers: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schemaVersion": MCP_SERVER_CATALOG_SCHEMA_VERSION,
            "projectId": project_id,
            "servers": [cls._server(server) for server in servers],
        }

    @staticmethod
    def _env_dump(env: dict[str, Any]) -> dict[str, Any]:
        return {
            name: binding.model_dump(by_alias=True) for name, binding in env.items()
        }

    @staticmethod
    def _secret_refs(env: dict[str, Any]) -> set[str]:
        refs = set()
        for binding in env.values():
            if isinstance(binding, dict):
                project_secret = binding.get("project_secret")
                if isinstance(project_secret, str):
                    refs.add(project_secret)
        return refs

    @staticmethod
    def _validate_secret_refs(project: Any, env: dict[str, Any]) -> None:
        # Settings may verify a reference exists, but must never decrypt its
        # plaintext.  Decryption happens only for the short-lived child launch.
        secret_names = {str(row["name"]) for row in project.secret_rows()}
        for name, binding in env.items():
            project_secret = getattr(binding, "project_secret", None)
            if project_secret is not None and project_secret not in secret_names:
                raise RouteError(
                    400, f"project secret '{project_secret}' for {name} is not set"
                )


def _test_stdio_server(
    project: Any, server: dict[str, Any]
) -> tuple[dict[str, str], int | None]:
    """Probe one saved server through the shared outbound stdio client.

    This is intentionally discovery-only: opening a client session sends
    initialize and all required tools/list pages; no tool is called here.
    """
    tested_at = _test_timestamp()
    if not server.get("enabled", True):
        return _failed_test(tested_at, "server_disabled"), None

    try:
        config, secret_values = project_mcp_launch_config(project, server)
        tool_names = asyncio.run(_discover_tools(config))
    except Exception as exc:
        # Exception detail can contain configured argv, literal values, child
        # stderr, or a decrypted Project Secret.  The public result exposes
        # only our stable, bounded error category.
        candidate = getattr(exc, "code", "")
        code = (
            candidate
            if isinstance(candidate, str) and candidate in _PUBLIC_PROBE_FAILURE_CODES
            else "probe_failed"
        )
        return _failed_test(tested_at, code), None

    inventory = ", ".join(
        redact_text(name, secret_values=secret_values, max_chars=80)
        for name in tool_names[:_MAX_ADVISORY_TOOL_NAMES]
    )
    suffix = "" if len(tool_names) <= _MAX_ADVISORY_TOOL_NAMES else ", …"
    diagnostic = redact_text(
        f"MCP server test passed: {len(tool_names)} tools discovered ({inventory}{suffix})",
        secret_values=secret_values,
        max_chars=500,
    )
    return {
        "status": "succeeded",
        "tested_at": tested_at,
        "diagnostic": diagnostic,
    }, len(tool_names)


async def _discover_tools(config: Any) -> list[str]:
    from frisket.ops.integrations.mcp_stdio import McpStdioClient

    async with McpStdioClient(config).session() as session:
        return [tool.name for tool in session.tools]


def _failed_test(tested_at: str, code: str) -> dict[str, str]:
    return {
        "status": "failed",
        "tested_at": tested_at,
        "diagnostic": f"MCP server test failed ({code})",
    }


def _test_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
