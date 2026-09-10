// RED-FIRST (authored 2026-06-14): operators need hosted admin user/invite
// management without direct database access.
// RED-FIRST (amended 2026-08-10): removing one existing-member membership is
// distinct from revoking a still-pending invitation.

import { expect, test } from '@playwright/test';

test('admin page distinguishes member removal from invite revocation and retains action flows', async ({ page }) => {
  let memberRemovalAttempts = 0;
  const users = {
    schema_version: 'frisket.admin_users.v1',
    capabilities: {
      assignable_roles: ['owner', 'admin', 'member'],
      invite_ttl_days: 14,
      magic_link_ttl_minutes: 15,
    },
    orgs: [
      {
        id: 1,
        name: 'News Lab',
        suspended: false,
        users: [
          {
            id: 7,
            email: 'owner@news.org',
            name: null,
            role: 'owner',
            created_at: '2026-06-01T10:00:00Z',
          },
          {
            id: 8,
            email: 'reporter@news.org',
            name: null,
            role: 'member',
            created_at: '2026-06-02T10:00:00Z',
          },
        ],
        pending_invites: [
          {
            email: 'pending@news.org',
            org_id: 1,
            expires_at: '2026-06-28T10:00:00Z',
          },
        ],
      },
    ],
  };

  await page.route('**/api/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ email: 'owner@news.org', display_name: 'Owner' }),
    });
  });
  await page.route('**/api/config', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        cache_mode: 'off',
        live_calls_possible: true,
        cache_mode_editable: false,
        email_from_address: null,
        email_from_name: null,
      }),
    });
  });
  await page.route('**/api/column-types', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });
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
  await page.route('**/api/admin/browser/users**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'GET' && url.pathname === '/api/admin/browser/users') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(users),
      });
      return;
    }
    if (request.method() === 'POST' && url.pathname === '/api/admin/browser/users/invite') {
      const body = request.postDataJSON() as { org_id: number; email: string };
      users.orgs[0].pending_invites.push({
        email: body.email.toLowerCase(),
        org_id: body.org_id,
        expires_at: '2026-06-28T10:00:00Z',
      });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ sent: true, org_id: body.org_id, email: body.email }),
      });
      return;
    }
    if (request.method() === 'PATCH' && url.pathname === '/api/admin/browser/users/8/role') {
      const body = request.postDataJSON() as { role: 'owner' | 'admin' | 'member' };
      users.orgs[0].users[1].role = body.role;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true, user_id: 8, org_id: 1, role: body.role }),
      });
      return;
    }
    if (request.method() === 'DELETE' && url.pathname === '/api/admin/browser/users/8') {
      expect(url.searchParams.get('org_id')).toBe('1');
      memberRemovalAttempts += 1;
      if (memberRemovalAttempts === 1) {
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Membership removal blocked by policy' }),
        });
        return;
      }
      users.orgs[0].users = users.orgs[0].users.filter((user) => user.id !== 8);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true,
          membership_removed: true,
          user_id: 8,
          org_id: 1,
        }),
      });
      return;
    }
    if (
      request.method() === 'DELETE'
      && (
        url.pathname === '/api/admin/browser/users/invites/pending@news.org'
        || url.pathname === '/api/admin/browser/users/invites/pending%40news.org'
      )
    ) {
      users.orgs[0].pending_invites = users.orgs[0].pending_invites.filter(
        (invite) => invite.email !== 'pending@news.org',
      );
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true, deleted: true }),
      });
      return;
    }
    await route.fulfill({ status: 404, body: 'unhandled admin users test route' });
  });

  await page.goto('/admin/users');
  const panel = page.getByTestId('admin-users-panel');
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId('admin-users-table')).toContainText('owner@news.org');
  await expect(panel.getByTestId('admin-users-table')).toContainText('reporter@news.org');
  await expect(panel.getByTestId('admin-invites-table')).toContainText('pending@news.org');

  await panel.getByTestId('admin-invite-email').fill('newperson@news.org');
  await panel.getByTestId('admin-invite-submit').click();
  const confirm = panel.getByTestId('admin-invite-confirm');
  await expect(confirm).toContainText('newperson@news.org');
  await expect(confirm).toContainText('News Lab');
  await panel.getByTestId('admin-invite-confirm-send').click();
  await expect(panel.getByTestId('admin-invites-table')).toContainText('newperson@news.org');

  await panel.getByTestId('admin-user-role-8').selectOption('admin');
  await expect(panel.getByTestId('admin-user-role-8')).toHaveValue('admin');

  const existingMemberAction = panel.getByTestId('admin-user-revoke-8');
  // Keep running the retained error/success/invite controls after recording
  // the single intended RED: the existing-member label is still "Revoke".
  await expect.soft(existingMemberAction).toHaveText('Remove membership');

  const pendingRow = panel.getByTestId('admin-invites-table').locator('tr', {
    hasText: 'pending@news.org',
  });
  await expect(pendingRow.getByRole('button', { name: 'Revoke', exact: true })).toBeVisible();

  await existingMemberAction.click();
  await expect(panel.getByTestId('admin-users-error')).toContainText(
    'Membership removal blocked by policy',
  );
  await expect(panel.getByTestId('admin-users-table')).toContainText('reporter@news.org');

  const membershipRemovalResponsePromise = page.waitForResponse((response) => {
    const request = response.request();
    const url = new URL(response.url());
    return request.method() === 'DELETE'
      && url.pathname === '/api/admin/browser/users/8'
      && response.status() === 200;
  });
  await existingMemberAction.click();
  const membershipRemovalResponse = await membershipRemovalResponsePromise;
  expect(await membershipRemovalResponse.json()).toEqual({
    ok: true,
    membership_removed: true,
    user_id: 8,
    org_id: 1,
  });
  await expect(panel.getByTestId('admin-users-table')).not.toContainText('reporter@news.org');
  await expect(panel.getByTestId('admin-users-notice')).toContainText('User access revoked');

  await pendingRow.getByRole('button', { name: 'Revoke' }).click();
  await expect(panel.getByTestId('admin-invites-table')).not.toContainText('pending@news.org');
});
