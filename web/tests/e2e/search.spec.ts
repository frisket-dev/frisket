// Project-wide search: ⌘K overlay, FTS results grouped by sheet, and result
// clicks that switch sheets and focus the matching grid cell.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  listSheets,
  openPalette,
  openProject,
  projectIdByName,
  sheetData,
  uniqueName,
} from './helpers';
import type { SearchHit, SheetRowLocation } from '../../src/api/open';

test('⌘K opens search; a seeded word returns grouped results', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await openProject(page, pid);

  await openPalette(page);

  // "listeria" appears in exactly one seeded snippet.
  await page.getByTestId('command-palette-input').fill('listeria');
  const results = page.getByTestId('search-results');
  await expect(results).toBeVisible();
  expect(await page.getByTestId('search-hit').count()).toBeGreaterThan(0);
  await expect(results.locator('.search-group-label').first()).toHaveText('stories');
  await expect(page.getByTestId('search-hit').first()).toContainText('listeria');

  // Esc closes the overlay.
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('workbench-region-commandPalette')).toBeHidden();
});

test('clicking a hit in another sheet switches to that sheet', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('search-cell-focus'));
  const startingSheetId = await importCsv(
    page.request,
    pid,
    'starting.csv',
    'name,story\nStart,"Nothing to find here"\n',
  );
  const targetRows = Array.from({ length: 620 }, (_, index) =>
    index === 517
      ? `"Record ${index}","Quayle is the cross-sheet search target"`
      : `"Record ${index}","Ordinary row ${index}"`,
  );
  const targetSheetId = await importCsv(
    page.request,
    pid,
    'targets.csv',
    ['name,story', ...targetRows].join('\n'),
  );
  const sheets = await listSheets(page.request, pid);
  const targetSheet = sheets.find((sheet) => sheet.id === targetSheetId);
  if (!targetSheet) throw new Error('target sheet missing');

  await openProject(page, pid, startingSheetId);
  await openPalette(page);
  const searchResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === `/api/projects/${pid}/search` && url.searchParams.get('q') === 'Quayle';
  });
  await page.getByTestId('command-palette-input').fill('Quayle');
  await expect(page.getByTestId('search-results')).toBeVisible();

  const hits = (await (await searchResponse).json()) as SearchHit[];
  const expectedHit = hits.find((hit) => String(hit.sheet_id) === String(targetSheet.id));
  if (!expectedHit) throw new Error('target search hit missing');
  const targetData = await sheetData(page.request, pid, targetSheet.id);
  const expectedColumnIndex = targetData.columns.findIndex(
    (column) => String(column.id) === String(expectedHit.column_id),
  );
  expect(expectedColumnIndex).toBeGreaterThanOrEqual(0);
  const locationResponse = await page.request.get(
    `/api/projects/${pid}/sheets/${targetSheet.id}/rows/${expectedHit.row_id}/locate?page_size=500`,
  );
  expect(locationResponse.ok()).toBeTruthy();
  const location = (await locationResponse.json()) as SheetRowLocation;
  expect(location.found).toBe(true);
  expect(location.index).not.toBeNull();

  // Pick the hit from the other sheet, well beyond the grid's first page.
  const targetGroup = page
    .locator('.search-group')
    .filter({ has: page.locator('.search-group-label', { hasText: targetSheet.name }) });
  await expect(targetGroup).toBeVisible();
  await targetGroup.getByTestId('search-hit').first().click();

  await expect(page.getByTestId('workbench-region-commandPalette')).toBeHidden();
  await expect(page.getByTestId(`workbench-mainView-tab-${targetSheet.id}`)).toHaveClass(/active/);
  await expect(page.locator('.sheet-title')).toHaveText(targetSheet.name);
  await expect.poll(() => page.evaluate(() => (
    window as unknown as {
      __frisketGridSelection?: { cell: readonly [number, number] | null };
    }
  ).__frisketGridSelection?.cell ?? null)).toEqual([expectedColumnIndex, location.index]);
});
