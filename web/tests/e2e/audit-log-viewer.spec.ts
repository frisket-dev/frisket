// RED-FIRST (authored 2026-06-14): hosted operators need a browsable audit
// feed in /admin without direct database access.

import { expect, test } from '@playwright/test';

const auditEvents = [
  {
    id: 'audit:invite',
    source: 'audit_log',
    category: 'invite',
    created_at: '2026-06-14T12:00:00Z',
    actor_user_id: 1,
    actor_email: 'analyst@example.com',
    org_id: 1,
    org_name: 'News Lab',
    project_id: null,
    project_name: null,
    action: 'admin_invited',
    detail: '1:reporter@example.com',
    object_type: 'invite',
    object_id: 'reporter@example.com',
    route: null,
    context: { target_org_id: 1, email: 'reporter@example.com' },
  },
  {
    id: 'audit:member',
    source: 'audit_log',
    category: 'member',
    created_at: '2026-06-14T12:01:00Z',
    actor_user_id: 1,
    actor_email: 'analyst@example.com',
    org_id: 1,
    org_name: 'News Lab',
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
    org_id: 1,
    org_name: 'News Lab',
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
    org_id: 2,
    org_name: 'Other Org',
    project_id: null,
    project_name: null,
    action: 'login',
    detail: '',
    object_type: null,
    object_id: null,
    route: null,
    context: { actor_org_id: 2 },
  },
];

test('admin audit feed shows event details and filters by org, project, user, and action', async ({ page }) => {
  const auditRequests: URL[] = [];

  await page.route('**/api/admin/overview', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        orgs: [],
        totals: { orgs: 0 },
      }),
    });
  });
  await page.route('**/api/admin/browser/users', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.admin_users.v1',
        capabilities: {
          assignable_roles: ['owner', 'admin', 'member'],
          invite_ttl_days: 14,
          magic_link_ttl_minutes: 15,
        },
        orgs: [],
      }),
    });
  });
  await page.route('**/api/admin/browser/jobs', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ summary: {}, jobs: [] }),
    });
  });
  await page.route('**/api/admin/browser/errors?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ errors: [] }),
    });
  });
  await page.route('**/api/admin/browser/audit?*', async (route) => {
    const url = new URL(route.request().url());
    auditRequests.push(url);
    const orgId = url.searchParams.get('org_id');
    const projectId = url.searchParams.get('project_id');
    const user = url.searchParams.get('user')?.toLowerCase() ?? '';
    const action = url.searchParams.get('action');
    const events = auditEvents.filter((event) => {
      if (orgId && String(event.org_id ?? '') !== orgId) return false;
      if (projectId && String(event.project_id ?? '') !== projectId) return false;
      if (action && event.action !== action) return false;
      if (user) {
        const actorId = event.actor_user_id == null ? '' : String(event.actor_user_id);
        const actorEmail = event.actor_email?.toLowerCase() ?? '';
        if (actorId !== user && !actorEmail.includes(user)) return false;
      }
      return true;
    });
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        events,
        filters: {
          orgs: [{ id: 1, name: 'News Lab' }, { id: 2, name: 'Other Org' }],
          projects: [{ org_id: 1, id: 'rss-watch', name: 'RSS Watch' }],
          actions: ['admin_invited', 'project_role_set', 'pat_created', 'login'],
        },
      }),
    });
  });

  await page.goto('/admin/audit');
  const panel = page.getByTestId('admin-audit-panel');
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId('admin-audit-count')).toContainText('4');
  const table = panel.getByTestId('admin-audit-table');
  await expect(table).toContainText('invite');
  await expect(table).toContainText('member');
  await expect(table).toContainText('security');

  for (const id of ['audit:invite', 'audit:member', 'audit:security']) {
    const details = panel.getByTestId(`admin-audit-details-${id}`);
    await details.locator('summary').click();
    await expect(details).toContainText('object');
  }
  await expect(panel.getByTestId('admin-audit-details-audit:invite')).toContainText('reporter@example.com');
  await expect(panel.getByTestId('admin-audit-details-audit:member')).toContainText('viewer');

  await panel.getByTestId('admin-audit-org-filter').selectOption('2');
  await panel.getByTestId('admin-audit-apply').click();
  await expect(table).toContainText('owner@other.org');
  await expect(table).not.toContainText('reporter@example.com');
  expect(auditRequests.at(-1)?.searchParams.get('org_id')).toBe('2');

  await panel.getByTestId('admin-audit-org-filter').selectOption('1');
  await panel.getByTestId('admin-audit-project-filter').selectOption('rss-watch');
  await panel.getByTestId('admin-audit-apply').click();
  await expect(table).toContainText('project_role_set');
  await expect(table).not.toContainText('pat_created');
  expect(auditRequests.at(-1)?.searchParams.get('project_id')).toBe('rss-watch');

  await panel.getByTestId('admin-audit-user-filter').fill('analyst@example.com');
  await panel.getByTestId('admin-audit-apply').click();
  await expect(table).toContainText('project_role_set');
  await expect(table).not.toContainText('pat_created');
  expect(auditRequests.at(-1)?.searchParams.get('user')).toBe('analyst@example.com');

  await panel.getByTestId('admin-audit-clear').click();
  await panel.getByTestId('admin-audit-action-filter').selectOption('pat_created');
  await panel.getByTestId('admin-audit-apply').click();
  await expect(table).toContainText('pat_created');
  await expect(table).not.toContainText('project_role_set');
  expect(auditRequests.at(-1)?.searchParams.get('action')).toBe('pat_created');
});
