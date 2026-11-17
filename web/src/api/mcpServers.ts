import { httpContract } from './httpContract';

type McpEnvBinding = { project_secret: string } | { value: string };
export interface McpServer { id: string; name: string; command: string; args: string[]; cwd: string | null; env: Record<string, McpEnvBinding>; enabled: boolean; revision: number; last_discovered_tool_count?: number | null; lastTest?: { status: 'succeeded' | 'failed'; tested_at?: string | null; diagnostic?: string | null } | null; }
export type McpServerDraft = Pick<McpServer, 'name' | 'command' | 'args' | 'cwd' | 'env' | 'enabled'>;

const mcpServerRequestError = (status: number) =>
  new Error(`MCP server request failed (${status})`);

export function createMcpServersApi(projectId: string) {
  return {
    async listMcpServers(): Promise<McpServer[]> {
      return httpContract(
        'tenant.list_mcp_servers.get',
        {
          pathParams: { pid: projectId },
          errorFactory: mcpServerRequestError,
        },
        (catalog) => catalog.servers,
      );
    },
    async createMcpServer(draft: McpServerDraft): Promise<McpServer> {
      return httpContract('tenant.create_mcp_server.post', {
        pathParams: { pid: projectId },
        body: draft,
        errorFactory: mcpServerRequestError,
      });
    },
    async updateMcpServer(id: string, patch: Partial<McpServerDraft>): Promise<McpServer> {
      return httpContract('tenant.update_mcp_server.patch', {
        pathParams: { pid: projectId, server_id: id },
        body: patch,
        errorFactory: mcpServerRequestError,
      });
    },
    async deleteMcpServer(id: string): Promise<{ ok: boolean; deleted: boolean }> {
      return httpContract('tenant.delete_mcp_server.delete', {
        pathParams: { pid: projectId, server_id: id },
        errorFactory: mcpServerRequestError,
      });
    },
    async testMcpServer(id: string): Promise<McpServer> {
      return httpContract('tenant.test_mcp_server.post', {
        pathParams: { pid: projectId, server_id: id },
        errorFactory: mcpServerRequestError,
      });
    },
  };
}
