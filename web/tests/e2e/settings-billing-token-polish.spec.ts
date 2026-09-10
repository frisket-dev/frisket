import { expect, test } from '@playwright/test';
import { mockHostedSettingsShell } from './settingsTestHelpers';

test('organization API key creation has a one-time copyable reveal and redacted list', async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { __copiedText: string | null }).__copiedText = null;
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: async (text: string) => {
          (window as unknown as { __copiedText: string | null }).__copiedText = text;
        },
      },
    });
  });
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

  await page.goto('/settings/organization/api-keys');

  const section = page.getByTestId('organization-api-keys-settings');
  await page.getByLabel('Token name').fill('automation token');
  await page.getByRole('button', { name: 'Create' }).click();

  const reveal = page.getByTestId('api-key-created');
  await expect(reveal).toContainText('Copy this API token now');
  await expect(reveal.getByTestId('api-key-created-token')).toContainText('frisket_pat_new_secret');
  await reveal.getByRole('button', { name: /^Copy$/ }).click();
  await expect(reveal).toContainText('Copied');
  await expect.poll(() => page.evaluate(() => (
    window as unknown as { __copiedText: string | null }
  ).__copiedText)).toBe('frisket_pat_new_secret');

  await expect(section.getByTestId('api-token-prefix-2')).toContainText('frisket_pat_new');
  await expect(section.getByTestId('api-token-prefix-2')).not.toContainText('secret');

  await page.goto('/settings/personal/preferences');
  await page.goto('/settings/organization/api-keys');

  await expect(page.getByTestId('api-key-created')).toHaveCount(0);
  await expect(page.getByText('frisket_pat_new_secret')).toHaveCount(0);
  await expect(page.getByTestId('organization-api-keys-settings')).toContainText('automation token');
});
