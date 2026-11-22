// Grid operations E2E suite: adding/inserting columns via v1 actions, hiding and
// reviving columns, column drag/reorder layout persistence, server-backed filtering,
// and server-backed sorting.

import { isDeepStrictEqual } from 'node:util';

import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  editCells,
  gridReadSelection,
  importCsv,
  openAdvancedSortPanel,
  openCellDrawer,
  openFriendlyFilterSidebar,
  openHistory,
  openProject,
  openToolbarOverflow,
  setColumnType,
  sheetColumns,
  uniqueName,
  type WireColumn,
} from './helpers';

const MARKER_WIDTH = 40;
const HEADER_HEIGHT = 34;

async function visibleColumns(page: Page): Promise<string[]> {
  const attr = await page.getByTestId('grid').getAttribute('data-visible-column-names');
  return attr ? attr.split(',') : [];
}

function visibleWire(columns: WireColumn[], hidden: string[]): WireColumn[] {
  const drop = new Set(hidden);
  return columns.filter((column) => !drop.has(column.name));
}

async function addViaEndButton(page: Page, name: string, type?: string): Promise<void> {
  await page.getByTestId('grid-add-column-button').click();
  await expect(page.getByTestId('grid-add-column-popover')).toBeVisible();
  await page.getByTestId('grid-add-column-name').fill(name);
  if (type) await page.getByTestId('grid-add-column-type').selectOption(type);
  await page.getByTestId('grid-add-column-submit').click();
  await expect(page.getByTestId('grid-add-column-popover')).toBeHidden();
}

async function hideColumn(page: Page, visible: WireColumn[], name: string): Promise<void> {
  await clickHeaderMenu(page, visible, name);
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  await page.getByTestId('header-menu-hide-column').click();
  await expect.poll(async () => visibleColumns(page)).not.toContain(name);
}

const columnWidth = (column: WireColumn): number => (column.type === 'text' ? 240 : 140);

function headerCenterX(columns: WireColumn[], name: string): number {
  let x = MARKER_WIDTH;
  for (const column of columns) {
    const width = columnWidth(column);
    if (column.name === name) return x + width / 2;
    x += width;
  }
  throw new Error(`missing column ${name}`);
}

async function dragHeaderBefore(
  page: Page,
  columns: WireColumn[],
  movedColumn: string,
  beforeColumn: string,
) {
  const box = await page.getByTestId('grid').boundingBox();
  if (!box) throw new Error('grid not visible');
  const targetColumn = columns.find((column) => column.name === beforeColumn);
  if (!targetColumn) throw new Error(`missing column ${beforeColumn}`);
  const fromX = box.x + headerCenterX(columns, movedColumn);
  const toX = box.x + headerCenterX(columns, beforeColumn) - columnWidth(targetColumn) / 2 + 8;
  const y = box.y + HEADER_HEIGHT / 2;
  await page.mouse.move(fromX, y);
  await page.mouse.down();
  await page.mouse.move(toX, y, { steps: 8 });
  await page.mouse.up();
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

  await clickHeaderMenu(page, columns, 'status');
  await page.getByTestId('header-menu-insert-column-left').click();
  await expect(page.getByTestId('grid-add-column-popover')).toBeVisible();
  await page.getByTestId('grid-add-column-name').fill('inserted');
  await page.getByTestId('grid-add-column-submit').click();
  await expect
    .poll(async () => (await sheetColumns(page.request, pid, sheetId)).map((c: WireColumn) => c.name))
    .toEqual(['city', 'inserted', 'status']);

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

  await openFriendlyFilterSidebar(page, columns, 'city');
  await expect(page.getByTestId('friendly-facet-extra')).toHaveCount(0);

  await addViaEndButton(page, 'extra', 'text');
  await expect.poll(async () => visibleColumns(page)).toContain('extra');

  await expect(page.getByTestId('friendly-facet-extra')).toHaveCount(1);
});

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
  await expect.poll(() => visibleColumns(page)).toEqual(['city', 'status', 'source']);

  await hideColumn(page, columns, 'status');
  await expect.poll(() => visibleColumns(page)).toEqual(['city', 'source']);

  await openCellDrawer(page, visibleWire(columns, ['status']), 'city', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-field-status')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('row-drawer')).toBeHidden();

  const chevron = page.getByTestId('grid-hidden-boundary');
  await expect(chevron).toBeVisible();
  await expect(chevron).toHaveAttribute('data-hidden-count', '1');
  await chevron.click();
  await expect.poll(() => visibleColumns(page)).toEqual(['city', 'status', 'source']);
  await expect(page.getByTestId('grid-hidden-boundary')).toHaveCount(0);

  await hideColumn(page, columns, 'status');
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await expect.poll(() => visibleColumns(page)).toEqual(['city', 'source']);
  await expect(page.getByTestId('grid-hidden-boundary')).toBeVisible();

  await openToolbarOverflow(page);
  await page.getByTestId('toolbar-show-hidden-columns').click();
  await expect.poll(() => visibleColumns(page)).toEqual(['city', 'status', 'source']);
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

  await hideColumn(page, columns, 'status');
  await hideColumn(page, visibleWire(columns, ['status']), 'source');
  await expect.poll(() => visibleColumns(page)).toEqual(['city', 'owner']);

  await clickHeaderMenu(page, visibleWire(columns, ['status', 'source']), 'owner');
  await expect(page.getByTestId('header-menu-unhide-columns')).toContainText('Unhide 2 columns');
  await page.keyboard.press('Escape');

  const chevron = page.getByTestId('grid-hidden-boundary');
  await expect(chevron).toHaveCount(1);
  await expect(chevron).toHaveAttribute('data-hidden-count', '2');
  await chevron.click();
  await expect.poll(() => visibleColumns(page)).toEqual(['city', 'status', 'source', 'owner']);
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
  const columns = await sheetColumns(page.request, pid, sheetId);

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  const mapSegment = page.getByTestId('view-switch-map');
  await expect(mapSegment).toBeVisible();
  await expect(mapSegment).toBeEnabled();

  await hideColumn(page, columns, 'point');
  await expect.poll(() => visibleColumns(page)).toEqual(['place']);

  await expect(mapSegment).toBeVisible();
  await expect(mapSegment).toBeEnabled();
});

test('grid column layout: drag reorder persists as default layout and saved-view columns', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-column-layout'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'city,status,source\nAlbany,open,court\nBuffalo,closed,council\n',
  );
  const backendColumns = await sheetColumns(page.request, pid, sheetId);
  expect(backendColumns.map((column) => column.name)).toEqual(['city', 'status', 'source']);
  const columnId = (name: string) => backendColumns.find((column) => column.name === name)!.id;
  const sourceSecond = [columnId('city'), columnId('source'), columnId('status')];

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await dragHeaderBefore(page, backendColumns, 'source', 'status');

  await openToolbarOverflow(page);
  await page.getByTestId('open-views-submenu').click();
  await page.getByTestId('views-overflow-open-panel').click();
  await expect(page.getByTestId('views-panel')).toBeVisible();
  await page.getByTestId('create-view').click();
  await page.getByTestId('view-name-input').fill('Status first');
  const firstViewResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await page.getByTestId('save-view-button').click();
  const firstView = await (await firstViewResponse).json();
  expect(firstView.spec.presentation.columns).toEqual(sourceSecond);

  expect((await sheetColumns(page.request, pid, sheetId)).map((column) => column.name)).toEqual([
    'city',
    'status',
    'source',
  ]);

  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openToolbarOverflow(page);
  await page.getByTestId('open-views-submenu').click();
  await page.getByTestId('views-overflow-open-panel').click();
  await page.getByTestId('create-view').click();
  await page.getByTestId('view-name-input').fill('Still source second');
  const secondViewResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await page.getByTestId('save-view-button').click();
  const secondView = await (await secondViewResponse).json();
  expect(secondView.spec.presentation.columns).toEqual(sourceSecond);
});

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
  const cityId = columns.find((column) => column.name === 'city')!.id;
  const orderedBy = (res: { url(): string }, dir: 'asc' | 'desc') => isDeepStrictEqual(
    gridReadSelection(new URL(res.url()), pid, sheetId)?.order,
    [{ column_id: cityId, dir }],
  );
  await openAdvancedSortPanel(page, columns, 'city');
  await page.getByTestId('grid-sort-column').selectOption('city');
  await page.getByTestId('grid-sort-direction').selectOption('asc');

  const ascResponse = page.waitForResponse((res) => (
    res.request().method() === 'GET' && orderedBy(res, 'asc')
  ));
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
  const descResponse = page.waitForResponse((res) => (
    res.request().method() === 'GET' && orderedBy(res, 'desc')
  ));
  await page.getByTestId('apply-grid-sort').click();
  const desc = await (await descResponse).json();
  expect(
    desc.rows.map((row: { cells: Record<string, unknown> }) =>
      row.cells[String(cityColumn.id)],
    ),
  ).toEqual(['Syracuse', 'Buffalo', 'Albany']);

  const clearedResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const selection = gridReadSelection(new URL(res.url()), pid, sheetId);
    return selection !== null && selection.order.length === 0;
  });
  await page.getByTestId('clear-grid-sort').click();
  const cleared = await (await clearedResponse).json();
  expect(cleared.total).toBe(3);
});
