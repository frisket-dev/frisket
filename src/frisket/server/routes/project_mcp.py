"""Solo-only HTTP routes for project-scoped stdio MCP configuration."""

from __future__ import annotations

from fastapi import FastAPI

from frisket.contracts.http.project_mcp import (
    McpServer,
    McpServerCatalog,
    McpServerCreateRequest,
    McpServerDeleteResponse,
    McpServerImportRequest,
    McpServerUpdateRequest,
)
from frisket.server.route_errors import http_error_responses
from frisket.server.services.project_mcp import ProjectMcpService


def register_project_mcp_routes(app: FastAPI, *, service: ProjectMcpService) -> None:
    @app.get(
        "/api/projects/{pid}/mcp-servers",
        response_model=McpServerCatalog,
        responses=http_error_responses(401, 404, 500),
    )
    def list_mcp_servers(pid: str) -> McpServerCatalog:
        return McpServerCatalog.model_validate(service.list_servers(pid))

    @app.post(
        "/api/projects/{pid}/mcp-servers",
        response_model=McpServer,
        responses=http_error_responses(400, 401, 404, 422, 500),
    )
    def create_mcp_server(pid: str, body: McpServerCreateRequest) -> McpServer:
        return McpServer.model_validate(service.create_server(pid, body))

    @app.post(
        "/api/projects/{pid}/mcp-servers/import",
        response_model=McpServerCatalog,
        responses=http_error_responses(400, 401, 404, 422, 500),
    )
    def import_mcp_servers(pid: str, body: McpServerImportRequest) -> McpServerCatalog:
        return McpServerCatalog.model_validate(service.import_servers(pid, body))

    @app.patch(
        "/api/projects/{pid}/mcp-servers/{server_id}",
        response_model=McpServer,
        responses=http_error_responses(400, 401, 404, 422, 500),
    )
    def update_mcp_server(
        pid: str, server_id: str, body: McpServerUpdateRequest
    ) -> McpServer:
        return McpServer.model_validate(service.update_server(pid, server_id, body))

    @app.delete(
        "/api/projects/{pid}/mcp-servers/{server_id}",
        response_model=McpServerDeleteResponse,
        responses=http_error_responses(401, 404, 500),
    )
    def delete_mcp_server(pid: str, server_id: str) -> McpServerDeleteResponse:
        return McpServerDeleteResponse.model_validate(
            service.delete_server(pid, server_id)
        )

    @app.post(
        "/api/projects/{pid}/mcp-servers/{server_id}/test",
        response_model=McpServer,
        responses=http_error_responses(401, 404, 500),
    )
    def test_mcp_server(pid: str, server_id: str) -> McpServer:
        return McpServer.model_validate(service.test_server(pid, server_id))
