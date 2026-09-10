import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  importCsv,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

const descSort = [{ column: 'city', dir: 'desc' }];

// The column ▾ caret menu (workbench-ia-action-drawer-v1): Sort asc/desc ·
// Filter… (opens increment 6's inline filter row) · Actions on this column
// (applicability metadata) · settings/save-view. The old inline operator/value
// richer filter UI moved to the caret's Facets-sidebar row; the
// clean/fetch fast-paths became drawer-opening actions.
test('grid column header caret menu sorts, filters via the inline row, lists column actions, and opens settings', async ({
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

  const descResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
    const rawSort = url.searchParams.get('sort');
    return rawSort !== null && JSON.stringify(JSON.parse(rawSort)) === JSON.stringify(descSort);
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
  await expect(page.getByTestId('active-grid-sort')).toContainText('city desc');

  // Actions-on-this-column: a text column offers type-applicable actions that
  // open the drawer pre-bound (the clean/fetch fast-paths are gone).
  await clickHeaderMenu(page, columns, 'status');
  await expect(menu.getByTestId('header-menu-actions-section')).toBeVisible();
  await expect(menu.getByTestId('header-menu-action-map.clean_column')).toBeVisible();
  await expect(menu.getByTestId('header-menu-clean-column')).toHaveCount(0);
  await expect(menu.getByTestId('header-menu-fetch-url')).toHaveCount(0);

  // Filter… opens increment 6's inline filter row (contains) for this column.
  await page.getByTestId('header-menu-filter').click();
  const filterRow = page.getByTestId('inline-filter-row');
  await expect(filterRow).toBeVisible();
  await expect(page.getByTestId('inline-filter-column')).toHaveValue('status');
  const filterResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
    return url.searchParams.has('filter');
  });
  await page.getByTestId('inline-filter-input').fill('open');
  const filtered = await (await filterResponse).json();
  const statusColumn = filtered.columns.find((column: { name: string }) => column.name === 'status');
  expect(statusColumn).toBeTruthy();
  expect(filtered.total).toBe(2);
  expect(
    filtered.rows.map((row: { cells: Record<string, unknown> }) =>
      row.cells[String(statusColumn.id)],
    ),
  ).toEqual(['open', 'open']);
  await expect(page.getByTestId('active-grid-filter')).toContainText('status');

  // Clearing via the caret's Clear filter removes the filter.
  await clickHeaderMenu(page, columns, 'status');
  await page.getByTestId('header-menu-clear-filter').click();
  await expect(page.getByTestId('active-grid-filter')).toBeHidden();

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
