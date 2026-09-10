import { expect, test } from '@playwright/test';

test('account redirect opens Personal Profile settings and saves display name', async ({ page }) => {
  let displayName = 'Owner Person';
  await page.route('**/api/me/profile', async (route) => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as { display_name: string };
      displayName = body.display_name;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        email: 'owner@example.com',
        display_name: displayName,
        avatar_seed: 'user:owner@example.com',
      }),
    });
  });
  await page.route('**/api/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        email: 'owner@example.com',
        display_name: displayName,
        avatar_seed: 'user:owner@example.com',
      }),
    });
  });
  await page.route('**/api/column-types', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '[]',
  }));

  await page.goto('/account');

  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();
  await expect(page.getByTestId('personal-display-name')).toHaveValue('Owner Person');
  await page.getByTestId('personal-display-name').fill('Reporter One');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('Profile saved')).toBeVisible();
  expect(displayName).toBe('Reporter One');
});
