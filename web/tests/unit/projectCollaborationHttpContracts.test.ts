import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, listProjectMembers } from '../../src/api/real';
import { createProjectCollaborationApi } from '../../src/api/projectCollaboration';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('project collaboration generated HTTP contracts', () => {
  it('preserves raw route inputs, request bodies, headers, abort, and bodyless DELETEs', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [
      [{ user_id: 7, email: 'member@example.com', role: 'owner' }],
      { ok: true, email: 'member@example.com', role: 'editor' },
      { ok: true, removed: true },
      { invites: [{ id: 9, project_id: 'project /☃', email: 'invite@example.com', role: 'viewer', created_at: 'now', expires_at: 'later', accepted_at: null, revoked_at: null }] },
      { sent: true, invite: { id: 10, project_id: 'project /☃', email: 'invite@example.com', role: 'editor', created_at: 'now', expires_at: 'later', accepted_at: null, revoked_at: null } },
      { ok: true, revoked: true },
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer session', 'X-Trace-Id': 'collaboration' },
    };
    const api = createProjectCollaborationApi((status, payload) =>
      Object.assign(new Error('collaboration failed'), { status, payload }),
    );
    const projectId = 'project /☃';
    const email = 'member +/☃@example.com';

    await api.listMembers(projectId, options);
    await api.setMember(projectId, email, 'editor', options);
    await api.removeMember(projectId, email, options);
    await api.listInvites(projectId, options);
    await api.createInvite(projectId, email, 'editor', options);
    await api.revokeInvite(projectId, '9 /☃', options);

    const base = '/api/projects/project%20%2F%E2%98%83';
    const encodedEmail = 'member%20%2B%2F%E2%98%83%40example.com';
    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      [`${base}/members`, 'GET', undefined],
      [`${base}/members`, 'POST', JSON.stringify({ email, role: 'editor' })],
      [`${base}/members/${encodedEmail}`, 'DELETE', undefined],
      [`${base}/invites`, 'GET', undefined],
      [`${base}/invites`, 'POST', JSON.stringify({ email, role: 'editor' })],
      [`${base}/invites/9%20%2F%E2%98%83`, 'DELETE', undefined],
    ]);
    expect(globalThis.fetch).toHaveBeenCalledTimes(6);
    for (const { init } of requests) {
      expect(init?.signal).toBe(controller.signal);
      const headers = new Headers(init?.headers);
      expect(headers.get('authorization')).toBe('Bearer session');
      expect(headers.get('x-trace-id')).toBe('collaboration');
    }
    for (const request of [requests[1], requests[4]]) {
      expect(new Headers(request.init?.headers).get('content-type')).toBe('application/json');
    }
    for (const request of [requests[0], requests[2], requests[3], requests[5]]) {
      expect(new Headers(request.init?.headers).get('content-type')).toBeNull();
    }
  });

  it('keeps string-detail collaboration failures as generic ApiErrors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'project owner required' }, 403)));

    await expect(listProjectMembers('project-1')).rejects.toMatchObject({
      name: 'ApiError',
      status: 403,
      message: 'project owner required',
      code: undefined,
      details: undefined,
    } satisfies Partial<ApiError>);
  });
});
