import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { createAdminOverviewApi } from '../../src/api/adminOverview';
import { ApiError, getAdminOverview } from '../../src/api/real';

const overview = {
  orgs: [{
    id: 17,
    name: 'Cloud',
    suspended: false,
    credits_usd: 12.5,
    projects: 3,
    users: ['owner@example.com'],
    runs_30d: 4,
    spend_30d: 4.2,
  }],
  totals: {
    orgs: 1,
    pending_invites: 2,
    credits_usd: 12.5,
    spend_30d: 4.2,
  },
  next_invite_expires_at: '2026-08-13T00:00:00Z',
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('admin overview generated HTTP contract', () => {
  it('uses the exact route and preserves top-level and totals extensions', async () => {
    const fetch = vi.fn(async () => jsonResponse(overview));
    vi.stubGlobal('fetch', fetch);
    const api = createAdminOverviewApi((status, payload) =>
      Object.assign(new Error(`mapped-${status}`), { status, payload }),
    );

    await expect(api.getAdminOverview()).resolves.toEqual(overview);
    expect(fetch).toHaveBeenCalledWith('/api/admin/overview', { method: 'GET' });
  });

  it('keeps AdminPage denial handling observable as a 403 ApiError', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      detail: 'admin routes require a browser session',
    }, 403)));

    await expect(getAdminOverview()).rejects.toMatchObject({
      name: 'ApiError',
      status: 403,
      message: 'admin routes require a browser session',
    } satisfies Partial<ApiError>);
  });
});
