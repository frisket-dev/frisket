import { expect, test } from '@playwright/test';
import { mockHostedSettingsShell } from './settingsTestHelpers';

test('Personal theme preferences persist locally and organization API keys are one-time tokens', async ({ page }) => {
  await mockHostedSettingsShell(page);

  const tokens = [
    {
      id: 1,
      user_id: 1,
      created_by: 'owner@example.org',
      name: 'existing token',
      prefix: 'frisket_pat_old',
      created_at: '2026-07-01T12:00:00Z',
      last_used_at: null,
      revoked: false,
    },
  ];

  await page.route('**/api/org/tokens', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(tokens) });
      return;
    }
    const body = route.request().postDataJSON() as { name: string };
    tokens.push({
      id: 2,
      user_id: 1,
      created_by: 'owner@example.org',
      name: body.name,
      prefix: 'frisket_pat_new',
      created_at: '2026-07-01T12:10:00Z',
      last_used_at: null,
      revoked: false,
    });
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 2,
        name: body.name,
        prefix: 'frisket_pat_new',
        token: 'frisket_pat_new_secret',
      }),
    });
  });
  await page.route('**/api/org/tokens/*', async (route) => {
    const id = Number(route.request().url().split('/').pop());
    const token = tokens.find((item) => item.id === id);
    if (token) token.revoked = true;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, revoked: true }) });
  });

  await page.goto('/settings/personal/preferences');

  await page.getByTestId('personal-pref-theme').selectOption('dark');
  await expect(page.getByText('Preferences saved')).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.frisketTheme)).toBe('dark');
  const stored = await page.evaluate(() => localStorage.getItem('frisket.personal_preferences.v1'));
  expect(stored).toContain('"theme":"dark"');

  await page.goto('/settings/organization/api-keys');

  const section = page.getByTestId('organization-api-keys-settings');
  await expect(section).toContainText('existing token');
  await page.getByLabel('Token name').fill('automation token');
  await page.getByRole('button', { name: 'Create' }).click();
  await expect(page.getByTestId('api-key-created')).toContainText('frisket_pat_new_secret');
  await expect(section).toContainText('automation token');
  await page.getByRole('button', { name: 'Revoke' }).first().click();
  await expect(section).toContainText('Revoked');
});
