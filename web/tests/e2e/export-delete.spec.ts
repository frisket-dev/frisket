import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import {
  createProject,
  importCsv,
  listProjects,
  openProject,
  TINY_CSV,
  uniqueName,
} from './helpers';

async function createImportedProject(page: Page, prefix: string) {
  const name = uniqueName(prefix);
  const pid = await createProject(page.request, name);
  const sheetId = await importCsv(page.request, pid, 'tiny.csv', TINY_CSV);
  await openProject(page, pid, sheetId);
  return { name, pid, sheetId };
}

test('project settings downloads exports with and without media', async ({ page }) => {
  const { pid } = await createImportedProject(page, 'export-ui');

  // Export actions live in the Data management settings section
  // (ProjectDataManagementSettings, web/src/settings/SettingsSections.tsx);
  // the general section owns name/description/delete.
  await page.goto(`/p/${pid}/settings/project/data-management`);
  const withMediaDownload = page.waitForEvent('download');
  await page.getByTestId('export-project-with-media').click();
  const withMedia = await withMediaDownload;
  expect(withMedia.suggestedFilename()).toMatch(/\.frisket\.zip$/);
  expect(await withMedia.path()).toBeTruthy();

  await page.goto(`/p/${pid}/settings/project/data-management`);
  for (const testId of ['export-project-without-media']) {
    const downloadPromise = page.waitForEvent('download');
    await page.getByTestId(testId).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/\.frisket\.zip$/);
    expect(await download.path()).toBeTruthy();
  }
});

test('project data management downloads db-only export', async ({ page }) => {
  const { pid } = await createImportedProject(page, 'db-export-ui');

  await page.goto(`/p/${pid}/settings/project/data-management`);
  const downloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-project-database').click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.frisket\.db$/);
  const path = await download.path();
  expect(path).toBeTruthy();
  const bytes = await readFile(path!);
  expect(bytes.subarray(0, 15).toString('utf8')).toBe('SQLite format 3');
});

test('project delete is confirm-gated and removes the project after accept', async ({ page }) => {
  const { name, pid } = await createImportedProject(page, 'delete-ui');

  await page.goto(`/p/${pid}/settings/project/general`);
  await expect(page.getByTestId('project-delete-confirm-input')).toBeVisible();
  // The danger-zone button is labeled 'Delete' with a stable testid now
  // (project-delete-button, web/src/settings/SettingsSections.tsx).
  await expect(page.getByTestId('project-delete-button')).toBeDisabled();
  await page.getByTestId('project-delete-confirm-input').fill(`${name}-wrong`);
  await expect(page.getByTestId('project-delete-button')).toBeDisabled();
  expect((await listProjects(page.request)).some((p) => p.id === pid)).toBeTruthy();

  await page.getByTestId('project-delete-confirm-input').fill(name);
  await page.getByTestId('project-delete-button').click();

  await expect(page.getByTestId('home-screen')).toBeVisible({ timeout: 20_000 });
  expect((await listProjects(page.request)).some((p) => p.id === pid)).toBeFalsy();
});
