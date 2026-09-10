// RED-FIRST (authored 2026-07-07) for grid-column-hide-unhide-v1: Google-Sheets-
// style column collapse. HIDE via the column ▾ caret ("Hide column"); the column
// vanishes from the canvas but its data/schema are untouched (still in Detail,
// still satisfies data-keyed views like Map). REVIVE via the slim boundary
// chevron between the flanking visible columns (the whole hidden run revives
// together), the caret "Unhide N columns" on an adjacent column, or the toolbar
// ⋯ overflow "Show N hidden columns" fallback. State persists per project+sheet.

import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  editCells,
  importCsv,
  openCellDrawer,
  openProject,
  openToolbarOverflow,
  setColumnType,
  sheetColumns,
  uniqueName,
  type WireColumn,
} from './helpers';

async function visibleColumns(page: Page): Promise<string[]> {
  const attr = await page.getByTestId('grid').getAttribute('data-visible-column-names');
  return attr ? attr.split(',') : [];
}

function visibleWire(columns: WireColumn[], hidden: string[]): WireColumn[] {
  const drop = new Set(hidden);
  return columns.filter((column) => !drop.has(column.name));
}

async function hideColumn(page: Page, visible: WireColumn[], name: string): Promise<void> {
  await clickHeaderMenu(page, visible, name);
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  await page.getByTestId('header-menu-hide-column').click();
  await expect.poll(async () => visibleColumns(page)).not.toContain(name);
}

test('hide via caret removes the column from the grid but not from Detail; the boundary chevron revives it; state persists', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-hide'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status,source\nAlbany,open,court\nBuffalo,closed,council\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  expect(await visibleColumns(page)).toEqual(['city', 'status', 'source']);

  // HIDE the middle column via the caret.
  await hideColumn(page, columns, 'status');
  expect(await visibleColumns(page)).toEqual(['city', 'source']);

  // Hidden ≠ deleted: the Detail panel (data-keyed on sheet.columns) still shows
  // the hidden column's field.
  // 'city' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, visibleWire(columns, ['status']), 'city', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-field-status')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('row-drawer')).toBeHidden();

  // REVIVE via the boundary chevron between the two flanking visible columns.
  const chevron = page.getByTestId('grid-hidden-boundary');
  await expect(chevron).toBeVisible();
  await expect(chevron).toHaveAttribute('data-hidden-count', '1');
  await chevron.click();
  expect(await visibleColumns(page)).toEqual(['city', 'status', 'source']);
  await expect(page.getByTestId('grid-hidden-boundary')).toHaveCount(0);

  // Persist across reload (per project+sheet UI preference).
  await hideColumn(page, columns, 'status');
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  expect(await visibleColumns(page)).toEqual(['city', 'source']);
  await expect(page.getByTestId('grid-hidden-boundary')).toBeVisible();

  // The toolbar ⋯ overflow "Show N hidden columns" is the always-reachable fallback.
  await openToolbarOverflow(page);
  await page.getByTestId('toolbar-show-hidden-columns').click();
  expect(await visibleColumns(page)).toEqual(['city', 'status', 'source']);
});

test('a multi-column hidden run revives together from one boundary chevron; the caret Unhide item mirrors it', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-hide-run'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status,source,owner\nAlbany,open,court,A\nBuffalo,closed,council,B\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Hide two adjacent columns to form a single run between city and owner.
  await hideColumn(page, columns, 'status');
  await hideColumn(page, visibleWire(columns, ['status']), 'source');
  expect(await visibleColumns(page)).toEqual(['city', 'owner']);

  // The caret menu of an adjacent visible column offers "Unhide 2 columns".
  await clickHeaderMenu(page, visibleWire(columns, ['status', 'source']), 'owner');
  await expect(page.getByTestId('header-menu-unhide-columns')).toContainText('Unhide 2 columns');
  await page.keyboard.press('Escape');

  // One boundary chevron represents the whole run; clicking it revives both.
  const chevron = page.getByTestId('grid-hidden-boundary');
  await expect(chevron).toHaveCount(1);
  await expect(chevron).toHaveAttribute('data-hidden-count', '2');
  await chevron.click();
  expect(await visibleColumns(page)).toEqual(['city', 'status', 'source', 'owner']);
});

test('a hidden geo column still satisfies Map availability (data-keyed views read sheet.columns, not visible columns)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grid-hide-geo'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,point\n"Eiffel Tower",\n"Louvre",\n',
  );
  const initialColumns = await sheetColumns(page.request, pid, sheetId);
  const point = initialColumns.find((column) => column.name === 'point')!;
  await setColumnType(page.request, pid, point.id, 'geo_point');
  const data = await page.request.get(`/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=10`);
  const rows = (await data.json()).rows as Array<{ id: number }>;
  await editCells(page.request, pid, [
    { rowId: rows[0].id, columnId: point.id, value: { lat: 48.8584, lon: 2.2945 } },
  ]);
  // Re-fetch so the helper's header x-math uses the post-set-type geo_point width.
  const columns = await sheetColumns(page.request, pid, sheetId);

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  const mapSegment = page.getByTestId('view-switch-map');
  await expect(mapSegment).toBeVisible();
  await expect(mapSegment).toBeEnabled();

  // Hide the geo column via the caret; it leaves the grid but stays in the schema.
  await hideColumn(page, columns, 'point');
  expect(await visibleColumns(page)).toEqual(['place']);

  // Map remains available — availability is data-keyed on sheet.columns.
  await expect(mapSegment).toBeVisible();
  await expect(mapSegment).toBeEnabled();
});
