// A sheet-level row-title override is settable from the column '...' menu's
// "Use as row title" (schema
// + server-persisted sheets.title_column_id). When unset, the default is the
// first column per the grid's CURRENT drag order (gridViewStore.
// columnOrderBySheet), falling back to canonical column order. ONE shared
// rowTitle helper (web/src/workbench/rowTitle.ts) drives both the row
// drawer's header and DocumentView's list; this spec pins the row drawer
// surface (drawer-header-shows-title) plus the two ways the title changes
// (set-from-menu, drag-reorder-renames).

import { expect, test, type Page } from '@playwright/test';
import {
  clickCell,
  clickHeaderMenu,
  createProject,
  importCsv,
  openProject,
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
): Promise<void> {
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

async function openRowDrawer(page: Page, columns: WireColumn[], columnName: string): Promise<void> {
  // All the columns this spec opens ('name', 'city', 'source') are plain
  // non-AI text columns, now in-place editable (grid-in-place-edit-v1), so
  // the floating icon — not Enter, which glide now routes to its own overlay
  // editor — opens the drawer.
  await clickCell(page, columns, columnName, 0);
  await page.getByTestId('cell-details-float').click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
}

async function seedSheet(page: Page, label: string): Promise<{ pid: string; sheetId: number; columns: WireColumn[] }> {
  const pid = await createProject(page.request, uniqueName(label));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'name,city,source\nAda,NYC,court\nGrace,Boston,council\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.map((c) => c.name)).toEqual(['name', 'city', 'source']);
  return { pid, sheetId, columns };
}

test('drawer-header-shows-title: the row drawer shows the resolved row title alongside the pinned ROW n kicker', async ({
  page,
}) => {
  const { pid, sheetId, columns } = await seedSheet(page, 'e2e-title-drawer');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await openRowDrawer(page, columns, 'name');
  // The kicker must stay exactly 'ROW 1': the title is a separate element and
  // does not repurpose it.
  await expect(page.getByTestId('inspect-detail-rownum')).toHaveText('ROW 1');
  // No explicit override anywhere -> default = first column per canonical
  // order ('name'), row 0's value.
  await expect(page.getByTestId('inspect-detail-row-title')).toHaveText('Ada');
});

test('set-from-menu: "Use as row title" persists the sheet-level override and the drawer reflects it after reload', async ({
  page,
}) => {
  const { pid, sheetId, columns } = await seedSheet(page, 'e2e-title-set');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await clickHeaderMenu(page, columns, 'city');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  const useAsRowTitle = page.getByTestId('header-menu-use-as-row-title');
  await expect(useAsRowTitle).toBeVisible();
  await expect(useAsRowTitle).toHaveAttribute('aria-pressed', 'false');
  const patchResponse = page.waitForResponse(
    (res) =>
      res.url().includes(`/api/projects/${pid}/sheets/${sheetId}`) &&
      res.request().method() === 'PATCH' &&
      res.status() === 200,
  );
  await useAsRowTitle.click();
  await patchResponse;

  await openRowDrawer(page, columns, 'name');
  await expect(page.getByTestId('inspect-detail-row-title')).toHaveText('NYC');

  // Server-persisted: survives reload, not just local UI state.
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await openRowDrawer(page, columns, 'name');
  await expect(page.getByTestId('inspect-detail-row-title')).toHaveText('NYC');

  // Re-opening the menu on the now-title column shows it disabled/pressed.
  await clickHeaderMenu(page, columns, 'city');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  await expect(page.getByTestId('header-menu-use-as-row-title')).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(page.getByTestId('header-menu-use-as-row-title')).toBeDisabled();

  // The backend round-trip: list_sheets reports the explicit override.
  const sheets = (await (await page.request.get(`/api/projects/${pid}/sheets`)).json()) as Array<{
    id: number;
    title_column_id: number | null;
  }>;
  const cityColumn = columns.find((c) => c.name === 'city')!;
  expect(sheets.find((s) => s.id === sheetId)?.title_column_id).toBe(cityColumn.id);
});

test('drag-reorder-renames: with no explicit override, dragging a column to the front changes the resolved title', async ({
  page,
}) => {
  const { pid, sheetId, columns } = await seedSheet(page, 'e2e-title-drag');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await openRowDrawer(page, columns, 'name');
  await expect(page.getByTestId('inspect-detail-row-title')).toHaveText('Ada');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('row-drawer')).toBeHidden();

  // Drag 'source' before 'name' -> grid order becomes [source, name, city].
  await dragHeaderBefore(page, columns, 'source', 'name');

  // Header drag is a layout preference, not a schema mutation -- canonical
  // column order (and title_column_id, still unset) is untouched.
  const backendColumns = await sheetColumns(page.request, pid, sheetId);
  expect(backendColumns.map((c) => c.name)).toEqual(['name', 'city', 'source']);
  const sheets = (await (await page.request.get(`/api/projects/${pid}/sheets`)).json()) as Array<{
    id: number;
    title_column_id: number | null;
  }>;
  expect(sheets.find((s) => s.id === sheetId)?.title_column_id).toBeNull();

  // The drawer's default title now follows the GRID's current order: 'source'.
  const reordered = [
    columns.find((c) => c.name === 'source')!,
    columns.find((c) => c.name === 'name')!,
    columns.find((c) => c.name === 'city')!,
  ];
  await openRowDrawer(page, reordered, 'name');
  await expect(page.getByTestId('inspect-detail-row-title')).toHaveText('court');
});
