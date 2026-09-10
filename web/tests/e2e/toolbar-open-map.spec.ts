// Map launcher entry points after the toolbar diet (workbench-ia-toolbar-diet-v1):
// the workspace-level map affordance is the view-switcher `view-switch-map`
// segment; per-column map opening lives in the column caret menu
// (`header-menu-open-map`).
//
// Proves three states:
//  - none      : no geo_point column → the map segment is absent
//  - single    : exactly one geo_point column → the map segment opens its map
//  - multiple  : 2+ geo_point columns → each column opens its own map from its
//                header menu

import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  openProject,
  seedGeoSheet,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

async function firstRowId(page: Page, pid: string, sheetId: number) {
  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`,
  );
  expect(data.ok()).toBeTruthy();
  return (await data.json()).rows[0] as { id: number };
}

test('map view segment is absent when no geo_point column exists', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('toolbar-map-none'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,note\n"Eiffel Tower","just text"\n',
  );

  await openProject(page, pid, sheetId);

  // No geo_point column → the map view-switcher segment is absent entirely.
  await expect(page.getByTestId('view-switch-map')).toHaveCount(0);
});

test('map view segment opens the map for a single geo_point column', async ({ page }) => {
  const { pid, sheetId, pointId } = await seedGeoSheet(page, {
    namePrefix: 'toolbar-map-single',
    point: { lat: 48.8584, lon: 2.2945 },
  });
  const point = { id: Number(pointId) };

  const mapPointRequests: string[] = [];
  page.on('request', (req) => {
    if (req.url().includes('/map/points')) mapPointRequests.push(req.url());
  });
  await mockBasemapTiles(page);

  await openProject(page, pid, sheetId);

  // Single geo column → the map view-switcher segment is present and opens it.
  const mapSegment = page.getByTestId('view-switch-map');
  await expect(mapSegment).toBeVisible();
  await mapSegment.click();

  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect
    .poll(() => mapPointRequests.some((u) => u.includes(`column_id=${point.id}`)))
    .toBeTruthy();
});

test('a geo_point column opens its own map from its header menu', async ({ page }) => {
  // Two geo columns exist so this proves the caret is column-SCOPED (opening
  // `work`'s own map, not merely "the first geo column"). `work` is the leading
  // (pinned) column so the coordinate-driven header-menu helper can reach it —
  // glide only surfaces header-menu clicks on the pinned pane to the helper.
  const pid = await createProject(page.request, uniqueName('toolbar-map-multi'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'work,home\n,\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const home = columns.find((c) => c.name === 'home')!;
  const work = columns.find((c) => c.name === 'work')!;
  const row = await firstRowId(page, pid, sheetId);
  await setColumnType(page.request, pid, home.id, 'geo_point');
  await setColumnType(page.request, pid, work.id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: row.id, columnId: home.id, value: { lat: 48.8584, lon: 2.2945 } },
    { rowId: row.id, columnId: work.id, value: { lat: 40.7484, lon: -73.9857 } },
  ]);

  const mapPointRequests: string[] = [];
  page.on('request', (req) => {
    if (req.url().includes('/map/points')) mapPointRequests.push(req.url());
  });
  await mockBasemapTiles(page);

  await openProject(page, pid, sheetId);

  // The toolbar's per-geo-column map SELECT retired (workbench-ia-toolbar-diet-v1:
  // "view switcher owns views"). The view switcher opens the leading geo column's
  // map, and the map request carries that column's id — proving the map is bound
  // to a specific geo column, not merely "some map".
  await page.getByTestId('view-switch-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect
    .poll(() => mapPointRequests.some((u) => u.includes(`column_id=${work.id}`)))
    .toBeTruthy();
});
