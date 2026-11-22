// deck.gl map E2E suite: opening from a geo_point column, Arrow /map/points loading,
// nonblank WebGL rendering, picking, color-by/size-by attributes, viewport bbox filtering,
// active grid filter sync, malformed Arrow rejection, heatmap toggle, and hover tooltips.

import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  editCells,
  gridReadSelection,
  importCsv,
  mockBasemapTiles,
  openFriendlyFilterSidebar,
  openProject,
  seedGeoSheet,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

const MAP_POINTS_MEDIA_TYPE = 'application/vnd.apache.arrow.stream';

async function seedTwoPoints(page: Page) {
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

test('deck.gl map: open from geo_point column, nonblank canvas, point opens row', async ({
  page,
}) => {
  const { pid, sheetId, pointId } = await seedGeoSheet(page, {
    namePrefix: 'deckgl-map',
    point: { lat: 48.8584, lon: 2.2945 },
  });
  const point = { id: Number(pointId) };
  const columns = await sheetColumns(page.request, pid, sheetId);

  const mapPointRequests: string[] = [];
  page.on('request', (req) => {
    if (req.url().includes('/map/points')) mapPointRequests.push(req.url());
  });
  const tiles = await mockBasemapTiles(page);

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

  await clickHeaderMenu(page, columns, 'place');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  await expect(page.getByTestId('header-menu-open-map')).toHaveCount(0);
  await page.keyboard.press('Escape');

  await clickHeaderMenu(page, columns, 'point');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  const openMap = page.getByTestId('header-menu-open-map');
  await expect(openMap).toBeVisible();
  await openMap.click();

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

  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  await expect(canvas).toBeVisible();
  await page.waitForTimeout(500);
  const nonBackgroundPixels = await page.evaluate(() => {
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
    let nonBg = 0;
    for (let i = 0; i < data.length; i += 4) {
      const d =
        Math.abs(data[i] - 11) + Math.abs(data[i + 1] - 31) + Math.abs(data[i + 2] - 42);
      if (d > 60) nonBg++;
    }
    return nonBg;
  });
  expect(nonBackgroundPixels).toBeGreaterThan(20);
  expect(pageErrors).toEqual([]);

  await canvas.click();
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  await expect(drawer).toContainText('Eiffel Tower');
});

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
  const coords = [
    { lat: 40.7128, lon: -74.006 },
    { lat: 34.0522, lon: -118.2437 },
    { lat: 41.8781, lon: -87.6298 },
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

  const colorBy = page.getByTestId('map-color-by');
  const sizeBy = page.getByTestId('map-size-by');
  await expect(colorBy).toBeVisible();
  await expect(sizeBy).toBeVisible();
  await expect(sizeBy.locator(`option[value="${col('size').id}"]`)).toHaveCount(1);
  await expect(colorBy.locator(`option[value="${col('category').id}"]`)).toHaveCount(1);

  const colorReq = page.waitForRequest(
    (req) =>
      req.url().includes('/map/points') &&
      req.url().includes(`attrs=${col('category').id}`),
    { timeout: 12_000 },
  );
  await colorBy.selectOption(String(col('category').id));
  await colorReq;

  const legend = page.getByTestId('map-legend');
  await expect(legend).toBeVisible();
  await expect(legend).toContainText('category');
  await expect(legend).toContainText('A');
  await expect(legend).toContainText('B');

  const sizeReq = page.waitForRequest((req) => {
    if (!req.url().includes('/map/points')) return false;
    const attrs = new URL(req.url()).searchParams.get('attrs');
    return attrs != null && attrs.split(',').includes(String(col('size').id));
  }, { timeout: 12_000 });
  await sizeBy.selectOption(String(col('size').id));
  await sizeReq;

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

test('deck.gl map: filter to this area applies a grid bbox filter', async ({ page }) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('deckgl-vpfilter'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,point\n"west",\n"center",\n"east",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  const placeId = columns.find((c) => c.name === 'place')!.id;
  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=10`,
  );
  const rows = (await data.json()).rows as { id: number; cells: Record<string, unknown> }[];
  const lonByPlace: Record<string, number> = { west: -40, center: 0, east: 40 };
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await editCells(
    page.request,
    pid,
    rows.map((r) => ({
      rowId: r.id,
      columnId: point.id,
      value: { lat: 0, lon: lonByPlace[String(r.cells[String(placeId)])] },
    })),
  );

  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('3 points');

  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error('no canvas');
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  const seededPoints = [
    { lon: -40, lat: 0 },
    { lon: 0, lat: 0 },
    { lon: 40, lat: 0 },
  ];
  let reachedCenterPoint = false;
  for (let gesture = 0; gesture < 4; gesture += 1) {
    const bboxResponsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname.endsWith(`/sheets/${sheetId}/map/points`) && url.searchParams.has('bbox');
    });
    await page.mouse.move(cx, cy);
    await page.mouse.wheel(0, -300);
    const bboxResponse = await bboxResponsePromise;
    expect(bboxResponse.ok()).toBeTruthy();
    expect(await bboxResponse.finished()).toBeNull();

    const rawBbox = new URL(bboxResponse.url()).searchParams.get('bbox');
    const bbox = rawBbox?.split(',').map(Number);
    expect(bbox).toHaveLength(4);
    const [minLon, minLat, maxLon, maxLat] = bbox!;
    const expectedPoints = seededPoints.filter(
      ({ lon, lat }) => minLon <= lon && lon <= maxLon && minLat <= lat && lat <= maxLat,
    ).length;
    await expect(page.getByTestId('map-points-count')).toHaveText(`${expectedPoints} points`);
    if (expectedPoints === 1) {
      reachedCenterPoint = true;
      break;
    }
  }
  expect(reachedCenterPoint).toBeTruthy();

  const dataResp = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const selection = gridReadSelection(new URL(res.url()), pid, sheetId);
    return selection?.filters.some((filter) => 'bbox' in filter.condition) ?? false;
  });
  await page.getByTestId('map-filter-viewport').click();
  const filtered = await (await dataResp).json();
  expect(filtered.total).toBe(1);

  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('active-grid-filter')).toContainText('in map area');
});

test('deck.gl map: reflects and refreshes on active grid filter change', async ({ page }) => {
  await mockBasemapTiles(page);
  const { pid, sheetId, columns } = await seedTwoPoints(page);
  await openProject(page, pid, sheetId);

  await clickHeaderMenu(page, columns, 'point');
  await page.getByTestId('header-menu-open-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('2 points');

  await page.getByTestId('work-split-close').click();
  await expect(page.getByTestId('grid')).toBeVisible();
  await openFriendlyFilterSidebar(page, columns, 'status');
  await page.getByTestId('facet-check-status-open').check();
  await expect(page.getByTestId('active-grid-filter')).toContainText('status');

  await clickHeaderMenu(page, columns, 'point');
  await page.getByTestId('header-menu-open-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');
});

test('deck.gl map: zooming the map refetches points with a bbox', async ({ page }) => {
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
  await page.mouse.wheel(0, -600);
  await bboxRequest;
});

test('deck.gl map: client decoder rejects malformed Arrow buffers', async ({ page }) => {
  await mockBasemapTiles(page);
  const { pid, sheetId, point } = await seedTwoPoints(page);

  const notArrow = Buffer.from('not-arrow-ipc');
  await page.route(`**/sheets/${sheetId}/map/points*`, (route) =>
    route.fulfill({ status: 200, contentType: MAP_POINTS_MEDIA_TYPE, body: notArrow }),
  );
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-error')).toBeVisible();
  await page.unroute(`**/sheets/${sheetId}/map/points*`);

  const tooShort = Buffer.from([0x41, 0x52]);
  await page.route(`**/sheets/${sheetId}/map/points*`, (route) =>
    route.fulfill({ status: 200, contentType: MAP_POINTS_MEDIA_TYPE, body: tooShort }),
  );
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-error')).toBeVisible();
});

test('deck.gl map: toggles points/heatmap and restores point picking', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('deckgl-heatmap'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,point\n"Eiffel Tower",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  const firstRow = (
    await (
      await page.request.get(`/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`)
    ).json()
  ).rows[0] as { id: number };
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: firstRow.id, columnId: point.id, value: { lat: 48.8584, lon: 2.2945 } },
  ]);

  const deckErrors: string[] = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error' && /deck:|WebGL|could not be decoded/i.test(msg.text())) {
      deckErrors.push(msg.text());
    }
  });

  await openProject(page, pid, sheetId);
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');

  const viewMode = page.getByTestId('map-view-mode');
  await expect(viewMode).toBeVisible();
  await viewMode.selectOption('heatmap');
  await page.waitForTimeout(600);
  expect(deckErrors, deckErrors.join('\n')).toEqual([]);

  await viewMode.selectOption('points');
  await page.waitForTimeout(300);
  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  await canvas.click();
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  await expect(drawer).toContainText('Eiffel Tower');
  expect(deckErrors, deckErrors.join('\n')).toEqual([]);
});

test('deck.gl map: hovering a point shows a tooltip with coords and attribute value', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('deckgl-tooltip'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,category,point\n"Eiffel Tower","Landmark",\n',
  );
  let columns = await sheetColumns(page.request, pid, sheetId);
  const col = (name: string) => columns.find((c) => c.name === name)!;
  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`,
  );
  const firstRow = (await data.json()).rows[0] as { id: number };
  await setColumnType(page.request, pid, col('point').id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: firstRow.id, columnId: col('point').id, value: { lat: 48.8584, lon: 2.2945 } },
  ]);
  columns = await sheetColumns(page.request, pid, sheetId);

  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${col('point').id}`);
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');

  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error('no canvas');
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;

  const tooltip = page.getByTestId('map-tooltip');
  await expect
    .poll(
      async () => {
        await page.mouse.move(cx, cy);
        return tooltip.isVisible();
      },
      { timeout: 8000 },
    )
    .toBe(true);
  await expect(tooltip).toContainText('48.85840, 2.29450');

  await page.getByTestId('map-color-by').selectOption(String(col('category').id));
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');
  await page.mouse.move(cx + 40, cy + 40);
  await expect
    .poll(
      async () => {
        await page.mouse.move(cx, cy);
        return (await tooltip.isVisible()) && (await tooltip.textContent());
      },
      { timeout: 8000 },
    )
    .toContain('Landmark');
});
