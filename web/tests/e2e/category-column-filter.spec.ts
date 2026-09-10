import { expect, test } from '@playwright/test';
import {
  clickCell,
  clickHeader,
  createProject,
  importCsv,
  openDiscoverTab,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

test('category promotion prioritizes facets and a category pill filters in one click', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-category-filter'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'contracts.csv',
    'contract,status\nA,active\nB,expired\nC,active\n',
  );
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const columns = await sheetColumns(page.request, pid, sheetId);
  await clickHeader(page, columns, 'status');
  const drawer = page.getByTestId('column-drawer');
  await drawer.getByTestId('column-type-select').selectOption('category');
  await expect(drawer.getByTestId('column-type-description')).toContainText(
    'clickable exact-value facet',
  );
  await drawer.getByTestId('column-save-button').click();
  await expect(drawer.getByTestId('column-update-result')).toHaveText('Column updated.');
  await drawer.getByLabel('Close drawer').click();

  const promotedColumns = await sheetColumns(page.request, pid, sheetId);
  const status = promotedColumns.find((column) => column.name === 'status');
  expect(status?.type).toBe('category');

  await openDiscoverTab(page, 'Facets');

  const facets = page.getByTestId('friendly-filters-panel');
  await expect(facets.getByTestId('friendly-facet-status')).toContainText('2 values');
  await expect(facets.locator(':scope > section').first()).toHaveAttribute(
    'data-testid',
    'friendly-facet-status',
  );

  const filteredResponse = page.waitForResponse((response) => {
    if (response.request().method() !== 'GET') return false;
    const url = new URL(response.url());
    if (url.pathname !== `/api/projects/${pid}/sheets/${sheetId}/data`) return false;
    const filter = url.searchParams.get('filter');
    return filter !== null && JSON.stringify(JSON.parse(filter)) === JSON.stringify({
      status: { eq: 'active' },
    });
  });
  await clickCell(page, promotedColumns, 'status', 0);
  const filtered = await (await filteredResponse).json();
  expect(filtered.total).toBe(2);
  await expect(page.getByTestId('active-grid-filter')).toContainText('status eq active');
});
