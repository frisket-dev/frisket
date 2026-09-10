// RED-FIRST (authored 2026-06-13): general sheet filtering is not implemented.
// The gate requires a grid filter control backed by GET /sheets/{id}/data so
// paging and saved views observe the same filtered row set.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openFriendlyFilterSidebar,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

const filterSpec = { status: { eq: 'open' } };

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
  await openFriendlyFilterSidebar(page, columns, 'status');

  const filteredResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
    const rawFilter = url.searchParams.get('filter');
    return rawFilter !== null && JSON.stringify(JSON.parse(rawFilter)) === JSON.stringify(filterSpec);
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
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      !url.searchParams.has('filter')
    );
  });
  await page.getByTestId('clear-grid-filter').click();
  const cleared = await (await clearedResponse).json();
  expect(cleared.total).toBe(4);
});
