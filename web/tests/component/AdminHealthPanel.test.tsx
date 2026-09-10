// @vitest-environment jsdom
//
// /admin/health had a route (src/frisket/team/observability_routes.py
// `admin_health`) with zero client callers. AdminHealthPanel is the minimal
// UI: ok/fail chips per probe (db, blob store, run queue), queue counts, and
// the active-run count. Pins both the all-ok render and a mixed-failure
// render so a probe going red is visibly distinct.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  getAdminOverview: vi.fn(),
  getHealth: vi.fn(),
  getAdminAuditLog: vi.fn(),
  getAdminErrors: vi.fn(),
  getAdminHealth: vi.fn(),
  getAdminJobs: vi.fn(),
  getAdminUsers: vi.fn(),
  inviteAdminUser: vi.fn(),
  updateAdminUserRole: vi.fn(),
  removeAdminUser: vi.fn(),
  revokeAdminInvite: vi.fn(),
  cancelAdminJob: vi.fn(),
}));
const diagnosticsApi = vi.hoisted(() => ({ fetchDiagnostics: vi.fn() }));

vi.mock('../../src/api/open', () => ({
  ...api,
  ApiError: class ApiError extends Error {
    status = 500;
  },
}));
vi.mock('../../src/api/diagnostics', () => diagnosticsApi);

import { AdminHealthPanel, AdminPage } from '../../src/components/AdminPage';
import type { AdminHealth } from '../../src/api/types';
import { EditionModuleProvider } from '../../src/editions/module';
import { TEAM_EDITION_MODULE } from '../../src/editions/openModules';



function health(overrides: Partial<AdminHealth> = {}): AdminHealth {
  return {
    schema_version: 'frisket.admin_health.v1',
    ok: true,
    db: { ok: true, dialect: 'sqlite', latency_ms: 1, error: null },
    blob_store: {
      ok: true,
      error: null,
    },
    active_runs: 2,
    queue: {
      ok: true,
      counts: { queued: 1, running: 2, done: 0, failed: 0, cancelled: 0 },
      error: null,
    },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, '', '/');
});

describe('AdminHealthPanel', () => {
  it('renders ok chips for a healthy probe set', () => {
    render(<AdminHealthPanel health={health()} error={null} onRefresh={() => {}} />);
    expect(screen.getByTestId('admin-health-overall')).toHaveTextContent('overall ok');
    expect(screen.getByTestId('admin-health-db')).toHaveTextContent('db ok');
    expect(screen.getByTestId('admin-health-blob')).toHaveTextContent('blob ok');
    expect(screen.getByTestId('admin-health-queue')).toHaveTextContent('queue ok');
    expect(screen.getByTestId('admin-health-active-runs')).toHaveTextContent('active runs 2');
    expect(screen.getByTestId('admin-health-queue-count-running')).toHaveTextContent('running 2');
    expect(screen.queryByTestId('admin-health-error')).toBeNull();
  });

  it('renders fail chips and the error banner when a probe is down', () => {
    render(
      <AdminHealthPanel
        health={health({
          ok: false,
          db: { ok: false, dialect: 'sqlite', latency_ms: null, error: 'failed' },
        })}
        error="db probe failed"
        onRefresh={() => {}}
      />,
    );
    expect(screen.getByTestId('admin-health-overall')).toHaveTextContent('overall fail');
    expect(screen.getByTestId('admin-health-db')).toHaveTextContent('db fail');
    expect(screen.getByTestId('admin-health-db').className).toContain('job-failed');
    expect(screen.getByTestId('admin-health-blob')).toHaveTextContent('blob ok');
    expect(screen.getByTestId('admin-health-error')).toHaveTextContent('db probe failed');
  });

  it('shows a loading placeholder when health has not loaded yet', () => {
    render(<AdminHealthPanel health={null} error={null} onRefresh={() => {}} />);
    expect(screen.getByTestId('admin-health-loading')).toHaveTextContent('Loading');
    expect(screen.queryByTestId('admin-health-overall')).toBeNull();
  });

  it('uses generated role capabilities and invite TTLs instead of fixed admin copy', async () => {
    window.history.replaceState(null, '', '/admin/users');
    api.getAdminOverview.mockResolvedValue({ totals: {} });
    api.getHealth.mockResolvedValue({ ok: true });
    api.getAdminHealth.mockResolvedValue(health());
    api.getAdminJobs.mockResolvedValue({
      schema_version: 'frisket.admin_jobs.v1',
      summary: { queued: 0, running: 0, stalled: 0, failed: 0, done: 0, cancelled: 0, live_workers: 1, no_live_worker: false },
      workers: { live: 1, liveness_window_seconds: 60 },
      jobs: [],
    });
    api.getAdminAuditLog.mockResolvedValue({
      schema_version: 'frisket.admin_audit.v1', events: [], filters: { orgs: [], projects: [], actions: [] },
    });
    api.getAdminErrors.mockResolvedValue({ schema_version: 'frisket.admin_errors.v1', errors: [], retention: null });
    api.getAdminUsers.mockResolvedValue({
      schema_version: 'frisket.admin_users.v1',
      orgs: [{ id: 7, name: 'Open', suspended: false, pending_invites: [], users: [{ user_id: 9, email: 'member@example.com', name: null, role: 'member', created_at: null }] }],
      capabilities: { assignable_roles: ['owner', 'member'], magic_link_ttl_minutes: 30, invite_ttl_days: 9 },
    });

    render(
      <EditionModuleProvider edition={TEAM_EDITION_MODULE}>
        <AdminPage />
      </EditionModuleProvider>,
    );

    const role = await screen.findByTestId('admin-user-role-9');
    expect(Array.from((role as HTMLSelectElement).options).map((option) => option.value)).toEqual(['owner', 'member']);
    expect(screen.getByTestId('admin-invites-recovery-note')).toHaveTextContent('30 minutes');
    expect(screen.getByTestId('admin-invites-recovery-note')).toHaveTextContent('9 days');
    expect(screen.getByTestId('admin-nav')).toHaveTextContent('Diagnostics');
    expect(screen.queryByTestId('admin-jobs-panel')).toBeNull();
    expect(api.getAdminUsers).toHaveBeenCalledOnce();
    expect(api.getAdminAuditLog).not.toHaveBeenCalled();
    expect(api.getAdminErrors).not.toHaveBeenCalled();
    expect(api.getAdminHealth).not.toHaveBeenCalled();
    expect(api.getAdminJobs).not.toHaveBeenCalled();
    expect(api.getHealth).not.toHaveBeenCalled();
  });

  it('reuses personal diagnostics beside deployment health', async () => {
    window.history.replaceState(null, '', '/admin/diagnostics');
    api.getAdminOverview.mockResolvedValue({ totals: {} });
    api.getHealth.mockResolvedValue({ ok: true });
    api.getAdminHealth.mockResolvedValue(health());
    api.getAdminJobs.mockResolvedValue({
      schema_version: 'frisket.admin_jobs.v1', summary: {}, workers: {}, jobs: [],
    });
    api.getAdminAuditLog.mockResolvedValue({
      schema_version: 'frisket.admin_audit.v1', events: [], filters: { orgs: [], projects: [], actions: [] },
    });
    api.getAdminErrors.mockResolvedValue({ schema_version: 'frisket.admin_errors.v1', errors: [], retention: null });
    api.getAdminUsers.mockResolvedValue({ schema_version: 'frisket.admin_users.v1', orgs: [], capabilities: {} });
    diagnosticsApi.fetchDiagnostics.mockResolvedValue({
      healthy: true,
      core: { models: { ok: true, detail: 'Models available' } },
      info: {},
    });

    render(
      <EditionModuleProvider edition={TEAM_EDITION_MODULE}>
        <AdminPage />
      </EditionModuleProvider>,
    );

    expect(await screen.findByTestId('diagnose-row-models')).toHaveTextContent('Models available');
    expect(await screen.findByTestId('admin-health-db')).toHaveTextContent('db ok');
    expect(screen.getByRole('link', { name: 'Diagnostics' })).toHaveAttribute('aria-current', 'page');
  });
});
