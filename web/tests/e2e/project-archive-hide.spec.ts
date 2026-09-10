// Archiving a project is a
// server-persisted flag (PATCH /api/projects/{id} { archived }, the same
// additive endpoint project-rename-from-list-v1's rename rides — see
// HomeScreen.tsx's setFlag / real.ts's "Home screen's Star/Archive flags and
// rename ride the same" comment). The default Home scope excludes archived
// projects (HomeScreen.tsx's `scoped` filter: `!p.archived`); the left-nav
// Archive item is the light affordance that reveals them; the card ⋯ menu's
// Archive/Unarchive entry (home-card-archive-<id>, same pattern as
// home-card-rename-<id>) is the round-trip control.

import { expect, test } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

test('archiving a project from the card ⋯ menu hides it from Home, surfaces it under Archive, and unarchiving restores it', async ({
  page,
}) => {
  const name = uniqueName('e2e-archive-hide');
  const pid = await createProject(page.request, name);

  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();

  // Starts visible in the default Home scope.
  await expect(page.getByTestId(`project-${pid}`)).toBeVisible();

  // Archive it via the card ⋯ menu — the server-persisted flag round-trips
  // through the same PATCH endpoint rename uses.
  await page.getByTestId(`home-card-menu-${pid}`).click();
  const menu = page.locator('.home-card-menu').filter({ hasText: 'Archive' });
  await expect(menu).toBeVisible();
  const archived = page.waitForResponse(
    (res) => res.url().endsWith(`/api/projects/${pid}`) && res.request().method() === 'PATCH',
  );
  await page.getByTestId(`home-card-archive-${pid}`).click();
  await archived;

  // Gone from the default Home list.
  await expect(page.getByTestId(`project-${pid}`)).toHaveCount(0);

  // The flag survives a reload (server-persisted, not just optimistic local
  // state) — still absent from Home after a fresh fetch.
  await page.reload();
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await expect(page.getByTestId(`project-${pid}`)).toHaveCount(0);

  // Visible under the Archive scope (the light affordance), with its
  // "archived" chip.
  await page.getByTestId('home-nav-archive').click();
  const archivedCard = page.getByTestId(`project-${pid}`);
  await expect(archivedCard).toBeVisible();
  await expect(archivedCard.getByText('archived')).toBeVisible();

  // Unarchive from the same ⋯ menu entry (now labeled Unarchive).
  await page.getByTestId(`home-card-menu-${pid}`).click();
  const unarchiveMenu = page.locator('.home-card-menu').filter({ hasText: 'Unarchive' });
  await expect(unarchiveMenu).toBeVisible();
  const unarchived = page.waitForResponse(
    (res) => res.url().endsWith(`/api/projects/${pid}`) && res.request().method() === 'PATCH',
  );
  await page.getByTestId(`home-card-archive-${pid}`).click();
  await unarchived;

  // Gone from the Archive scope…
  await expect(page.getByTestId(`project-${pid}`)).toHaveCount(0);

  // …and back in the default Home scope, surviving a reload.
  await page.getByTestId('home-nav-home').click();
  await expect(page.getByTestId(`project-${pid}`)).toBeVisible();
  await page.reload();
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await expect(page.getByTestId(`project-${pid}`)).toBeVisible();
});

test('the project ▾ menu\'s Archive project entry marks the project archived and returns Home', async ({
  page,
}) => {
  const name = uniqueName('e2e-archive-topnav');
  const pid = await createProject(page.request, name);

  await page.goto(`/p/${pid}`);
  await expect(
    page.getByTestId('grid').or(page.getByTestId('import-dropzone')).first(),
  ).toBeVisible({ timeout: 20_000 });

  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  const archived = page.waitForResponse(
    (res) => res.url().endsWith(`/api/projects/${pid}`) && res.request().method() === 'PATCH',
  );
  await page.getByTestId('project-archive').click();
  await archived;

  // Archiving returns Home, where the project is excluded from the default
  // scope (same flag the card-menu path sets).
  await expect(page.getByTestId('home-screen')).toBeVisible();
  await expect(page.getByTestId(`project-${pid}`)).toHaveCount(0);
  await page.getByTestId('home-nav-archive').click();
  await expect(page.getByTestId(`project-${pid}`)).toBeVisible();
});
