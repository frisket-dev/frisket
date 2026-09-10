// The Home screen's project-card ⋯
// menu (HomeScreen.tsx) gains a Rename entry that reuses the SAME additive
// PATCH /api/projects/{id} `updateProject` call the project-scope General
// settings page's Name field already rides (SettingsSections.tsx's
// ProjectGeneralSettings -> api.updateCurrentProject, both riding real.ts's
// shared PATCH endpoint) — no new API surface.

import { expect, test } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

test('the project card ⋯ menu offers Rename, which persists a new name via the shared PATCH endpoint', async ({
  page,
}) => {
  const originalName = uniqueName('e2e-rename-before');
  const pid = await createProject(page.request, originalName);

  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  const card = page.getByTestId(`project-${pid}`);
  await expect(card).toBeVisible();
  await expect(card.getByText(originalName)).toBeVisible();

  await page.getByTestId(`home-card-menu-${pid}`).click();
  const menu = page.locator('.home-card-menu').filter({ hasText: 'Rename' });
  await expect(menu).toBeVisible();
  await expect(page.getByTestId(`home-card-rename-${pid}`)).toBeVisible();
  await page.getByTestId(`home-card-rename-${pid}`).click();

  const input = page.getByTestId(`home-card-rename-input-${pid}`);
  await expect(input).toBeVisible();
  await expect(input).toHaveValue(originalName);

  const renamedName = uniqueName('e2e-rename-after');
  await input.fill(renamedName);
  const patched = page.waitForResponse(
    (res) => res.url().endsWith(`/api/projects/${pid}`) && res.request().method() === 'PATCH',
  );
  await page.getByTestId(`home-card-rename-save-${pid}`).click();
  await patched;

  // The card reflects the new name immediately (optimistic), and the input
  // form is gone.
  await expect(page.getByTestId(`home-card-rename-input-${pid}`)).toHaveCount(0);
  await expect(card.getByText(renamedName)).toBeVisible();
  await expect(card.getByText(originalName)).toHaveCount(0);

  // The rename round-trips through the real API — a reload still shows it.
  await page.reload();
  const reloadedCard = page.getByTestId(`project-${pid}`);
  await expect(reloadedCard).toBeVisible();
  await expect(reloadedCard.getByText(renamedName)).toBeVisible();
});

test('Escape cancels a rename without persisting it; the ⋯ menu closes when the rename starts', async ({
  page,
}) => {
  const originalName = uniqueName('e2e-rename-cancel');
  const pid = await createProject(page.request, originalName);

  await page.goto('/');
  const card = page.getByTestId(`project-${pid}`);
  await expect(card).toBeVisible();

  await page.getByTestId(`home-card-menu-${pid}`).click();
  await page.getByTestId(`home-card-rename-${pid}`).click();

  // Starting the rename dismisses the ⋯ menu (it isn't left open behind the form).
  await expect(page.locator('.home-card-menu')).toHaveCount(0);

  const input = page.getByTestId(`home-card-rename-input-${pid}`);
  await input.fill('should not be saved');
  await input.press('Escape');

  await expect(page.getByTestId(`home-card-rename-input-${pid}`)).toHaveCount(0);
  await expect(card.getByText(originalName)).toBeVisible();

  await page.reload();
  const reloadedCard = page.getByTestId(`project-${pid}`);
  await expect(reloadedCard.getByText(originalName)).toBeVisible();
});
