// Opening personal settings from a project page must preselect that project
// instead of making the user choose it again.
//
// The fix threads the in-scope project through as a settings-project "nav
// param": AccountMenu.tsx's goPersonal() writes it via
// settings/settingsProjectContext.ts (write/read/clear — the SAME mechanism
// settings-navigation.spec.ts's stale-hijack test already exercises) before
// navigating, so SettingsWorkspace mounts with the originating project
// pre-selected instead of forcing a manual dropdown pick.

import { expect, test } from '@playwright/test';
import { openProject, projectIdByName } from './helpers';

test('personal settings opened from a project page auto-populates that project', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await page.getByTestId('chrome-account').click();
  await page.getByTestId('account-settings').click();

  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByTestId('settings-workspace')).toBeVisible();
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();

  // Auto-populated: the project switcher already shows the originating
  // project — no manual pick required.
  const switcher = page.getByTestId('settings-project-switcher');
  await expect(switcher).toHaveValue(pid);
});

test('personal preferences opened from a project page also auto-populates that project', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await page.getByTestId('chrome-account').click();
  await page.getByTestId('account-preferences').click();

  await expect(page).toHaveURL(/\/settings\/personal\/preferences(?:\?|$)/);
  await expect(page.getByTestId('settings-project-switcher')).toHaveValue(pid);
});

test('personal settings opened from Home (no project) leaves the dropdown for a manual pick', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  await page.getByTestId('home-account').click();
  await page.getByTestId('account-settings').click();

  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  const switcher = page.getByTestId('settings-project-switcher');
  await expect(switcher).toBeVisible();
  await expect(switcher).toHaveValue('');
});

test('switching projects from within a project then opening personal settings autofills the NEW project', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  // Simulate having left settings-project context stale from a prior visit —
  // the live chrome-account flow must overwrite it with the CURRENT project,
  // not merely fail to clear a stale one.
  await page.evaluate(() => {
    localStorage.setItem('frisket.settings.last_project.v1', JSON.stringify({
      id: 'stale-other-project',
      name: 'Stale Other Project',
    }));
    sessionStorage.setItem('frisket.settings.active_project.v1', 'stale-other-project');
  });

  await page.getByTestId('chrome-account').click();
  await page.getByTestId('account-settings').click();

  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByTestId('settings-project-switcher')).toHaveValue(pid);
});
