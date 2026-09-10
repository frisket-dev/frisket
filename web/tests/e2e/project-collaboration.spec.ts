import { expect, test } from '@playwright/test';

test('Project Access settings manage members and invites without mounting Workspace', async ({ page }) => {
  const members = [
    { user_id: 1, email: 'owner@example.org', role: 'owner' },
    { user_id: 2, email: 'reporter@example.org', role: 'editor' },
  ];
  const invites = [
    {
      id: 50,
      email: 'pending@example.org',
      role: 'viewer',
      created_at: '2026-06-17T12:00:00Z',
      expires_at: '2026-07-01T12:00:00Z',
      accepted_at: null,
      revoked_at: null,
    },
  ];
  const memberChanges: Array<{ email: string; role: string }> = [];
  const inviteCreates: Array<{ email: string; role: string }> = [];

  await page.route('**/api/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ email: 'owner@example.org', display_name: 'Owner' }),
  }));
  await page.route('**/api/column-types', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '[]',
  }));
  await page.route('**/api/projects', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{ id: 'alpha', name: 'Alpha Project', description: '', sensitive: false, updated_at: null, pending_review_count: 0, starred: false, archived: false, role: 'owner' }]),
  }));
  await page.route('**/api/projects/alpha/members', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(members) });
      return;
    }
    const body = route.request().postDataJSON() as { email: string; role: string };
    const member = members.find((item) => item.email === body.email);
    if (member) member.role = body.role;
    memberChanges.push(body);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, email: body.email, role: body.role }) });
  });
  await page.route('**/api/projects/alpha/members/*', async (route) => {
    const email = decodeURIComponent(route.request().url().split('/').pop() ?? '');
    const index = members.findIndex((item) => item.email === email);
    if (index >= 0) members.splice(index, 1);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, removed: index >= 0 }) });
  });
  await page.route('**/api/projects/alpha/invites', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ invites }) });
      return;
    }
    const body = route.request().postDataJSON() as { email: string; role: 'viewer' | 'editor' };
    inviteCreates.push(body);
    invites.push({
      id: 51,
      email: body.email,
      role: body.role,
      created_at: '2026-06-17T13:00:00Z',
      expires_at: '2026-07-01T13:00:00Z',
      accepted_at: null,
      revoked_at: null,
    });
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ sent: true, invite: invites.at(-1) }) });
  });
  await page.route('**/api/projects/alpha/invites/*', async (route) => {
    const id = Number(route.request().url().split('/').pop());
    const index = invites.findIndex((item) => item.id === id);
    if (index >= 0) invites.splice(index, 1);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, revoked: index >= 0 }) });
  });

  await page.goto('/p/alpha/settings/project/access');

  const section = page.getByTestId('project-access-settings');
  await expect(section).toBeVisible();
  await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
  await expect(section).toContainText('owner@example.org');
  await expect(section).toContainText('reporter@example.org');
  await section.getByTestId('project-member-role-2').selectOption('viewer');
  expect(memberChanges).toContainEqual({ email: 'reporter@example.org', role: 'viewer' });

  await section.getByTestId('project-invite-email').fill('collab@example.org');
  await section.getByTestId('project-invite-submit').click();
  await expect(section).toContainText('collab@example.org');
  expect(inviteCreates).toContainEqual({ email: 'collab@example.org', role: 'viewer' });
});

test('Project Access settings render partial data and read-only controls for viewers', async ({ page }) => {
  await page.route('**/api/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ email: 'viewer@example.org', display_name: 'Viewer' }),
  }));
  await page.route('**/api/column-types', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '[]',
  }));
  await page.route('**/api/projects', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([{ id: 'alpha', name: 'Alpha Project', description: '', sensitive: false, updated_at: null, pending_review_count: 0, starred: false, archived: false, role: 'viewer' }]),
  }));
  await page.route('**/api/projects/alpha/members', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify([
      { user_id: 1, email: 'owner@example.org', role: 'owner' },
      { user_id: 2, email: 'viewer@example.org', role: 'viewer' },
    ]),
  }));
  await page.route('**/api/projects/alpha/invites', (route) => route.fulfill({
    status: 403,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'permission denied' }),
  }));

  await page.goto('/p/alpha/settings/project/access');

  const section = page.getByTestId('project-access-settings');
  await expect(section).toContainText('owner@example.org');
  await expect(section).toContainText('viewer@example.org');
  await expect(section).toContainText('cannot change members or invites');
  await expect(section).toContainText('You do not have permission to manage project invites.');
  await expect(section.getByTestId('project-member-role-2')).toBeDisabled();
  await expect(section.getByTestId('project-member-remove-2')).toBeDisabled();
  await expect(section.getByTestId('project-invite-email')).toHaveCount(0);
});
