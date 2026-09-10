// RED-FIRST: summarized JSON list/table cells should activate like cells, not
// only behave as ordinary row-focus clicks.

import { expect, test } from '@playwright/test';
import {
  clickCell,
  createProject,
  dblclickCell,
  importCsv,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

async function retypeColumn(
  page: import('@playwright/test').Page,
  pid: string,
  columns: Awaited<ReturnType<typeof sheetColumns>>,
  name: string,
) {
  const column = columns.find((c) => c.name === name);
  expect(column).toBeTruthy();
  await setColumnType(page.request, pid, column!.id, 'json');
}

test('double-clicking an object-array summary cell opens the expanded table field', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-list-summary-dblclick'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'sponsors.csv',
    'episode,sponsors,notes\n' +
      '"morning show","[{""company"":""Acme Foods"",""coupon"":""ACME20"",""product"":""seltzer""},{""company"":""Northstar"",""coupon"":""STAR10"",""product"":""coffee""},{""company"":""BrightCo"",""coupon"":""BRIGHT"",""product"":""lamp""}]","three paid reads"\n',
  );
  let columns = await sheetColumns(page.request, pid, sheetId);
  await retypeColumn(page, pid, columns, 'sponsors');
  columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'sponsors')?.type).toBe('json');
  await openProject(page, pid, sheetId);

  await dblclickCell(page, columns, 'sponsors', 0);

  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  const field = page.getByTestId('row-field-sponsors');
  await expect(field).toHaveAttribute('data-selected-cell', 'true');
  await expect(field).toBeInViewport();
  await expect(field.getByTestId('json-mini-table')).toBeVisible();
  await expect(field.getByTestId('json-mini-table').locator('tbody tr')).toHaveCount(3);
  await expect(field).toContainText('Acme Foods');
});

test('pressing Enter on a selected scalar-array summary cell reopens the expanded list field', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-list-summary-enter'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'topics.csv',
    'title,topics,summary\n' +
      '"city budget","[""schools"",""transit"",""housing""]","budget hearing preview"\n',
  );
  let columns = await sheetColumns(page.request, pid, sheetId);
  await retypeColumn(page, pid, columns, 'topics');
  columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.find((c) => c.name === 'topics')?.type).toBe('json');
  await openProject(page, pid, sheetId);

  await clickCell(page, columns, 'topics', 0);
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);

  await page.keyboard.press('Enter');

  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  const field = page.getByTestId('row-field-topics');
  await expect(field).toHaveAttribute('data-selected-cell', 'true');
  await expect(field.getByTestId('json-list')).toBeVisible();
  await expect(field.getByTestId('json-list').locator('li')).toHaveCount(3);
  await expect(field).toContainText('transit');
});
