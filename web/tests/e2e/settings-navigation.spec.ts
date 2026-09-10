import { expect, test } from '@playwright/test';
import { openProject, projectIdByName } from './helpers';

test('personal settings direct URL and account redirect render the routed shell', async ({ page }) => {
  await page.goto('/settings/personal/profile');
  await expect(page.getByTestId('settings-workspace')).toBeVisible();
  await expect(page.getByTestId('settings-nav')).toContainText('Personal');
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();
  await expect(page.locator('.picker-card')).toHaveCount(0);
  await expect(page.locator('.account-screen')).toHaveCount(0);

  await page.goto('/account');
  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();
});

test('the Home account menu opens Personal Profile (no stale project hijack)', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await page.evaluate(() => {
    localStorage.setItem('frisket.settings.last_project.v1', JSON.stringify({
      id: 'stale',
      name: 'Stale Project',
    }));
    sessionStorage.setItem('frisket.settings.active_project.v1', 'stale');
  });
  // The retired picker's gear re-homed into the §5C account menu (Account
  // settings → Personal Profile); a stale last_project must not hijack it.
  await page.getByTestId('home-account').click();
  await page.getByTestId('account-settings').click();
  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByTestId('settings-workspace')).toBeVisible();
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();
  await expect(page.locator('[data-testid^="settings-nav-project-"]')).toHaveCount(0);
  await expect(page.getByTestId('settings-project-switcher')).toBeVisible();
  await expect(page.getByTestId('settings-return-project')).toHaveCount(0);
});

test('project menu settings entry opens Project General without mounting workspace hosts', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  // The retired sidebar footer's Settings link re-homed to the project ▾ menu
  // (workbench-ia-focus-v1; its other home is the ⌘K palette Open Settings).
  await page.getByTestId('switch-project').click();
  await page.getByTestId('project-menu').getByTestId('project-settings-open').click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/settings/project/general(?:\\?|$)`));
  await expect(page.getByTestId('settings-workspace')).toBeVisible();
  await expect(page.getByTestId('settings-section-project-general')).toBeVisible();

  await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
  await expect(page.getByTestId('sheet-tabs')).toHaveCount(0);
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);
  await expect(page.locator('[data-testid^="workbench-contribution-"]')).toHaveCount(0);
});

test('direct project settings route resolves project context without Workspace', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await page.goto(`/p/${pid}/settings/project/general`);

  await expect(page.getByTestId('settings-workspace')).toBeVisible();
  await expect(page.getByTestId('settings-section-project-general')).toContainText('Local stories');
  await expect(page.getByTestId('settings-section-project-general')).toContainText(pid);
  await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
  await expect(page.getByTestId('sheet-tabs')).toHaveCount(0);
});

test('project-prefixed personal settings route canonicalizes to the global route', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await page.goto(`/p/${pid}/settings/personal/profile`);

  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();
  await expect(page.locator('[data-testid^="settings-nav-project-"]')).toHaveCount(0);
  await expect(page.getByTestId('settings-project-switcher')).toBeVisible();
});

test('local tier renders hosted-only organization sections as disabled', async ({ page }) => {
  await page.goto('/settings/organization/ai-providers');

  await expect(page.getByTestId('settings-workspace')).toBeVisible();
  await expect(page.getByTestId('settings-disabled-organization-ai-providers')).toBeVisible();
  await expect(page.getByTestId('settings-disabled-organization-ai-providers')).toContainText(
    'Hosted organization settings are unavailable in local mode.',
  );
});

test('unscoped project settings route points users back to project selection', async ({ page }) => {
  await page.goto('/settings/project/general');

  const disabled = page.getByTestId('settings-disabled-project-general');
  await expect(disabled).toBeVisible();
  await expect(disabled).toContainText('Open a project to manage project settings.');
  await expect(disabled.getByRole('button', { name: 'Open a project' })).toBeVisible();
});

test('settings navigation preserves browser back and forward state', async ({ page }) => {
  await page.goto('/settings/personal/profile');
  await page.getByTestId('settings-nav-personal-preferences').click();

  await expect(page).toHaveURL(/\/settings\/personal\/preferences(?:\?|$)/);
  await expect(page.getByTestId('settings-section-personal-preferences')).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(/\/settings\/personal\/profile(?:\?|$)/);
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();

  await page.goForward();
  await expect(page).toHaveURL(/\/settings\/personal\/preferences(?:\?|$)/);
  await expect(page.getByTestId('settings-section-personal-preferences')).toBeVisible();
});
