// Home screen (§5A, replaces the retired ProjectPicker), deep links, and
// project switching chrome.

import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, projectIdByName, uniqueName } from './helpers';

test('the home screen lists the seeded projects', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  const list = page.getByTestId('project-list');
  await expect(list).toBeVisible();
  await expect(list.getByText('Local stories')).toBeVisible();
  await expect(list.getByText('Tariff impacts')).toBeVisible();
  await expect(list.getByText('Council audio')).toBeVisible();
});

test('creating a project lands in its empty workspace', async ({ page }) => {
  const name = uniqueName('e2e-picker');
  await page.goto('/');
  // + New project reveals the inline create form (§5A).
  await page.getByTestId('home-new-project').click();
  await page.getByTestId('new-project-name').fill(name);
  await page.getByTestId('create-project').click();
  // New project boots straight into the workspace: empty → import dropzone.
  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
  expect(page.url()).toMatch(/\/p\/[^/?#]+/);
});

test('deep link /p/{id} boots into the project', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('sheet-stats')).toHaveText(/8 rows/);
  await expect(page.getByTestId('workbench-mainView-tabs').getByText('stories')).toBeVisible();
});

test('project dropdown switches projects', async ({ page }) => {
  const stories = await projectIdByName(page.request, 'Local stories');
  const tariff = await projectIdByName(page.request, 'Tariff impacts');
  await openProject(page, stories);
  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  await page.getByTestId(`menu-project-${tariff}`).click();
  await page.waitForURL(`**/p/${tariff}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('switch-project')).toHaveText(/Tariff impacts/);
});

test('wordmark returns to the home screen', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);
  await page.getByTestId('brand-home').click();
  await expect(page.getByTestId('home-screen')).toBeVisible();
});

test('action deep link opens the requested action form', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-action-deeplink'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'addresses.csv',
    'address,state,country\n"1600 Pennsylvania Ave NW",DC,US\n',
  );
  await page.goto(`/p/${pid}/s/${sheetId}/action/enrich.geocode`);

  await expect(page.getByTestId('action-panel')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await expect(page.getByTestId('action-form-title')).toContainText('Geocode');
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${sheetId}/action/enrich.geocode(?:\\?|$)`));
});
