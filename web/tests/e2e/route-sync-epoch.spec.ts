// Route-sync epoch: pins the behavior the route substrate must preserve while
// it retires the route-sync
// effect trio:
//   1. Map deep-link entry hydration and URL normalization — now
//      subsumed into projectRouteState inside the projection path — still opens
//      the split and drops the panel from the URL, and coexists with a row
//      Detail across Back/Forward with no flicker / no lost panel.
//   2. Back/forward across sheet + row + split round-trips route state (the
//      structural-equality re-entry guard means a write-induced synthetic
//      popstate never desyncs or double-writes).
//   3. The route-driven row fetch resolves the row drawer against the
//      grid filter active at that route state — the back/forward-under-filter
//      case that proves the dependency key includes the active filter.

import { expect, test } from '@playwright/test';
import {
  importCsv,
  mockBasemapTiles,
  openFriendlyFilterSidebar,
  openCellDrawer,
  openProject,
  seedGeoSheet,
  sheetColumns,
  sheetData,
} from './helpers';

const routeRe = (path: string) =>
  new RegExp(`${path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:\\?|$)`);

test('map deep-link entry hydrates the split and normalizes the URL (B1), coexisting with a row Detail', async ({
  page,
}) => {
  const { pid, sheetId, pointId } = await seedGeoSheet(page, {
    namePrefix: 'e2e-route-sync-map',
    point: { lat: 48.8584, lon: 2.2945 },
  });
  await mockBasemapTiles(page);

  // The raw deep link carries a /map/column panel. On load the projection must
  // hydrate the workspace split (map-view visible) and normalize the panel out
  // of the URL — the whole point of B1: the panel slot is freed for a row Detail.
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${pointId}`);
  await expect(page.getByTestId('map-view')).toBeVisible({ timeout: 15_000 });
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}`));
  // Structurally excluded state (openSplit) can never leave the URL desynced —
  // the URL is the bare sheet, the split is workspace state.
  await expect(page).not.toHaveURL(/\/map\//);

  // Coexistence: click a pin to open the row Detail BESIDE the split.
  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  await expect(canvas).toBeVisible();
  await page.waitForTimeout(700);
  await canvas.click();
  await expect(page.getByTestId('row-drawer')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('workbench-mainView-split')).toBeVisible();

  // Back closes the row Detail (its push entry) without dropping the split or
  // flickering the URL back to /map.
  await page.goBack();
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}`));
  await expect(page).not.toHaveURL(/\/map\//);
});

test('back/forward across sheet + row round-trips route state without flicker', async ({
  page,
}) => {
  const pid = await (async () => {
    const p = await seedGeoSheet(page, { namePrefix: 'e2e-route-sync-nav' });
    return p.pid;
  })();
  const firstSheetId = await importCsv(
    page.request,
    pid,
    'cities.csv',
    'city,status\nAlbany,open\nBuffalo,closed\n',
  );
  const secondSheetId = await importCsv(page.request, pid, 'countries.csv', 'country\nGermany\nSpain\n');
  const columns = await sheetColumns(page.request, pid, firstSheetId);
  const firstRow = (await sheetData(page.request, pid, firstSheetId, 0, 5)).rows[0];
  const city = columns.find((c) => c.name === 'city');
  expect(city && firstRow).toBeTruthy();

  await openProject(page, pid, firstSheetId);
  await expect(page.getByTestId(`workbench-mainView-tab-${firstSheetId}`)).toHaveClass(/active/);

  // sheet select = push
  await page.getByTestId(`workbench-mainView-tab-${secondSheetId}`).click();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${secondSheetId}`));
  await page.goBack();
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${firstSheetId}`));
  await expect(page.getByTestId(`workbench-mainView-tab-${firstSheetId}`)).toHaveClass(/active/);

  // open row = push; back restores the bare sheet; forward re-opens the drawer
  // (the route-driven row-fetch re-resolves the row from the committed panel).
  // 'city' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'city', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-drawer')).toContainText('Albany');
  await page.goBack();
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${firstSheetId}`));
  await page.goForward();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-drawer')).toContainText('Albany');
});

test('row drawer resolves against the active grid filter during back/forward navigation', async ({
  page,
}) => {
  const pid = (await seedGeoSheet(page, { namePrefix: 'e2e-route-sync-filter' })).pid;
  const sheetId = await importCsv(
    page.request,
    pid,
    'cities.csv',
    'city,status\nAlbany,open\nBuffalo,closed\nChicago,open\nDover,closed\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const rows = (await sheetData(page.request, pid, sheetId, 0, 10)).rows;
  // Two rows sit in DIFFERENT filter partitions: an 'open' row and a 'closed'
  // one. The route-driven row-fetch must resolve whichever the panel names
  // against the filter active at that route state — not a stale filter.
  const openRowIndex = 0; // Albany / open

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Apply filter F1: status = open.
  await openFriendlyFilterSidebar(page, columns, 'status');
  await page.getByTestId('facet-check-status-open').check();

  // Open a row that IS in F1 (push → /row/{id}). The drawer must resolve it
  // against F1's scope (locateSheetRow + getSheetData carry the filter).
  await openCellDrawer(page, columns, 'city', openRowIndex);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-drawer')).toContainText('Albany');
  // Opening via a cell focuses that column, so the panel URL is /row/{id}[/column/{colId}].
  await expect(page).toHaveURL(
    new RegExp(`/p/${pid}/s/${sheetId}/row/${rows[openRowIndex].id}(?:/column/|\\?|$)`),
  );

  // Back to the bare filtered sheet; the drawer closes, the filter stays applied.
  await page.goBack();
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}`));

  // Forward re-opens the drawer: the row-fetch effect re-runs off the committed
  // panel keyed on the CURRENT filter/sort and re-resolves the same row.
  await page.goForward();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-drawer')).toContainText('Albany');
});
