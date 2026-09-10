// "Filter to this area": the map viewport becomes a grid bbox filter (one
// canonical filter contract — the grid /data re-fetch honors the geo-bbox op).

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
  // Three points on the equator; their midpoint is exactly "center" (lon 0), so
  // zooming in on the fitted center keeps "center" and drops "west"/"east".
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

  // Zoom in on the center incrementally until the map's own bbox refetch drops
  // west/east, leaving just the center point in view. Keep gestures bounded and
  // let each matching bbox response settle in the UI before continuing: a fast
  // polling loop would replace the active bbox while its prior response was
  // still decoding, so no result could become visible.
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

  // "Filter to this area" → return to grid; /data re-fetches with the bbox filter,
  // matching only the in-view point.
  const dataResp = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      (url.searchParams.get('filter') ?? '').includes('bbox')
    );
  });
  await page.getByTestId('map-filter-viewport').click();
  const filtered = await (await dataResp).json();
  expect(filtered.total).toBe(1);

  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('active-grid-filter')).toContainText('in map area');
});
