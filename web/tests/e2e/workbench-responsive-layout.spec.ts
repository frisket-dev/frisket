import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  openCellDrawer,
  openProject,
  openToolbarOverflow,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';
import { routeGeoPluginLifecycle } from './workbenchPluginLifecycleFixture';

type Rect = { left: number; right: number; top: number; bottom: number; width: number };

function boxesOverlap(a: Rect, b: Rect): boolean {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

async function box(locator: Locator): Promise<Rect> {
  const rect = await locator.boundingBox();
  expect(rect).not.toBeNull();
  return {
    left: rect!.x,
    right: rect!.x + rect!.width,
    top: rect!.y,
    bottom: rect!.y + rect!.height,
    width: rect!.width,
  };
}

async function createResponsiveGeoProject(page: Page) {
  const pid = await createProject(page.request, uniqueName('responsive-layout'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,point\n"Eiffel Tower",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`,
  );
  const firstRow = (await data.json()).rows[0] as { id: number };
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: firstRow.id, columnId: point.id, value: { lat: 48.8584, lon: 2.2945 } },
  ]);
  return { pid, sheetId, columns: await sheetColumns(page.request, pid, sheetId) };
}

// Seed the standalone visibility store with nothing hidden → Map stays enabled.
function installEnabledMapPreset(projectId: string) {
  localStorage.setItem(
    `frisket:contribution-visibility:${projectId}`,
    JSON.stringify({
      schemaVersion: 'frisket.contribution_visibility.v1',
      hiddenContributionIds: [],
    }),
  );
}

test('tablet layout keeps fixed regions and grid map split reachable', async ({ page }) => {
  await page.setViewportSize({ width: 820, height: 760 });
  const { pid, sheetId } = await createResponsiveGeoProject(page);
  await page.addInitScript(installEnabledMapPreset, pid);
  await routeGeoPluginLifecycle(page, pid, { initialState: 'enabled' });
  await openProject(page, pid, sheetId);

  await page.getByTestId('view-switch-map').click();
  const mainView = page.getByTestId('workbench-region-mainView');
  const bottomDock = page.getByTestId('workbench-region-bottomDock');
  const split = page.getByTestId('workbench-mainView-split');
  const grid = page.getByTestId('workbench-contribution-frisket-core-view-grid');
  const map = page.getByTestId('workbench-contribution-frisket-geo-view-map');

  // The left sidebar region is retired (workbench-ia-focus-v1).
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);
  await expect(split).toBeVisible();
  await expect(grid).toBeVisible();
  await expect(map).toBeVisible();
  await expect(page.getByTestId('view-switch-map')).toBeVisible();

  const mainBox = await box(mainView);
  const dockBox = await box(bottomDock);
  const gridBox = await box(grid);
  const mapBox = await box(map);
  expect(boxesOverlap(mainBox, dockBox)).toBe(false);
  expect(gridBox.width).toBeGreaterThan(220);
  expect(mapBox.width).toBeGreaterThan(220);
});

test('phone layout stacks companion panes and keeps modal and row drawer reachable', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 760 });
  const { pid, sheetId, columns } = await createResponsiveGeoProject(page);
  await page.addInitScript(installEnabledMapPreset, pid);
  await routeGeoPluginLifecycle(page, pid, { initialState: 'enabled' });
  await openProject(page, pid, sheetId);

  await page.getByTestId('view-switch-map').click();
  const grid = page.getByTestId('workbench-contribution-frisket-core-view-grid');
  const map = page.getByTestId('workbench-contribution-frisket-geo-view-map');
  await expect(grid).toBeVisible();
  await expect(map).toBeVisible();
  const gridBox = await box(grid);
  const mapBox = await box(map);
  expect(mapBox.top).toBeGreaterThan(gridBox.top);
  expect(gridBox.width).toBeGreaterThan(140);
  expect(mapBox.width).toBeGreaterThan(140);

  await openToolbarOverflow(page);
  await page.getByTestId('open-provenance-manifest').click();
  const modalRegion = page.getByTestId('workbench-region-modalOrPeek');
  const provenance = page.getByTestId('workbench-contribution-frisket-core-panel-provenance');
  await expect(modalRegion).toHaveAttribute('data-modal-stack-host', 'true');
  await expect(provenance).toBeVisible();
  const provenanceBox = await box(provenance);
  expect(provenanceBox.width).toBeLessThanOrEqual(390);
  await page.getByLabel('Close drawer').click();
  await expect(provenance).toBeHidden();
  await grid.scrollIntoViewIfNeeded();

  // 'place' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so double-click edits it instead of opening the
  // drawer; open via the floating icon instead.
  await openCellDrawer(page, columns, 'place', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText('Eiffel Tower');
});
