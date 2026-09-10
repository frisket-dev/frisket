// RED-FIRST (authored 2026-06-13): column drag/reorder should affect the active
// grid layout and saved views, not the canonical backend column schema order.

import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  openToolbarOverflow,
  sheetColumns,
  uniqueName,
  type WireColumn,
} from './helpers';

const MARKER_WIDTH = 40;
const HEADER_HEIGHT = 34;

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

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await dragHeaderBefore(page, backendColumns, 'source', 'status');

  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await expect(page.getByTestId('views-panel')).toBeVisible();
  await page.getByTestId('create-saved-view').click();
  await page.getByTestId('view-name-input').fill('Status first');
  const firstViewResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await page.getByTestId('save-view-button').click();
  const firstView = await (await firstViewResponse).json();
  expect(firstView.spec.columns).toEqual(['city', 'source', 'status']);

  // Header drag is a layout preference, not a schema mutation.
  expect((await sheetColumns(page.request, pid, sheetId)).map((column) => column.name)).toEqual([
    'city',
    'status',
    'source',
  ]);

  // The active/default layout persists across reloads in the current browser.
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await page.getByTestId('create-saved-view').click();
  await page.getByTestId('view-name-input').fill('Still source second');
  const secondViewResponse = page.waitForResponse((res) =>
    res.url().includes(`/api/projects/${pid}/views`) &&
    res.request().method() === 'POST' &&
    res.status() === 200,
  );
  await page.getByTestId('save-view-button').click();
  const secondView = await (await secondViewResponse).json();
  expect(secondView.spec.columns).toEqual(['city', 'source', 'status']);
});
