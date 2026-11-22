// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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

vi.mock('../../src/api/open', () => ({
  ...api,
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  },
}));

import { AdminPage } from '../../src/components/AdminPage';
import type { AdminAuditEvent, AdminAuditFilters, AdminUsers } from '../../src/api/types';
import { EditionModuleProvider } from '../../src/editions/module';
import { TEAM_EDITION_MODULE } from '../../src/editions/openModules';

const AUDIT_EVENTS: AdminAuditEvent[] = [
  {
    id: 'audit:invite',
    source: 'audit_log',
    category: 'invite',
    created_at: '2026-06-14T12:00:00Z',
    actor_user_id: 1,
    actor_email: 'analyst@example.com',
    organization_id: 1,
    organization_name: 'News Lab',
    project_id: null,
    project_name: null,
    action: 'admin_invited',
    detail: '1:reporter@example.com',
    object_type: 'invite',
    object_id: 'reporter@example.com',
    route: null,
    context: { target_organization_id: 1, email: 'reporter@example.com' },
  },
  {
    id: 'audit:member',
    source: 'audit_log',
    category: 'member',
    created_at: '2026-06-14T12:01:00Z',
    actor_user_id: 1,
    actor_email: 'analyst@example.com',
    organization_id: 1,
    organization_name: 'News Lab',
    project_id: 'rss-watch',
    project_name: 'RSS Watch',
    action: 'project_role_set',
    detail: 'rss-watch:reporter@example.com:viewer',
    object_type: 'project_member',
    object_id: 'reporter@example.com',
    route: '/p/rss-watch',
    context: { project_id: 'rss-watch', email: 'reporter@example.com', role: 'viewer' },
  },
  {
    id: 'audit:security',
    source: 'audit_log',
    category: 'security',
    created_at: '2026-06-14T12:04:00Z',
    actor_user_id: 1,
    actor_email: 'analyst@example.com',
    organization_id: 1,
    organization_name: 'News Lab',
    project_id: null,
    project_name: null,
    action: 'pat_created',
    detail: 'frisket_pat_abcd',
    object_type: 'api_token',
    object_id: 'frisket_pat_abcd',
    route: null,
    context: { prefix: 'frisket_pat_abcd' },
  },
  {
    id: 'audit:other-org',
    source: 'audit_log',
    category: 'security',
    created_at: '2026-06-14T12:05:00Z',
    actor_user_id: 9,
    actor_email: 'owner@other.org',
    organization_id: 2,
    organization_name: 'Other Org',
    project_id: null,
    project_name: null,
    action: 'login',
    detail: '',
    object_type: null,
    object_id: null,
    route: null,
    context: { actor_organization_id: 2 },
  },
];

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, '', '/');
});

function renderAdmin(path: string) {
  window.history.replaceState(null, '', path);
  api.getAdminOverview.mockResolvedValue({ organizations: [], totals: { organizations: 0 } });
  return render(
    <EditionModuleProvider edition={TEAM_EDITION_MODULE}>
      <AdminPage />
    </EditionModuleProvider>,
  );
}

describe('AdminPage users and audit panels', () => {
  it('distinguishes member removal from invite revocation and retains invite, role, and retry flows', async () => {
    let memberRemovalAttempts = 0;
    const users: AdminUsers = {
      schema_version: 'frisket.admin_users.v1',
      capabilities: {
        assignable_roles: ['owner', 'admin', 'member'],
        invite_ttl_days: 14,
        magic_link_ttl_minutes: 15,
      },
      organizations: [
        {
          id: 1,
          name: 'News Lab',
          suspended: false,
          users: [
            { user_id: 7, email: 'owner@news.org', name: null, role: 'owner', created_at: '2026-06-01T10:00:00Z' },
            { user_id: 8, email: 'reporter@news.org', name: null, role: 'member', created_at: '2026-06-02T10:00:00Z' },
          ],
          pending_invites: [
            { email: 'pending@news.org', organization_id: 1, role: 'member', expires_at: '2026-06-28T10:00:00Z' },
          ],
        },
      ],
    };

    api.getAdminUsers.mockImplementation(async () => ({
      ...users,
      organizations: users.organizations.map((org) => ({
        ...org,
        users: org.users.map((u) => ({ ...u })),
        pending_invites: org.pending_invites.map((inv) => ({ ...inv })),
      })),
    }));
    api.inviteAdminUser.mockImplementation(async (organizationId: number, email: string) => {
      users.organizations[0].pending_invites.push({
        email: email.toLowerCase(),
        organization_id: organizationId,
        role: 'member',
        expires_at: '2026-06-28T10:00:00Z',
      });
      return { sent: true, organization_id: organizationId, email };
    });
    api.updateAdminUserRole.mockImplementation(async (_orgId: number, userId: number, role: 'owner' | 'admin' | 'member') => {
      const target = users.organizations[0].users.find((u) => u.user_id === userId);
      if (target) target.role = role;
      return { ok: true, user_id: userId, organization_id: 1, role };
    });
    api.removeAdminUser.mockImplementation(async (_orgId: number, userId: number) => {
      memberRemovalAttempts += 1;
      if (memberRemovalAttempts === 1) {
        throw new Error('Membership removal blocked by policy');
      }
      users.organizations[0].users = users.organizations[0].users.filter((u) => u.user_id !== userId);
      return { ok: true, membership_removed: true, user_id: userId, organization_id: 1 };
    });
    api.revokeAdminInvite.mockImplementation(async (_orgId: number, email: string) => {
      users.organizations[0].pending_invites = users.organizations[0].pending_invites.filter(
        (invite) => invite.email !== email,
      );
      return { ok: true, deleted: true };
    });

    const user = userEvent.setup();
    renderAdmin('/admin/users');

    const panel = await screen.findByTestId('admin-users-panel');
    await waitFor(() => expect(within(panel).getByTestId('admin-users-table')).toHaveTextContent('reporter@news.org'));
    expect(within(panel).getByTestId('admin-users-table')).toHaveTextContent('owner@news.org');
    expect(within(panel).getByTestId('admin-invites-table')).toHaveTextContent('pending@news.org');

    await user.type(within(panel).getByTestId('admin-invite-email'), 'newperson@news.org');
    await user.click(within(panel).getByTestId('admin-invite-submit'));
    const confirm = within(panel).getByTestId('admin-invite-confirm');
    expect(confirm).toHaveTextContent('newperson@news.org');
    expect(confirm).toHaveTextContent('News Lab');
    await user.click(within(panel).getByTestId('admin-invite-confirm-send'));
    await waitFor(() => expect(within(panel).getByTestId('admin-invites-table')).toHaveTextContent('newperson@news.org'));

    await user.selectOptions(within(panel).getByTestId('admin-user-role-8'), 'admin');
    await waitFor(() => expect(api.updateAdminUserRole).toHaveBeenCalledWith(1, 8, 'admin'));
    await waitFor(() => expect(within(panel).getByTestId('admin-user-role-8')).toHaveValue('admin'));

    const existingMemberAction = within(panel).getByTestId('admin-user-revoke-8');
    expect(existingMemberAction).toHaveTextContent('Remove membership');

    const pendingRow = within(panel).getByTestId('admin-invites-table').querySelectorAll('tr');
    const pendingInviteRow = Array.from(pendingRow).find((row) => row.textContent?.includes('pending@news.org'))!;
    expect(within(pendingInviteRow).getByRole('button', { name: 'Revoke' })).toBeVisible();

    await user.click(existingMemberAction);
    expect(await within(panel).findByTestId('admin-users-error')).toHaveTextContent(
      'Membership removal blocked by policy',
    );
    expect(within(panel).getByTestId('admin-users-table')).toHaveTextContent('reporter@news.org');

    await user.click(existingMemberAction);
    await waitFor(() => expect(within(panel).getByTestId('admin-users-table')).not.toHaveTextContent('reporter@news.org'));
    expect(within(panel).getByTestId('admin-users-notice')).toHaveTextContent('User access revoked');

    await user.click(within(pendingInviteRow).getByRole('button', { name: 'Revoke' }));
    await waitFor(() => expect(within(panel).getByTestId('admin-invites-table')).not.toHaveTextContent('pending@news.org'));
  });

  it('shows audit event details and filters by organization, project, user, and action', async () => {
    const requestedFilters: AdminAuditFilters[] = [];
    api.getAdminAuditLog.mockImplementation(async (filters: AdminAuditFilters = {}) => {
      requestedFilters.push(filters);
      const userTerm = filters.user?.toLowerCase() ?? '';
      const events = AUDIT_EVENTS.filter((event) => {
        if (filters.organizationId && String(event.organization_id ?? '') !== filters.organizationId) return false;
        if (filters.projectId && String(event.project_id ?? '') !== filters.projectId) return false;
        if (filters.action && event.action !== filters.action) return false;
        if (userTerm) {
          const actorId = event.actor_user_id == null ? '' : String(event.actor_user_id);
          const actorEmail = event.actor_email?.toLowerCase() ?? '';
          if (actorId !== userTerm && !actorEmail.includes(userTerm)) return false;
        }
        return true;
      });
      return {
        schema_version: 'frisket.admin_audit.v1',
        events,
        filters: {
          organizations: [{ id: 1, name: 'News Lab' }, { id: 2, name: 'Other Org' }],
          projects: [{ organization_id: 1, project_id: 'rss-watch', name: 'RSS Watch' }],
          actions: ['admin_invited', 'project_role_set', 'pat_created', 'login'],
        },
      };
    });

    const user = userEvent.setup();
    renderAdmin('/admin/audit');

    const panel = await screen.findByTestId('admin-audit-panel');
    await waitFor(() => expect(within(panel).getByTestId('admin-audit-count')).toHaveTextContent('4'));
    const table = within(panel).getByTestId('admin-audit-table');
    expect(table).toHaveTextContent('invite');
    expect(table).toHaveTextContent('member');
    expect(table).toHaveTextContent('security');

    for (const id of ['audit:invite', 'audit:member', 'audit:security']) {
      const details = within(panel).getByTestId(`admin-audit-details-${id}`);
      await user.click(details.querySelector('summary')!);
      expect(details).toHaveTextContent('object');
    }
    expect(within(panel).getByTestId('admin-audit-details-audit:invite')).toHaveTextContent('reporter@example.com');
    expect(within(panel).getByTestId('admin-audit-details-audit:member')).toHaveTextContent('viewer');

    await user.selectOptions(within(panel).getByTestId('admin-audit-org-filter'), '2');
    await user.click(within(panel).getByTestId('admin-audit-apply'));
    await waitFor(() => expect(table).toHaveTextContent('owner@other.org'));
    expect(table).not.toHaveTextContent('reporter@example.com');
    expect(requestedFilters.at(-1)?.organizationId).toBe('2');

    await user.selectOptions(within(panel).getByTestId('admin-audit-org-filter'), '1');
    await user.selectOptions(within(panel).getByTestId('admin-audit-project-filter'), 'rss-watch');
    await user.click(within(panel).getByTestId('admin-audit-apply'));
    await waitFor(() => expect(table).toHaveTextContent('project_role_set'));
    expect(table).not.toHaveTextContent('pat_created');
    expect(requestedFilters.at(-1)?.projectId).toBe('rss-watch');

    await user.type(within(panel).getByTestId('admin-audit-user-filter'), 'analyst@example.com');
    await user.click(within(panel).getByTestId('admin-audit-apply'));
    await waitFor(() => expect(requestedFilters.at(-1)?.user).toBe('analyst@example.com'));
    expect(table).toHaveTextContent('project_role_set');
    expect(table).not.toHaveTextContent('pat_created');

    await user.click(within(panel).getByTestId('admin-audit-clear'));
    await user.selectOptions(within(panel).getByTestId('admin-audit-action-filter'), 'pat_created');
    await user.click(within(panel).getByTestId('admin-audit-apply'));
    await waitFor(() => expect(table).toHaveTextContent('pat_created'));
    expect(table).not.toHaveTextContent('project_role_set');
    expect(requestedFilters.at(-1)?.action).toBe('pat_created');
  });
});
