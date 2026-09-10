import { httpContract } from './httpContract';

export type McpEnvBinding = { project_secret: string } | { value: string };
export interface McpServer { id: string; name: string; command: string; args: string[]; cwd: string | null; env: Record<string, McpEnvBinding>; enabled: boolean; revision: number; last_discovered_tool_count?: number | null; lastTest?: { status: 'succeeded' | 'failed'; tested_at?: string | null; diagnostic?: string | null } | null; }
export type McpServerDraft = Pick<McpServer, 'name' | 'command' | 'args' | 'cwd' | 'env' | 'enabled'>;

const mcpServerRequestError = (status: number) =>
  new Error(`MCP server request failed (${status})`);

export function createMcpServersApi() {
  return {
    list(projectId: string): Promise<McpServer[]> {
      return httpContract(
        'tenant.list_mcp_servers.get',
        {
          pathParams: { pid: projectId },
          query: {},
          errorFactory: mcpServerRequestError,
        },
        (catalog) => catalog.servers,
      );
    },
    create(projectId: string, draft: McpServerDraft): Promise<McpServer> {
      return httpContract('tenant.create_mcp_server.post', {
        pathParams: { pid: projectId },
        query: {},
        body: draft,
        errorFactory: mcpServerRequestError,
      });
    },
    update(
      projectId: string,
      id: string,
      patch: Partial<McpServerDraft>,
    ): Promise<McpServer> {
      return httpContract('tenant.update_mcp_server.patch', {
        pathParams: { pid: projectId, server_id: id },
        query: {},
        body: patch,
        errorFactory: mcpServerRequestError,
      });
    },
    remove(projectId: string, id: string): Promise<{ ok: boolean; deleted: boolean }> {
      return httpContract('tenant.delete_mcp_server.delete', {
        pathParams: { pid: projectId, server_id: id },
        query: {},
        errorFactory: mcpServerRequestError,
      });
    },
    test(projectId: string, id: string): Promise<McpServer> {
      return httpContract('tenant.test_mcp_server.post', {
        pathParams: { pid: projectId, server_id: id },
        query: {},
        errorFactory: mcpServerRequestError,
      });
    },
  };
}
