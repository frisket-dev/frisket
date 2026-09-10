import { afterEach, describe, expect, it, vi } from 'vitest';

import { createAdminBrowserApi } from '../../src/api/adminBrowser';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('admin browser generated HTTP transport', () => {
  it('renders every method, path, query, and JSON body through the generated harness', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse({});
    }));
    const api = createAdminBrowserApi((status, payload) =>
      Object.assign(new Error(`admin-${status}`), { status, payload }),
    );

    await api.getAdminHealth();
    await api.getAdminUsers();
    await api.inviteAdminUser(7, 'member@example.com');
    await api.updateAdminUserRole(7, 9, 'member');
    await api.removeAdminUser(7, 9);
    await api.revokeAdminInvite(7, 'member@example.com');
    await api.getAdminJobs();
    await api.cancelAdminJob(13);
    await api.getAdminAuditLog({ limit: 25, orgId: '7', projectId: 'alpha', user: 'member@example.com', action: 'org_member_set' });
    await api.getAdminErrors(30);

    expect(requests.map((request) => [request.input, request.init?.method, request.init?.body ?? null])).toEqual([
      ['/api/admin/browser/health', 'GET', null],
      ['/api/admin/browser/users', 'GET', null],
      ['/api/admin/browser/users/invite', 'POST', JSON.stringify({ org_id: 7, email: 'member@example.com' })],
      ['/api/admin/browser/users/9/role', 'PATCH', JSON.stringify({ org_id: 7, role: 'member' })],
      ['/api/admin/browser/users/9?org_id=7', 'DELETE', null],
      ['/api/admin/browser/users/invites/member%40example.com?org_id=7', 'DELETE', null],
      ['/api/admin/browser/jobs', 'GET', null],
      ['/api/admin/browser/jobs/13/cancel', 'POST', null],
      ['/api/admin/browser/audit?limit=25&org_id=7&project_id=alpha&user=member%40example.com&action=org_member_set', 'GET', null],
      ['/api/admin/browser/errors?limit=30', 'GET', null],
    ]);
  });

  it('preserves the generated cancel conflict as a typed 409 error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      ok: false, outcome: 'already_terminal', job_id: 13, retryable: false, detail: 'done',
    }, 409)));
    const api = createAdminBrowserApi((status, payload) =>
      Object.assign(new Error(`admin-${status}`), { status, payload }),
    );

    await expect(api.cancelAdminJob(13)).rejects.toMatchObject({ status: 409 });
  });
});
