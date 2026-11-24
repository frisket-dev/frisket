// The New project dialog sets Allowed services; the project then shows the
// setting, its pickers hide what the setting refuses, and loosening it in
// project settings asks first.

import { expect, test } from '@playwright/test';
import { importCsv, openAction, uniqueName } from './helpers';

test('a This machine only project hides remote models and loosens through a confirmation', async ({ page }) => {
  const name = uniqueName('e2e-this-machine');
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  await page.getByTestId('home-new-project').click();
  const dialog = page.getByTestId('new-project-dialog');
  await expect(dialog).toBeVisible();
  await expect(page.getByTestId('new-project-name')).toBeFocused();
  await page.getByTestId('new-project-name').fill(name);
  await dialog.getByRole('radio', { name: /This machine only/ }).check();
  await expect(page.getByTestId('new-project-preview-services')).toHaveText('This machine only');
  await expect(page.getByTestId('allowed-services-disclaimer')).toHaveText('Best-effort. Not network isolation.');
  await page.getByTestId('new-project-name').press('Enter');

  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 20_000 });
  const pid = decodeURIComponent(new URL(page.url()).pathname.split('/')[2] ?? '');
  expect(pid).not.toBe('');
  await expect(page.getByTestId('project-services-state')).toHaveText('This machine only');

  await importCsv(page.request, pid, 'stories.csv', 'headline\n"Transit service expanded"\n');
  await page.goto(`/p/${pid}`);
  await openAction(page, 'map.classify');
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  const selector = page.getByTestId('engine-selector-dialog');
  await expect(selector).toHaveJSProperty('open', true);
  await expect(selector.locator('[data-engine-selector-choice*="local_semantic"]')).toBeVisible();
  await expect(selector.locator('[data-engine-selector-choice*="openai"]')).toHaveCount(0);
  await expect(selector.getByText(/GPT/)).toHaveCount(0);
  await page.keyboard.press('Escape');

  await page.goto('/');
  await expect(page.getByTestId(`project-${pid}-services`)).toHaveText('This machine only');

  await page.goto(`/p/${pid}/settings/project/data-management`);
  const section = page.getByTestId('project-allowed-services-section');
  await expect(section.getByRole('radio', { name: /This machine only/ })).toBeChecked();
  // A looser value is not applied until the confirmation is accepted.
  await section.getByRole('radio', { name: /All services/ }).click();
  const confirm = page.getByTestId('allowed-services-confirm');
  await expect(confirm).toContainText('Change Allowed services from This machine only to All services?');
  await expect(section.getByRole('radio', { name: /This machine only/ })).toBeChecked();
  await page.getByTestId('allowed-services-confirm-button').click();
  await expect(confirm).toHaveCount(0);
  await expect(section.getByRole('radio', { name: /All services/ })).toBeChecked();

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('switch-project')).toContainText(name);
  await expect(page.getByTestId('project-services-state')).toHaveCount(0);
});
