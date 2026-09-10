// RED-FIRST (authored 2026-06-13): general sheet sorting is not implemented.
// The gate requires a grid sort control backed by GET /sheets/{id}/data so
// paging and saved views observe the same ordered row set.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAdvancedSortPanel,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

const ascSort = [{ column: 'city', dir: 'asc' }];
const descSort = [{ column: 'city', dir: 'desc' }];

test('grid sort: order rows by column through the sheet data endpoint', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-sort'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nSyracuse,open\nAlbany,closed\nBuffalo,open\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const columns = await sheetColumns(page.request, pid, sheetId);
  await openAdvancedSortPanel(page, columns, 'city');
  await page.getByTestId('grid-sort-column').selectOption('city');
  await page.getByTestId('grid-sort-direction').selectOption('asc');

  const ascResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
    const rawSort = url.searchParams.get('sort');
    return rawSort !== null && JSON.stringify(JSON.parse(rawSort)) === JSON.stringify(ascSort);
  });
  await page.getByTestId('apply-grid-sort').click();
  const asc = await (await ascResponse).json();
  const cityColumn = asc.columns.find((column: { name: string }) => column.name === 'city');
  expect(cityColumn).toBeTruthy();
  expect(
    asc.rows.map((row: { cells: Record<string, unknown> }) =>
      row.cells[String(cityColumn.id)],
    ),
  ).toEqual(['Albany', 'Buffalo', 'Syracuse']);
  await expect(page.getByTestId('active-grid-sort')).toContainText('city');

  await page.getByTestId('grid-sort-direction').selectOption('desc');
  const descResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
    const rawSort = url.searchParams.get('sort');
    return rawSort !== null && JSON.stringify(JSON.parse(rawSort)) === JSON.stringify(descSort);
  });
  await page.getByTestId('apply-grid-sort').click();
  const desc = await (await descResponse).json();
  expect(
    desc.rows.map((row: { cells: Record<string, unknown> }) =>
      row.cells[String(cityColumn.id)],
    ),
  ).toEqual(['Syracuse', 'Buffalo', 'Albany']);

  const clearedResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      !url.searchParams.has('sort')
    );
  });
  await page.getByTestId('clear-grid-sort').click();
  const cleared = await (await clearedResponse).json();
  expect(cleared.total).toBe(3);
});
