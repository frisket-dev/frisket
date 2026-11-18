// RED-FIRST (authored 2026-06-13): general sheet filtering is not implemented.
// The gate requires a grid filter control backed by GET /sheets/{id}/data so
// paging and views observe the same filtered row set.

import { isDeepStrictEqual } from 'node:util';

import { expect, test } from '@playwright/test';
import {
  createProject,
  gridReadSelection,
  importCsv,
  openFriendlyFilterSidebar,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

test('long filter values stay intact in queries but have a bounded summary', async ({ page }) => {
  const value = 'DocumentText'.repeat(100);
  const pid = await createProject(page.request, uniqueName('e2e-long-filter'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', `body\n"${value}"\nOther\n`);
  await openProject(page, pid, sheetId);
  const columns = await sheetColumns(page.request, pid, sheetId);
  const bodyId = columns.find((column) => column.name === 'body')!.id;
  await openFriendlyFilterSidebar(page, columns, 'body');
  const response = page.waitForResponse((res) => {
    const selection = gridReadSelection(new URL(res.url()), pid, sheetId);
    return selection?.filters.some((filter) => (
      filter.column_id === bodyId && filter.condition.eq === value
    )) ?? false;
  });
  await page.getByTestId('facet-values-body').getByRole('checkbox').first().check();
  expect((await (await response).json()).total).toBe(1);
  const summary = page.getByTestId('active-grid-filter');
  await expect(summary).toContainText(`${value.slice(0, 79)}…`);
  await summary.evaluate((element) => { element.style.maxWidth = '150px'; });
  const dimensions = await summary.locator('.active-grid-filter-label').evaluate((element) => ({
    height: element.getBoundingClientRect().height,
    lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight),
    totalHeight: element.scrollHeight,
  }));
  expect(dimensions.totalHeight).toBeGreaterThan(dimensions.height);
  expect(dimensions.height).toBeLessThanOrEqual(dimensions.lineHeight * 3 + 1);
  await expect(page.getByTestId('clear-grid-filter')).toBeVisible();
  await page.getByTestId('clear-grid-filter').click();
  await expect(summary).toHaveCount(0);
});

test('grid filter: filter rows by column value through the sheet data endpoint', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-filter'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nAlbany,open\nBuffalo,closed\nAlbany,closed\nSyracuse,open\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const columns = await sheetColumns(page.request, pid, sheetId);
  const statusId = columns.find((column) => column.name === 'status')!.id;
  await openFriendlyFilterSidebar(page, columns, 'status');

  const filteredResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const selection = gridReadSelection(new URL(res.url()), pid, sheetId);
    return isDeepStrictEqual(selection?.filters, [
      { column_id: statusId, condition: { eq: 'open' } },
    ]);
  });
  await page.getByTestId('facet-check-status-open').check();
  const filtered = await (await filteredResponse).json();

  const statusColumn = filtered.columns.find((column: { name: string }) => column.name === 'status');
  expect(statusColumn).toBeTruthy();
  expect(filtered.total).toBe(2);
  expect(
    filtered.rows.map((row: { cells: Record<string, unknown> }) =>
      row.cells[String(statusColumn.id)],
    ),
  ).toEqual(['open', 'open']);
  await expect(page.getByTestId('active-grid-filter')).toContainText('status');

  const clearedResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const selection = gridReadSelection(new URL(res.url()), pid, sheetId);
    return selection !== null && selection.filters.length === 0;
  });
  await page.getByTestId('clear-grid-filter').click();
  const cleared = await (await clearedResponse).json();
  expect(cleared.total).toBe(4);
});
