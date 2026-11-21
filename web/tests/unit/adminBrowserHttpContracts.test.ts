import { afterEach, describe, expect, it, vi } from 'vitest';

import { createAdminBrowserApi } from '../../src/api/adminBrowser';
import { jsonResponse } from '../support/httpContractTestHelpers';

afterEach(() => vi.unstubAllGlobals());

describe('admin browser generated HTTP transport', () => {
  it('renders every method, path, query, and JSON body through the generated harness', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse({});
    }));
    const api = createAdminBrowserApi();

    await api.getAdminHealth();
    await api.getAdminUsers();
    await api.inviteAdminUser(7, 'member@example.com');
    await api.updateAdminUserRole(7, 9, 'member');
    await api.removeAdminUser(7, 9);
    await api.revokeAdminInvite(7, 'member@example.com');
    await api.getAdminJobs();
    await api.cancelAdminJob(13);
    await api.getAdminAuditLog({ limit: 25, organizationId: '7', projectId: 'alpha', user: 'member@example.com', action: 'organization_member_set' });
    await api.getAdminErrors(30);

    expect(requests.map((request) => [request.input, request.init?.method, request.init?.body ?? null])).toEqual([
      ['/api/admin/browser/health', 'GET', null],
      ['/api/admin/browser/users', 'GET', null],
      ['/api/admin/browser/users/invite', 'POST', JSON.stringify({ organization_id: 7, email: 'member@example.com' })],
      ['/api/admin/browser/users/9/role', 'PATCH', JSON.stringify({ organization_id: 7, role: 'member' })],
      ['/api/admin/browser/users/9?organization_id=7', 'DELETE', null],
      ['/api/admin/browser/users/invites/member%40example.com?organization_id=7', 'DELETE', null],
      ['/api/admin/browser/jobs', 'GET', null],
      ['/api/admin/browser/jobs/13/cancel', 'POST', null],
      ['/api/admin/browser/audit?limit=25&organization_id=7&project_id=alpha&user=member%40example.com&action=organization_member_set', 'GET', null],
      ['/api/admin/browser/errors?limit=30', 'GET', null],
    ]);
  });

  it('preserves the generated cancel conflict as a typed 409 error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      ok: false, outcome: 'already_terminal', job_id: 13, retryable: false, detail: 'done',
    }, 409)));
    const api = createAdminBrowserApi();

    await expect(api.cancelAdminJob(13)).rejects.toMatchObject({
      name: 'ApiError', status: 409, message: 'done', code: undefined, details: undefined,
    });
  });
});
