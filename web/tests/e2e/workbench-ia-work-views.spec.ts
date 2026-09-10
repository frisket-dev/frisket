// Workbench IA increment 6 — Work region: the view switcher, split-beside-grid
// with a synced selection + promote, and the grid header sort/filter UX.
//
// The Work region uses a toolbar view switcher with split/promote escalation
// and a drag-resizable split seam clamped to 280–820px.
//
// Selection is the REAL SelectedGridRows state (single source of truth): a grid
// row click is reflected in the split's synced-selection readout, and a map pin
// click writes the same shared selection back so the grid + Detail update.
// Promoted tabs are NOT preview tabs — they never start a run.

import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderLabel,
  clickHeaderMenu,
  createProject,
  importCsv,
  mockBasemapTiles,
  openFriendlyFilterSidebar,
  openProject,
  openToolbarOverflow,
  seedGeoSheet as seedGeoSheetBase,
  selectRow,
  sheetColumns,
  uniqueName,
} from './helpers';

// The geo column starts EMPTY so column.set_type → geo_point succeeds (text
// values could not coerce); one point is then written via editCells.
const GEO_CSV = 'place,point\n"Eiffel Tower",\n"Louvre",\n"Notre Dame",\n';
const SORT_CSV =
  'city,status\n"Paris","open"\n"Berlin","closed"\n"Amsterdam","open"\n"Zurich","closed"\n';

// A single geo point so fit-to-bounds centers it and a canvas center-click
// reliably picks it (the deck.gl pin-selection pattern).
async function seedGeoSheet(page: Page): Promise<{ pid: string; sheetId: number; pointId: string }> {
  return seedGeoSheetBase(page, {
    namePrefix: 'work-views-geo',
    csv: GEO_CSV,
    point: { lat: 48.8584, lon: 2.2945 },
  });
}

async function historyTotal(request: import('@playwright/test').APIRequestContext, pid: string) {
  const res = await request.get(`/api/projects/${pid}/history?limit=50`);
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { total: number }).total;
}

test('switcher renders only data-satisfied segments; a non-Grid segment opens a split with the grid still visible; the seam resizes and persists', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page);
  await mockBasemapTiles(page);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible();

  // Grid + Map are data-satisfied; Gallery (needs an image column) is not, and
  // Graph is data-keyed to materialized edge/join sheets — a geo sheet is not
  // one, so its segment is absent.
  await expect(page.getByTestId('view-switch-grid')).toBeVisible();
  await expect(page.getByTestId('view-switch-map')).toBeVisible();
  await expect(page.getByTestId('view-switch-graph')).toHaveCount(0);
  await expect(page.getByTestId('view-switch-gallery')).toHaveCount(0);
  // Grid is the active segment before any split opens.
  await expect(page.getByTestId('view-switch-grid')).toHaveAttribute('aria-pressed', 'true');

  // Clicking Map opens a SPLIT (not a swap): the grid stays visible beside the map.
  await page.getByTestId('view-switch-map').click();
  const split = page.getByTestId('workbench-mainView-split');
  await expect(split).toBeVisible();
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('view-switch-map')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('work-split-header')).toBeVisible();

  // The seam resizes the companion and the size persists across reload.
  const seam = page.getByTestId('work-split-seam');
  await expect(seam).toBeVisible();
  const companion = page.getByTestId('work-split-companion');
  const before = (await companion.boundingBox())!.width;
  const seamBox = (await seam.boundingBox())!;
  await page.mouse.move(seamBox.x + seamBox.width / 2, seamBox.y + seamBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(seamBox.x - 160, seamBox.y + seamBox.height / 2, { steps: 8 });
  await page.mouse.up();
  const after = (await companion.boundingBox())!.width;
  expect(Math.abs(after - before)).toBeGreaterThan(60);

  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-map').click();
  await expect(page.getByTestId('work-split-companion')).toBeVisible();
  const persisted = (await page.getByTestId('work-split-companion').boundingBox())!.width;
  expect(Math.abs(persisted - after)).toBeLessThan(40);

  // Clicking Grid (or the active Map segment again) closes the split.
  await page.getByTestId('view-switch-grid').click();
  await expect(page.getByTestId('workbench-mainView-split')).toHaveCount(0);
  await expect(page.getByTestId('grid')).toBeVisible();
});

test('synced selection: a grid row click shows in the split, and a map pin click writes back to the shared grid selection', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page);
  await mockBasemapTiles(page);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();

  const readout = page.getByTestId('work-split-selection');
  await expect(readout).toBeVisible();
  await expect(readout).toHaveAttribute('data-selection-count', '0');

  // Grid → split: selecting a grid row updates the split's synced-selection state.
  await selectRow(page, 0);
  await expect(readout).toHaveAttribute('data-selection-count', '1');

  // Split → grid: a map pin click writes the SAME SelectedGridRows state back.
  // Opening the picked row opens its Detail column BESIDE the split (the split
  // stays open — Detail and split are independent surfaces), and the shared
  // selection the grid toolbar reads is now that row — proven by the Detail
  // opening on the picked row and the delete-rows button reflecting a live
  // single-row selection (both read SelectedGridRows, not a map-local highlight).
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');
  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  await expect(canvas).toBeVisible();
  await page.waitForTimeout(700);
  await canvas.click();
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  await expect(drawer).toContainText('Eiffel Tower');
  await expect(page.getByTestId('workbench-mainView-split')).toBeVisible();
  await expect(page.getByTestId('delete-rows-button')).toHaveAttribute(
    'title',
    /Delete 1 selected row/,
  );
});

test('a map pin opens the Detail column WITHOUT evicting the split (split + Detail + grid coexist); the map pane renders no back-to-grid button', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page);
  await mockBasemapTiles(page);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('workbench-mainView-split')).toBeVisible();

  // The map pane's redundant "← Back to grid" affordance is gone: the split
  // header × and the switcher's Grid segment are the close paths.
  await expect(page.getByTestId('map-close')).toHaveCount(0);

  // Click the single pin. The bug: the row Detail and the map split both read the
  // ONE route panel slot, so opening Detail evicted the map. Now they coexist —
  // Work grid | split | Detail all visible together.
  await expect(page.getByTestId('map-points-count')).toHaveText('1 points');
  const canvas = page.locator('[data-testid="map-canvas-container"] canvas').first();
  await expect(canvas).toBeVisible();
  await page.waitForTimeout(700);
  await canvas.click();

  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible({ timeout: 15_000 });
  await expect(drawer).toContainText('Eiffel Tower');
  // The bug pin: the split must STAY open beside the Detail column.
  await expect(page.getByTestId('workbench-mainView-split')).toBeVisible();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('grid')).toBeVisible();
});

test('deep link /map/column opens the split as workspace state and normalizes the URL so the panel slot frees up', async ({
  page,
}) => {
  const { pid, sheetId, pointId } = await seedGeoSheet(page);
  await mockBasemapTiles(page);
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${pointId}`);

  // The deep link is a valid ENTRY point: it hydrates the split state.
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('workbench-mainView-split')).toBeVisible();

  // …then the URL normalizes (the map split is no longer route-driven), so the
  // single route panel slot is free for a coexisting row Detail.
  await expect.poll(() => new URL(page.url()).pathname).not.toContain('/map/column');
});

test('promote detaches the split into a persistent Navigate tab carrying selection, closes back to the sheet, survives reload, and never starts a run', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page);
  await mockBasemapTiles(page);
  const historyBefore = await historyTotal(page.request, pid);

  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-map').click();
  await expect(page.getByTestId('map-view')).toBeVisible();
  await selectRow(page, 0);
  await expect(page.getByTestId('work-split-selection')).toHaveAttribute('data-selection-count', '1');

  // Promote → a persistent tab in the Navigate strip; the split closes and the
  // promoted view renders full-window (the grid toolbar is gone).
  await page.getByTestId('work-split-promote').click();
  const promotedTab = page.getByTestId('workbench-mainView-promotedTab');
  await expect(promotedTab).toBeVisible();
  await expect(promotedTab).toContainText(/Map of/i);
  await expect(page.getByTestId('workbench-mainView-split')).toHaveCount(0);
  await expect(page.getByTestId('map-view')).toBeVisible();
  // Selection carried into the promoted tab.
  await expect(page.getByTestId('promoted-view-selection')).toHaveAttribute('data-selection-count', '1');

  // Promoting never persisted anything — no run/history entry (not a preview tab).
  expect(await historyTotal(page.request, pid)).toBe(historyBefore);

  // Survives reload.
  await page.reload();
  await expect(page.getByTestId('workbench-mainView-promotedTab')).toBeVisible();

  // Closing the promoted tab returns to the source sheet's grid.
  await page.getByTestId('workbench-mainView-promotedTab-close').click();
  await expect(page.getByTestId('workbench-mainView-promotedTab')).toHaveCount(0);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId(`workbench-mainView-tab-${sheetId}`)).toHaveAttribute('aria-selected', 'true');
});

test('grid header: label click sorts server-side and the second click flips direction', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('work-views-sort'));
  const sheetId = await importCsv(page.request, pid, 'cities.csv', SORT_CSV);
  const columns = await sheetColumns(page.request, pid, sheetId);

  const sortRequests: Array<{ dir: string }> = [];
  page.on('request', (req) => {
    const u = new URL(req.url());
    if (u.pathname.includes(`/sheets/${sheetId}/data`) && u.searchParams.get('sort')) {
      const sort = JSON.parse(u.searchParams.get('sort')!) as Array<{ column: string; dir: string }>;
      const rule = sort.find((r) => r.column === 'city');
      if (rule) sortRequests.push({ dir: rule.dir });
    }
  });

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible();

  await clickHeaderLabel(page, columns, 'city');
  await expect(page.getByTestId('active-grid-sort')).toContainText('city asc');
  await expect.poll(() => sortRequests.some((r) => r.dir === 'asc')).toBeTruthy();

  await clickHeaderLabel(page, columns, 'city');
  await expect(page.getByTestId('active-grid-sort')).toContainText('city desc');
  await expect.poll(() => sortRequests.some((r) => r.dir === 'desc')).toBeTruthy();
});

test('grid header: the funnel opens an inline filter row that filters live via the server GridFilterSpec, clears, and leaves the advanced panels reachable', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('work-views-filter'));
  const sheetId = await importCsv(page.request, pid, 'cities.csv', SORT_CSV);
  const columns = await sheetColumns(page.request, pid, sheetId);

  const filterRequests: string[] = [];
  page.on('request', (req) => {
    const u = new URL(req.url());
    if (u.pathname.includes(`/sheets/${sheetId}/data`) && u.searchParams.get('filter')) {
      filterRequests.push(u.searchParams.get('filter')!);
    }
  });

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('sheet-stats')).toContainText('4 rows');

  // The caret's Filter… opens the inline filter row (the toolbar funnel retired).
  await clickHeaderMenu(page, columns, 'status');
  await page.getByTestId('header-menu-filter').click();
  await expect(page.getByTestId('inline-filter-row')).toBeVisible();
  await page.getByTestId('inline-filter-column').selectOption('status');
  await page.getByTestId('inline-filter-input').fill('closed');

  // Rows filter live and the count updates; the server saw a real GridFilterSpec.
  await expect(page.getByTestId('sheet-stats')).toContainText('2 rows');
  await expect(page.getByTestId('active-grid-filter')).toContainText('status');
  await expect
    .poll(() => filterRequests.some((f) => f.includes('status') && f.includes('contains')))
    .toBeTruthy();

  // Clear restores every row.
  await page.getByTestId('inline-filter-clear').click();
  await expect(page.getByTestId('sheet-stats')).toContainText('4 rows');

  // Friendly filters, advanced sort, and saved views stay reachable through
  // the caret/sidebar and toolbar-overflow homes.
  await openFriendlyFilterSidebar(page, columns, 'status');
  await openToolbarOverflow(page);
  await page.getByTestId('open-saved-views-submenu').click();
  await page.getByTestId('saved-views-overflow-open-panel').click();
  await expect(page.getByTestId('views-panel')).toBeVisible();
});

test('a hidden-but-data-satisfied view renders a DISABLED segment naming the reason', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('view-switch-map')).toBeVisible();

  // Hide the map contribution via the palette's visibility commands — the
  // segment must stay VISIBLE but disabled, naming why (a hidden map with geo
  // data present was an unexplainable silent omission).
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+KeyP' : 'Control+Shift+KeyP');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  await palette.getByTestId(/workbench-visibility-command-hide-/).filter({ hasText: /map/i }).first().click();

  const segment = page.getByTestId('view-switch-map');
  await expect(segment).toBeVisible();
  await expect(segment).toBeDisabled();
  await expect(segment).toHaveAttribute('data-disabled-reason', /hidden|reveal/i);
});
