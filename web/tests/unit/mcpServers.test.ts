// Dedicated project-MCP endpoint contract.  These calls intentionally do not
// piggyback on project secrets: server definitions and secret values have
// different ownership and refresh lifecycles.

import { afterEach, describe, expect, it, vi } from 'vitest';

import { createMcpServersApi } from '../../src/api/mcpServers';

const server = {
  id: 'local-crm',
  name: 'Local CRM',
  command: 'uvx',
  args: ['company-crm-mcp'],
  cwd: null,
  env: { CRM_TOKEN: { project_secret: 'CRM_TOKEN' } },
  enabled: true,
};

afterEach(() => vi.unstubAllGlobals());

describe('project MCP servers API', () => {
  it('uses the dedicated project endpoint for list, CRUD, and test', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ servers: [server] }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(server), { status: 201 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...server, enabled: false }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ...server,
        revision: 1,
        last_discovered_tool_count: 8,
        lastTest: { status: 'succeeded', tested_at: '2026-09-02T12:00:00Z' },
      }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, deleted: true }), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const api = createMcpServersApi();

    await expect(api.list('project-7')).resolves.toEqual([server]);
    await api.create('project-7', server);
    await api.update('project-7', 'local-crm', { enabled: false });
    await api.test('project-7', 'local-crm');
    await api.remove('project-7', 'local-crm');

    expect(fetch.mock.calls.map(([path, init]) => [path, init.method, init.body && JSON.parse(init.body as string)])).toEqual([
      ['/api/projects/project-7/mcp-servers', 'GET', undefined],
      ['/api/projects/project-7/mcp-servers', 'POST', server],
      ['/api/projects/project-7/mcp-servers/local-crm', 'PATCH', { enabled: false }],
      ['/api/projects/project-7/mcp-servers/local-crm/test', 'POST', undefined],
      ['/api/projects/project-7/mcp-servers/local-crm', 'DELETE', undefined],
    ]);
  });

  it('preserves the MCP-specific request error at the shared transport seam', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: 'server detail' }), { status: 503 }),
    ));

    await expect(createMcpServersApi().list('project-7')).rejects.toThrow(
      'MCP server request failed (503)',
    );
  });
});
