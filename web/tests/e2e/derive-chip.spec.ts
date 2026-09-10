// Child-count chip: parent sheets that have a derived child
// sheet get a trailing chip column showing each row's derived-row count;
// clicking the chip jumps to the child sheet FILTERED to that parent's
// children (parent_row_id on /data), with a banner that clears the filter.
// Read-only against the seeded "People mentioned" project (no model calls).

import { expect, test, type APIRequestContext } from '@playwright/test';
import {
  openProject,
  projectIdByName,
  listSheets,
  type WireColumn,
} from './helpers';

const MARKER_WIDTH = 40;
const HEADER_HEIGHT = 34;
const ROW_HEIGHT = 34;
const CHIP_WIDTH = 130; // SheetGrid CHILD_CHIP_COL default
const colWidth = (c: WireColumn): number => (c.type === 'text' ? 240 : 140);

async function seededParentWithChildRows(request: APIRequestContext) {
  const pid = await projectIdByName(request, 'People mentioned');
  const sheets = await listSheets(request, pid);
  const child = sheets.find((s) => s.parent_sheet_id !== null);
  expect(child, 'seeded derive project has a child sheet').toBeTruthy();
  const parent = sheets.find((s) => s.id === child!.parent_sheet_id)!;
  const data = (await (
    await request.get(`/api/projects/${pid}/sheets/${parent.id}/data?offset=0&limit=20`)
  ).json()) as {
    columns: WireColumn[];
    rows: Array<{ id: number; child_count?: number }>;
  };
  const rowIdx = data.rows.findIndex((r) => (r.child_count ?? 0) > 0);
  expect(rowIdx, 'a seeded parent row has child rows').toBeGreaterThanOrEqual(0);
  return { pid, parent, child: child!, data, rowIdx, parentRow: data.rows[rowIdx] };
}

test('child-count chip filters the child sheet to one parent row', async ({
  page,
  request,
}) => {
  const { pid, parent, child, data, rowIdx, parentRow } = await seededParentWithChildRows(request);
  const count = parentRow.child_count!;

  await openProject(page, pid, parent.id);

  // The chip column is appended after the real columns; click it on the
  // parent row (canvas grid → coordinate click).
  const box = await page.getByTestId('grid').boundingBox();
  expect(box).toBeTruthy();
  const chipX = MARKER_WIDTH + data.columns.reduce((a, c) => a + colWidth(c), 0) + CHIP_WIDTH / 2;
  const filteredFetch = page.waitForRequest((r) =>
    r.url().includes(`parent_row_id=${parentRow.id}`),
  );
  await page.mouse.click(
    box!.x + chipX,
    box!.y + HEADER_HEIGHT + rowIdx * ROW_HEIGHT + ROW_HEIGHT / 2,
  );

  // Click-through: the child sheet opens, data is fetched with the
  // parent_row_id filter, and the banner reports the filtered count.
  await filteredFetch;
  await expect(page.getByTestId('sheet-breadcrumb')).toBeVisible();
  const banner = page.getByTestId('child-filter-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(String(count));
  await expect(banner).toContainText(parent.name);

  // Clearing the filter keeps the child sheet open, full row count restored.
  await page.getByTestId('child-filter-clear').click();
  await expect(banner).not.toBeVisible();
  await expect(page.getByTestId('sheet-stats')).toContainText(
    `${child.rows.toLocaleString()} rows`,
  );
});

test('keyboard selection of the child-count chip waits for explicit activation', async ({
  page,
  request,
}) => {
  const { pid, parent, data, rowIdx, parentRow } = await seededParentWithChildRows(request);
  const count = parentRow.child_count!;

  await openProject(page, pid, parent.id);

  const box = await page.getByTestId('grid').boundingBox();
  expect(box).toBeTruthy();
  const lastColumn = data.columns[data.columns.length - 1];
  expect(lastColumn).toBeTruthy();
  const beforeLastWidth = data.columns
    .slice(0, -1)
    .reduce((total, column) => total + colWidth(column), 0);
  const lastRealColumnX = MARKER_WIDTH + beforeLastWidth + colWidth(lastColumn!) / 2;
  const rowY = HEADER_HEIGHT + rowIdx * ROW_HEIGHT + ROW_HEIGHT / 2;
  await page.mouse.click(box!.x + lastRealColumnX, box!.y + rowY);
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('row-drawer')).toBeVisible();

  let filteredRequests = 0;
  page.on('request', (route) => {
    if (route.url().includes(`parent_row_id=${parentRow.id}`)) filteredRequests += 1;
  });
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(250);
  expect(filteredRequests).toBe(0);
  await expect(page.getByTestId('child-filter-banner')).toHaveCount(0);

  await page.keyboard.press('Enter');
  await expect.poll(() => filteredRequests).toBeGreaterThan(0);
  const banner = page.getByTestId('child-filter-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(String(count));
  await expect(banner).toContainText(parent.name);
});
