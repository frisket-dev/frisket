// Proves that a geo_point column exposes "Open map" (and a text column does not);
// the map loads points from the Arrow /map/points endpoint (not by fetching
// full grid rows); the WebGL canvas is provably nonblank after points load
// (a pixel sample, so a blank render fails); and clicking a point opens the
// existing row drawer for the correct stable row_id via deck picking.

import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu,
  mockBasemapTiles,
  openProject,
  seedGeoSheet,
  sheetColumns,
} from './helpers';

test('deck.gl map: open from geo_point column, nonblank canvas, point opens row', async ({
  page,
}) => {
  // --- seed: one row with a single valid geo_point (deterministic center) ---
  const { pid, sheetId, pointId } = await seedGeoSheet(page, {
    namePrefix: 'deckgl-map',
    point: { lat: 48.8584, lon: 2.2945 },
  });
  const point = { id: Number(pointId) };
  const columns = await sheetColumns(page.request, pid, sheetId);

  // Record map-points requests so we can prove the Arrow endpoint is used.
  const mapPointRequests: string[] = [];
  page.on('request', (req) => {
    if (req.url().includes('/map/points')) mapPointRequests.push(req.url());
  });
  const tiles = await mockBasemapTiles(page);

  // Surface any client-side image-decode/WebGL errors so a broken basemap fails
  // the test instead of only polluting the console.
  const pageErrors: string[] = [];
  page.on('console', (msg) => {
    if (
      msg.type() === 'error' &&
      /could not be decoded|WebGL|BitmapLayer|argument not a container/i.test(
        msg.text(),
      )
    ) {
      pageErrors.push(msg.text());
    }
  });

  await openProject(page, pid, sheetId);

  // --- a text column must NOT offer "Open map" ---
  await clickHeaderMenu(page, columns, 'place');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  await expect(page.getByTestId('header-menu-open-map')).toHaveCount(0);
  await page.keyboard.press('Escape');

  // --- the geo_point column offers "Open map" ---
  await clickHeaderMenu(page, columns, 'point');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  const openMap = page.getByTestId('header-menu-open-map');
  await expect(openMap).toBeVisible();
  await openMap.click();

  // --- the map surface loads from the Arrow endpoint and basemap tiles ---
  const split = page.getByTestId('workbench-mainView-split');
  await expect(split).toBeVisible();
  const gridContribution = page.getByTestId('workbench-contribution-frisket-core-view-grid');
  await expect(gridContribution).toBeVisible();
  await expect(gridContribution).toHaveAttribute('data-contribution-id', 'frisket.core.view.grid');
  await expect(gridContribution).toHaveAttribute('data-host', 'mainView');
  await expect(gridContribution).toHaveAttribute('data-mode', 'pane');

  const mapContribution = page.getByTestId('workbench-contribution-frisket-geo-view-map');
  await expect(mapContribution).toBeVisible();
  await expect(mapContribution).toHaveAttribute('data-contribution-id', 'frisket.geo.view.map');
  await expect(mapContribution).toHaveAttribute('data-host', 'mainView');
  await expect(mapContribution).toHaveAttribute('data-mode', 'pane');
  // Pin revision (geo-bundled-plugin-v1): the map view is a runtime plugin
  // contribution — its componentKey is the SDK-emitted
  // `<pluginId>.components.<export>` binding
  // (src/frisket/authoring/bundled_plugins/frisket.geo/plugin.config.mjs), not the old
  // first-party 'geo.views.MapView' registry key.
  await expect(mapContribution).toHaveAttribute(
    'data-runtime-component-key',
    'frisket.geo.components.MapView',
  );
  await expect(mapContribution).toHaveAttribute('data-required-capabilities', /projection\.status/);
  await expect(mapContribution).toHaveAttribute('data-required-capabilities', /grid\.filter\.applyBbox/);
  await expect(mapContribution).toHaveAttribute('data-required-capabilities', /host\.navigation\.openRow/);

  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');
  expect(
    mapPointRequests.some((u) => u.includes(`column_id=${point.id}`)),
  ).toBeTruthy();
  expect(mapPointRequests.some((u) => u.includes('format=arrow'))).toBeTruthy();
  await expect.poll(() => tiles.count()).toBeGreaterThan(0);

  // --- the WebGL canvas is nonblank (pixel sample catches a blank render) ---
  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  await expect(canvas).toBeVisible();
  // give deck.gl a couple of frames to paint
  await page.waitForTimeout(500);
  const nonBackgroundPixels = await page.evaluate(() => {
    const c = document.querySelector(
      '[data-testid="map-canvas-container"] canvas',
    ) as HTMLCanvasElement | null;
    if (!c || !c.width || !c.height) return -1;
    // drawImage captures the WebGL canvas's currently displayed bitmap into a
    // 2D canvas, so we can read pixels without preserveDrawingBuffer.
    const off = document.createElement('canvas');
    off.width = c.width;
    off.height = c.height;
    const ctx = off.getContext('2d');
    if (!ctx) return -1;
    ctx.drawImage(c, 0, 0);
    const { data } = ctx.getImageData(0, 0, off.width, off.height);
    // background is dark teal (~11,31,42); points are teal + white stroke.
    let nonBg = 0;
    for (let i = 0; i < data.length; i += 4) {
      const d =
        Math.abs(data[i] - 11) + Math.abs(data[i + 1] - 31) + Math.abs(data[i + 2] - 42);
      if (d > 60) nonBg++;
    }
    return nonBg;
  });
  expect(nonBackgroundPixels).toBeGreaterThan(20);

  // The basemap tiles must decode cleanly — no image-decode/WebGL console errors.
  expect(pageErrors).toEqual([]);

  // --- clicking the point opens the row drawer for the correct row ---
  // The single point sits at the view center (fit-to-bounds), so a center click
  // picks it; the click resolves index -> row_id and opens the drawer.
  await canvas.click();
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  await expect(drawer).toContainText('Eiffel Tower');
});
