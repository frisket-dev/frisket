// Hardening pass (red-first) for the deck.gl map projection.
//
// Item 1: the map reflects the active grid filter and refreshes when it changes,
//         driven through the real UI.
// Item 4: bbox is exercised from the MAP path (viewport refetch), not only
//         backend unit tests.
// Item 6: the client Arrow decoder rejects malformed buffers instead of
//         rendering garbage.

import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  openFriendlyFilterSidebar,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

const MAP_POINTS_MEDIA_TYPE = 'application/vnd.apache.arrow.stream';

async function seedTwoPoints(page: import('@playwright/test').Page) {
  const pid = await createProject(page.request, uniqueName('deckgl-harden'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,status,point\n"Paris","open",\n"NYC","closed",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=10`,
  );
  const rows = (await data.json()).rows as { id: number; cells: Record<string, unknown> }[];
  const placeCol = columns.find((c) => c.name === 'place')!;
  const paris = rows.find((r) => r.cells[String(placeCol.id)] === 'Paris')!;
  const nyc = rows.find((r) => r.cells[String(placeCol.id)] === 'NYC')!;
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: paris.id, columnId: point.id, value: { lat: 48.8584, lon: 2.2945 } },
    { rowId: nyc.id, columnId: point.id, value: { lat: 40.7128, lon: -74.006 } },
  ]);
  const finalCols = await sheetColumns(page.request, pid, sheetId);
  return { pid, sheetId, point, columns: finalCols };
}

test('item 1: map reflects and refreshes on active grid filter change', async ({ page }) => {
  await mockBasemapTiles(page);
  const { pid, sheetId, columns } = await seedTwoPoints(page);
  await openProject(page, pid, sheetId);

  // No filter → both points.
  await clickHeaderMenu(page, columns, 'point');
  await page.getByTestId('header-menu-open-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('2 points');

  // Close the split back to the grid via the split header × (the map pane's own
  // "Back to grid" button was removed; splitfix), then apply a grid filter.
  await page.getByTestId('work-split-close').click();
  await expect(page.getByTestId('grid')).toBeVisible();
  await openFriendlyFilterSidebar(page, columns, 'status');
  await page.getByTestId('facet-check-status-open').check();
  await expect(page.getByTestId('active-grid-filter')).toContainText('status');

  // Reopen the map → it must reflect the now-active filter (Paris only).
  await clickHeaderMenu(page, columns, 'point');
  await page.getByTestId('header-menu-open-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');
});

test('item 4: zooming the map refetches points with a bbox', async ({ page }) => {
  await mockBasemapTiles(page);
  const { pid, sheetId, point } = await seedTwoPoints(page);
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText(/points/);

  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error('no canvas');
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;

  const bboxRequest = page.waitForRequest(
    (req) => req.url().includes('/map/points') && req.url().includes('bbox='),
    { timeout: 12_000 },
  );
  await page.mouse.move(cx, cy);
  await page.mouse.wheel(0, -600); // zoom in → viewport bbox refetch
  await bboxRequest;
});

test('item 6: client decoder rejects malformed Arrow buffers', async ({ page }) => {
  await mockBasemapTiles(page);
  const { pid, sheetId, point } = await seedTwoPoints(page);

  // Non-Arrow response with the Arrow media type → must surface an error, not
  // silently decode garbage.
  const notArrow = Buffer.from('not-arrow-ipc');
  await page.route(`**/sheets/${sheetId}/map/points*`, (route) =>
    route.fulfill({ status: 200, contentType: MAP_POINTS_MEDIA_TYPE, body: notArrow }),
  );
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-error')).toBeVisible();
  await page.unroute(`**/sheets/${sheetId}/map/points*`);

  // Short/malformed buffer → also an error, never a crash.
  const tooShort = Buffer.from([0x41, 0x52]); // "AR"
  await page.route(`**/sheets/${sheetId}/map/points*`, (route) =>
    route.fulfill({ status: 200, contentType: MAP_POINTS_MEDIA_TYPE, body: tooShort }),
  );
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-error')).toBeVisible();
});
