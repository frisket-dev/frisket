// Map "Color by" / "Size by" must actually re-style the deck.gl markers, not
// just populate the legend. The attribute values arrive on a SECOND fetch
// (selecting a column triggers a refetch with `attrs=`), and the marker
// accessors must be recomputed once those values land — otherwise the points
// keep their default fill/radius and "nothing happens".
//
// Color-by auto-picks the encoding from the column type: numeric → continuous
// ramp (dark-blue high end), string → categorical palette.
//
// These are pixel-sampling tests (like deckgl-map.spec.ts): a blank/unchanged
// render fails. Basemap tiles are mocked to a known light-gray so marker colors
// are distinguishable from the background.

import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

// Three well-separated points so fit-to-bounds shows all of them without
// overlap; radius is in screen pixels so it is independent of zoom.
const ROWS: Array<{ place: string; lat: number; lon: number; value: number; cat: string }> = [
  { place: 'New York', lat: 40.7484, lon: -73.9857, value: 1, cat: 'red' },
  { place: 'Denver', lat: 39.7392, lon: -104.9903, value: 50, cat: 'green' },
  { place: 'Los Angeles', lat: 34.0522, lon: -118.2437, value: 100, cat: 'blue' },
];

async function seedSheet(page: Page) {
  const pid = await createProject(page.request, uniqueName('map-style'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,point,value,cat\n' +
      ROWS.map((r) => `"${r.place}",,${r.value},${r.cat}`).join('\n') +
      '\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  const value = columns.find((c) => c.name === 'value')!;
  const cat = columns.find((c) => c.name === 'cat')!;

  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=10`,
  );
  const rows = (await data.json()).rows as Array<{ id: number; cells: Record<string, unknown> }>;
  // Map each seeded place to its row id via the imported "place" cell.
  const placeCol = columns.find((c) => c.name === 'place')!;
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await setColumnType(page.request, pid, value.id, 'number');
  await editCells(
    page.request,
    pid,
    rows.map((row) => {
      const seed = ROWS.find((r) => row.cells[String(placeCol.id)] === r.place)!;
      return { rowId: row.id, columnId: point.id, value: { lat: seed.lat, lon: seed.lon } };
    }),
  );

  return { pid, sheetId, point, value, cat };
}

/** Count canvas pixels close (sum of abs RGB diffs) to a target color. */
async function countPixelsNear(page: Page, target: [number, number, number], tol = 45) {
  return page.evaluate(
    ({ target, tol }) => {
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
      let n = 0;
      for (let i = 0; i < data.length; i += 4) {
        const d =
          Math.abs(data[i] - target[0]) +
          Math.abs(data[i + 1] - target[1]) +
          Math.abs(data[i + 2] - target[2]);
        if (d <= tol) n++;
      }
      return n;
    },
    { target, tol },
  );
}

// Default marker fill (GEO_FILL in MapView). Used to measure marker *coverage*
// independent of color: more teal pixels ⇒ larger markers.
const MARKER_TEAL: [number, number, number] = [29, 118, 110];

/** Count canvas pixels covered by the default teal marker fill. */
async function countMarkerPixels(page: Page) {
  return countPixelsNear(page, MARKER_TEAL, 40);
}

async function openMap(page: Page, pid: string, sheetId: number) {
  const tiles = await mockBasemapTiles(page);
  await openProject(page, pid, sheetId);
  const columns = await sheetColumns(page.request, pid, sheetId);
  await clickHeaderMenu(page, columns, 'point');
  await page.getByTestId('header-menu-open-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('3 points');
  // Wait for the basemap + markers to actually paint before sampling pixels.
  await expect.poll(() => tiles.count(), { timeout: 10000 }).toBeGreaterThan(0);
  await expect.poll(() => countMarkerPixels(page), { timeout: 10000 }).toBeGreaterThan(20);
}

test('Color by a numeric column applies a continuous gradient to the markers', async ({
  page,
}) => {
  const { pid, sheetId, value } = await seedSheet(page);
  await openMap(page, pid, sheetId);

  // Sanity: before color-by, no dark-blue (high-end ramp) pixels exist.
  await page.waitForTimeout(400);
  expect(await countPixelsNear(page, [8, 81, 156])).toBe(0);

  await page.getByTestId('map-color-by').selectOption(String(value.id));

  // Numeric column → continuous-ramp legend.
  const legend = page.getByTestId('map-legend');
  await expect(legend).toBeVisible();
  await expect(legend.locator('.map-legend-ramp')).toBeVisible();

  // The highest-value point must now render near the dark-blue ramp end.
  await expect
    .poll(async () => countPixelsNear(page, [8, 81, 156]), { timeout: 5000 })
    .toBeGreaterThan(15);
});

test('Color by a string column applies a categorical palette to the markers', async ({
  page,
}) => {
  const { pid, sheetId, cat } = await seedSheet(page);
  await openMap(page, pid, sheetId);

  await page.waitForTimeout(400);
  // First categorical palette color is orange [255,127,14]; absent by default.
  expect(await countPixelsNear(page, [255, 127, 14])).toBe(0);

  await page.getByTestId('map-color-by').selectOption(String(cat.id));

  const legend = page.getByTestId('map-legend');
  await expect(legend).toBeVisible();
  await expect(legend.locator('.map-legend-swatch').first()).toBeVisible();

  await expect
    .poll(async () => countPixelsNear(page, [255, 127, 14]), { timeout: 5000 })
    .toBeGreaterThan(15);
});

test('Size by a numeric column enlarges the markers', async ({ page }) => {
  const { pid, sheetId, value } = await seedSheet(page);
  await openMap(page, pid, sheetId);

  await page.waitForTimeout(400);
  const before = await countMarkerPixels(page);
  expect(before).toBeGreaterThan(0);

  await page.getByTestId('map-size-by').selectOption(String(value.id));

  // The largest-value point grows to the max radius, so total marker coverage
  // must visibly increase once the size accessor is recomputed.
  await expect
    .poll(async () => countMarkerPixels(page), { timeout: 5000 })
    .toBeGreaterThan(before * 1.3);
});
