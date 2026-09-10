// RED-FIRST (authored 2026-07-07) for grid-add-column-v1: add columns from the
// grid, not just rows. A "+" affordance at the right end of the header row opens
// an inline name+type prompt that creates the column via the v1 column.add action
// (op-logged + undoable, no direct-DB write); the column ▾ caret also gains
// "Insert column left/right" (positioned). Undo removes the column; a column added
// while a catalog-driven drawer is open appears in its source-column selects.

import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  importCsv,
  openFriendlyFilterSidebar,
  openHistory,
  openProject,
  sheetColumns,
  uniqueName,
  type WireColumn,
} from './helpers';

async function visibleColumns(page: Page): Promise<string[]> {
  const attr = await page.getByTestId('grid').getAttribute('data-visible-column-names');
  return attr ? attr.split(',') : [];
}

async function addViaEndButton(page: Page, name: string, type?: string): Promise<void> {
  await page.getByTestId('grid-add-column-button').click();
  await expect(page.getByTestId('grid-add-column-popover')).toBeVisible();
  await page.getByTestId('grid-add-column-name').fill(name);
  if (type) await page.getByTestId('grid-add-column-type').selectOption(type);
  await page.getByTestId('grid-add-column-submit').click();
  await expect(page.getByTestId('grid-add-column-popover')).toBeHidden();
}

test('the header "+" creates a column immediately with the typed name/type via the v1 column.add action', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-add'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nAlbany,open\nBuffalo,closed\n',
  );
  const columnAddActions: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'column.add') columnAddActions.push(payload);
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  expect(await visibleColumns(page)).toEqual(['city', 'status']);

  await addViaEndButton(page, 'notes', 'text');
  await expect.poll(async () => visibleColumns(page)).toEqual(['city', 'status', 'notes']);

  // Went through the v1 action path (no direct-DB write), typed name + type.
  expect(columnAddActions).toHaveLength(1);
  expect(columnAddActions[0]).toMatchObject({
    action_id: 'column.add',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      sheet_id: Number(sheetId),
      name: 'notes',
      type: 'text',
    },
  });
  expect(columnAddActions[0]).not.toHaveProperty('kind');
  expect(columnAddActions[0]).not.toHaveProperty('capabilities');
  expect(String(columnAddActions[0]?.idempotency_key)).toMatch(/^web-column\.add:/);

  // The new column is a real schema column now (backend truth).
  const backend = await sheetColumns(page.request, pid, sheetId);
  expect(backend.map((column: WireColumn) => column.name)).toEqual(['city', 'status', 'notes']);
});

test('the caret "Insert column left/right" positions correctly and undo removes the added column', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-insert'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nAlbany,open\nBuffalo,closed\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openHistory(page);

  // Insert LEFT of "status" → lands between city and status.
  await clickHeaderMenu(page, columns, 'status');
  await page.getByTestId('header-menu-insert-column-left').click();
  await expect(page.getByTestId('grid-add-column-popover')).toBeVisible();
  await page.getByTestId('grid-add-column-name').fill('inserted');
  await page.getByTestId('grid-add-column-submit').click();
  await expect
    .poll(async () => (await sheetColumns(page.request, pid, sheetId)).map((c: WireColumn) => c.name))
    .toEqual(['city', 'inserted', 'status']);

  // Op-logged + undoable: undo removes the column.
  await expect(page.getByTestId('history-list')).toContainText('add column');
  await page.getByTestId('undo-button').click();
  await expect
    .poll(async () => (await sheetColumns(page.request, pid, sheetId)).map((c: WireColumn) => c.name))
    .toEqual(['city', 'status']);
  await expect.poll(async () => visibleColumns(page)).toEqual(['city', 'status']);
});

test('a column added while a catalog-driven drawer is open appears in its source-column selects', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-add-catalog'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status\nAlbany,open\nBuffalo,closed\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Open friendly facets, whose cards are populated from sheet.columns.
  await openFriendlyFilterSidebar(page, columns, 'city');
  await expect(page.getByTestId('friendly-facet-extra')).toHaveCount(0);

  // Add a column from the header "+" while the panel stays open.
  await addViaEndButton(page, 'extra', 'text');
  await expect.poll(async () => visibleColumns(page)).toContain('extra');

  // The sidebar re-reads sheet.columns and now offers the new facet.
  await expect(page.getByTestId('friendly-facet-extra')).toHaveCount(1);
});
