// Display attributes: color/size the map by a chosen sheet column.
//
// Proves the Color-by / Size-by controls exist, that choosing one makes the map
// request the binary endpoint with attrs=<col_id>, and that the points still
// render (nonblank canvas) — i.e. the attribute pipeline is wired end to end.

import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

test('deck.gl map: color-by and size-by request attrs and keep the canvas nonblank', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('deckgl-attrs'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,category,size,point\n"Paris","A","10",\n"NYC","B","20",\n"Tokyo","A","30",\n',
  );
  let columns = await sheetColumns(page.request, pid, sheetId);
  const col = (name: string) => columns.find((c) => c.name === name)!;
  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=10`,
  );
  const rows = (await data.json()).rows as { id: number }[];
  // Three continental-US points (clustered → a normal zoom, no antimeridian
  // wrap), assigned by row order so the seed is deterministic.
  const coords = [
    { lat: 40.7128, lon: -74.006 }, // NYC
    { lat: 34.0522, lon: -118.2437 }, // LA
    { lat: 41.8781, lon: -87.6298 }, // Chicago
  ];
  await setColumnType(page.request, pid, col('point').id, 'geo_point');
  await setColumnType(page.request, pid, col('size').id, 'number');
  await editCells(
    page.request,
    pid,
    rows.map((r, i) => ({
      rowId: r.id,
      columnId: col('point').id,
      value: coords[i],
    })),
  );
  columns = await sheetColumns(page.request, pid, sheetId);

  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${col('point').id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('3 points');

  // Controls exist.
  const colorBy = page.getByTestId('map-color-by');
  const sizeBy = page.getByTestId('map-size-by');
  await expect(colorBy).toBeVisible();
  await expect(sizeBy).toBeVisible();
  // Size-by only offers the numeric column; color-by offers the category column.
  await expect(sizeBy.locator(`option[value="${col('size').id}"]`)).toHaveCount(1);
  await expect(colorBy.locator(`option[value="${col('category').id}"]`)).toHaveCount(1);

  // Choosing color-by category re-requests points with attrs=<category col id>.
  const colorReq = page.waitForRequest(
    (req) =>
      req.url().includes('/map/points') &&
      req.url().includes(`attrs=${col('category').id}`),
    { timeout: 12_000 },
  );
  await colorBy.selectOption(String(col('category').id));
  await colorReq;

  // A legend appears for the active color encoding, naming the column and its
  // categories (A and B from the seed).
  const legend = page.getByTestId('map-legend');
  await expect(legend).toBeVisible();
  await expect(legend).toContainText('category');
  await expect(legend).toContainText('A');
  await expect(legend).toContainText('B');

  // Choosing size-by sends the size column in the attrs list (precise check:
  // the attrs query param must actually contain the size column id).
  const sizeReq = page.waitForRequest((req) => {
    if (!req.url().includes('/map/points')) return false;
    const attrs = new URL(req.url()).searchParams.get('attrs');
    return attrs != null && attrs.split(',').includes(String(col('size').id));
  }, { timeout: 12_000 });
  await sizeBy.selectOption(String(col('size').id));
  await sizeReq;

  // Points still render after styling (nonblank canvas).
  await expect(page.getByTestId('map-points-count')).toHaveText('3 points');
  await page.waitForTimeout(400);
  const nonBg = await page.evaluate(() => {
    const c = document.querySelector(
      '[data-testid="map-canvas-container"] canvas',
    ) as HTMLCanvasElement | null;
    if (!c || !c.width || !c.height) return -1;
    const off = document.createElement('canvas');
    off.width = c.width;
    off.height = c.height;
    const ctx = off.getContext('2d');
    if (!ctx) return -1;
    ctx.drawImage(c, 0, 0);
    const { data } = ctx.getImageData(0, 0, off.width, off.height);
    let nonEmpty = 0;
    for (let i = 3; i < data.length; i += 4) if (data[i] > 0) nonEmpty++;
    return nonEmpty;
  });
  expect(nonBg).toBeGreaterThan(20);
});
