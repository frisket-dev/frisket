import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

test('Grid shows a loading state until its first row page arrives', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('grid-loading-state'));
  const firstSheetId = await importCsv(request, pid, 'first.csv', 'story\nAlready loaded\n');
  const sheetId = await importCsv(request, pid, 'stories.csv', 'story\nOne\nTwo\n');
  await openProject(page, pid, firstSheetId);

  let releaseRows!: () => void;
  const rowsReleased = new Promise<void>((resolve) => {
    releaseRows = resolve;
  });
  let markIntercepted!: () => void;
  const intercepted = new Promise<void>((resolve) => {
    markIntercepted = resolve;
  });
  let sawFirstRequest = false;
  await page.route(`**/api/projects/${pid}/sheets/${sheetId}/data**`, async (route) => {
    if (!sawFirstRequest) {
      sawFirstRequest = true;
      markIntercepted();
    }
    await rowsReleased;
    await route.continue();
  });

  try {
    await page.getByTestId(`workbench-mainView-tab-${sheetId}`).click();
    await intercepted;
    await expect(page.getByTestId('grid-data-loading')).toBeVisible();
    await expect(page.getByTestId('grid-loading')).toBeVisible();
  } finally {
    releaseRows();
  }

  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('grid-data-loading')).toHaveCount(0);
});
