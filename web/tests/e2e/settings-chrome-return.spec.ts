// Settings needs the same chrome as the rest of the application so the user
// can return to the active project.
//
// SettingsWorkspace.tsx now renders the SAME chrome-bar (workbench/
// ChromeBar.tsx's ChromeBarShell, data-testid="chrome-bar") every other
// surface uses, with a back link: "Back to {project}" when a project is in
// scope (settings-return-project), or "Back to projects" when it is not
// (settings-return-home — organization settings, or personal settings
// entered with no project context).

import { expect, test } from '@playwright/test';
import { openProject, projectIdByName } from './helpers';

test('settings opened from a project page carries the standard chrome with a way back', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await page.getByTestId('chrome-account').click();
  await page.getByTestId('account-settings').click();
  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);

  const chromeBar = page.getByTestId('chrome-bar');
  await expect(chromeBar).toBeVisible();
  const back = chromeBar.getByTestId('settings-return-project');
  await expect(back).toBeVisible();
  await expect(back).toContainText('Local stories');

  await back.click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}(?:/|\\?|$)`));
  await expect(page.getByTestId('workbench-shell')).toBeVisible();
});

test('the real Guide survives account-settings navigation and reload recovery', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await page.getByTestId('chrome-walkthrough').click();
  await page.getByTestId('walkthrough-choice-local-model-lab').click();
  for (let step = 0; step < 5; step += 1) {
    await page.getByRole('button', { name: 'Next' }).click();
  }
  await expect(page.getByRole('heading', { name: 'Open the account menu' })).toBeVisible();
  await page.getByTestId('chrome-account').click();
  await expect(page.getByRole('heading', { name: 'Open account settings' })).toBeVisible();
  await page.getByTestId('account-settings').click();

  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByRole('heading', { name: 'Open AI providers' })).toBeVisible();
  await expect(page.getByTestId('walkthrough-target-box')).toHaveAttribute(
    'data-target-testid',
    'settings-nav-personal-ai-providers',
  );

  await page.reload();
  await expect(page.getByTestId('walkthrough-card')).toHaveCount(0);
  await page.getByTestId('settings-return-project').click();
  await expect(page.getByTestId('chrome-walkthrough-resume')).toBeVisible();
  await page.getByTestId('chrome-walkthrough-resume').click();
  await expect(page.getByRole('heading', { name: 'Open AI providers' })).toBeVisible();
});

test('project settings carries the same chrome back link', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await page.getByTestId('switch-project').click();
  await page.getByTestId('project-menu').getByTestId('project-settings-open').click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/settings/project/general(?:\\?|$)`));

  const back = page.getByTestId('chrome-bar').getByTestId('settings-return-project');
  await expect(back).toBeVisible();
  await expect(back).toContainText('Local stories');
  await back.click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}(?:/|\\?|$)`));
  await expect(page.getByTestId('workbench-shell')).toBeVisible();
});

test('settings entered with no project in scope shows a back-to-projects fallback', async ({ page }) => {
  await page.goto('/settings/personal/profile');

  const chromeBar = page.getByTestId('chrome-bar');
  await expect(chromeBar).toBeVisible();
  await expect(chromeBar.getByTestId('settings-return-project')).toHaveCount(0);
  const backHome = chromeBar.getByTestId('settings-return-home');
  await expect(backHome).toBeVisible();

  await backHome.click();
  await expect(page).toHaveURL('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
});

test('Home account menu into personal settings keeps no stale back-to-project link', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await page.evaluate(() => {
    localStorage.setItem('frisket.settings.last_project.v1', JSON.stringify({
      id: 'stale',
      name: 'Stale Project',
    }));
    sessionStorage.setItem('frisket.settings.active_project.v1', 'stale');
  });
  await page.getByTestId('home-account').click();
  await page.getByTestId('account-settings').click();
  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);

  const chromeBar = page.getByTestId('chrome-bar');
  await expect(chromeBar.getByTestId('settings-return-project')).toHaveCount(0);
  await expect(chromeBar.getByTestId('settings-return-home')).toBeVisible();
});
