// AccountMenu's "Keyboard shortcuts" item used to dispatch
// `frisket:show-shortcuts` to no listener. AccountMenu now takes an optional
// onOpenCommandPalette prop; ChromeBar
// (the workspace chrome bar, which has a real palette opener via
// chrome.openCommandPalette) passes it through, so the item opens the
// command palette (matching the act-menu's own help-shortcuts ->
// palette.open mapping, workspace/useWorkspaceModel.tsx:3054-3057). Home
// has no palette to open, so HomeScreen passes nothing and the item is
// absent there rather than being a dead button.

import { expect, test } from '@playwright/test';
import { openProject, projectIdByName } from './helpers';

test('Keyboard shortcuts opens the command palette in a workspace', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await page.getByTestId('chrome-account').click();
  const shortcutsItem = page.getByTestId('account-shortcuts');
  await expect(shortcutsItem).toBeVisible();
  await shortcutsItem.click();

  await expect(page.getByTestId('workbench-region-commandPalette')).toBeVisible();
  // The menu itself closes when the item is activated.
  await expect(page.getByTestId('account-menu')).toHaveCount(0);
});

test('Keyboard shortcuts item is absent on Home (no palette to open)', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  await page.getByTestId('home-account').click();
  await expect(page.getByTestId('account-menu')).toBeVisible();
  await expect(page.getByTestId('account-shortcuts')).toHaveCount(0);
});
