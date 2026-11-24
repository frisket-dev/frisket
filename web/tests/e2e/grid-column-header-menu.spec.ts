import { isDeepStrictEqual } from 'node:util';

import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  gridReadSelection,
  importCsv,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

// The column ⋮ menu: Sort ascending/descending (and Clear sort when sorted) ·
// Filter… (opens the Filter sidebar on this column's facet) and Clear filter
// when filtered · Actions on this column (applicability metadata) ·
// settings/save-view.
test('grid column header menu sorts, filters through the sidebar facet, lists column actions, and opens settings', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-header-menu'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nSyracuse,open\nAlbany,closed\nBuffalo,open\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await clickHeaderMenu(page, columns, 'city');
  const menu = page.getByTestId('grid-column-header-menu');
  await expect(menu).toBeVisible();
  await expect(menu).toContainText('city');

  const cityId = columns.find((column) => column.name === 'city')!.id;
  const descResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    return isDeepStrictEqual(
      gridReadSelection(new URL(res.url()), pid, sheetId)?.order,
      [{ column_id: cityId, dir: 'desc' }],
    );
  });
  await page.getByTestId('header-menu-sort-desc').click();
  const sorted = await (await descResponse).json();
  const cityColumn = sorted.columns.find((column: { name: string }) => column.name === 'city');
  expect(cityColumn).toBeTruthy();
  expect(
    sorted.rows.map((row: { cells: Record<string, unknown> }) =>
      row.cells[String(cityColumn.id)],
    ),
  ).toEqual(['Syracuse', 'Buffalo', 'Albany']);
  await expect(page.getByTestId('row-filter-chip-sort')).toContainText('Sorted by city ↓');

  // Actions-on-this-column: a text column offers type-applicable actions that
  // open the drawer pre-bound (the clean/fetch fast-paths are gone).
  await clickHeaderMenu(page, columns, 'status');
  await expect(menu.getByTestId('header-menu-actions-section')).toBeVisible();
  await expect(menu.getByTestId('header-menu-action-map.clean_column')).toBeVisible();
  await expect(menu.getByTestId('header-menu-clean-column')).toHaveCount(0);
  await expect(menu.getByTestId('header-menu-fetch-url')).toHaveCount(0);

  // Filter… opens the sidebar on this column's facet.
  await page.getByTestId('header-menu-filter').click();
  const facet = page.getByTestId('friendly-facet-status');
  await expect(facet).toHaveAttribute('data-focused', 'true');
  const filterResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const selection = gridReadSelection(new URL(res.url()), pid, sheetId);
    return (selection?.filters.length ?? 0) > 0;
  });
  await facet.getByTestId('facet-check-status-open').check();
  const filtered = await (await filterResponse).json();
  const statusColumn = filtered.columns.find((column: { name: string }) => column.name === 'status');
  expect(statusColumn).toBeTruthy();
  expect(filtered.total).toBe(2);
  expect(
    filtered.rows.map((row: { cells: Record<string, unknown> }) =>
      row.cells[String(statusColumn.id)],
    ),
  ).toEqual(['open', 'open']);
  await expect(page.getByTestId('row-filter-chip-facet:status')).toContainText('status open');
  await expect(page.getByTestId('sheet-stats')).toHaveText('2 of 3 rows · 2 columns');

  // Clearing via the menu's Clear filter removes the filter.
  await clickHeaderMenu(page, columns, 'status');
  await page.getByTestId('header-menu-clear-filter').click();
  await expect(page.getByTestId('row-filter-chip-facet:status')).toHaveCount(0);

  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-column-settings').click();
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  await expect(page.getByTestId('column-format-select')).toBeVisible();

  await page.getByTestId('column-drawer').getByLabel('Close drawer').click();
  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-save-view').click();
  await expect(page.getByTestId('views-panel')).toBeVisible();
  await expect(page.getByTestId('view-name-input')).toHaveValue('city view');
});

test('grid column header menu freezes and unfreezes the left column prefix', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-freeze-menu'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status,owner\nSyracuse,open,Ada\nAlbany,closed,Grace\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  const grid = page.getByTestId('grid');
  await expect(grid).toBeVisible({ timeout: 15_000 });
  await expect(grid).toHaveAttribute('data-frozen-columns', '1');

  await clickHeaderMenu(page, columns, 'city');
  await expect(page.getByTestId('header-menu-unfreeze-columns')).toBeVisible();
  await page.getByTestId('header-menu-unfreeze-columns').click();
  await expect(grid).toHaveAttribute('data-frozen-columns', '0');

  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-freeze-columns').click();
  await expect(grid).toHaveAttribute('data-frozen-columns', '1');

  await clickHeaderMenu(page, columns, 'status');
  await page.getByTestId('header-menu-freeze-columns').click();
  await expect(grid).toHaveAttribute('data-frozen-columns', '2');

  await clickHeaderMenu(page, columns, 'owner');
  await page.getByTestId('header-menu-freeze-columns').click();
  await expect(grid).toHaveAttribute('data-frozen-columns', '3');
});
