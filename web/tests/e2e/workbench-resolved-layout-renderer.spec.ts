import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  openDiscoverTab,
  openProject,
  setColumnType,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

let pid: string;

test.beforeEach(async ({ page }) => {
  pid = await createProject(page.request, uniqueName('e2e-workbench-resolved-layout'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'place,point\n"Pier 57",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((column) => column.name === 'point');
  if (!point) throw new Error('point column not imported');
  const data = await sheetData(page.request, pid, sheetId, 0, 1);
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: data.rows[0].id, columnId: point.id, value: { lat: 40.741, lon: -74.01 } },
  ]);
  await openProject(page, pid, sheetId);
});

test('workbench regions are populated by a resolved layout tree', async ({ page }) => {
  // The activityRail host resolves in the layout model but no region renders
  // it any longer (the rail retired with the redesign purge).
  const leftSidebar = page.getByTestId('workbench-resolved-layout-region-leftSidebar');
  const mainView = page.getByTestId('workbench-resolved-layout-region-mainView');
  const rightInspector = page.getByTestId('workbench-resolved-layout-region-rightInspector');
  const bottomDock = page.getByTestId('workbench-resolved-layout-region-bottomDock');
  const modalOrPeek = page.getByTestId('workbench-resolved-layout-region-modalOrPeek');

  for (const region of [leftSidebar, mainView, rightInspector, bottomDock, modalOrPeek]) {
    await expect(region).toHaveAttribute('data-renderer', 'ResolvedWorkbenchLayoutRendererV1');
  }

  // Search + Copilot left the leftSidebar stream for the palette/popover
  // (workbench-ia-focus-v1); the Discover-hosted panels remain.
  await expect(leftSidebar).toHaveAttribute(
    'data-contribution-ids',
    /frisket\.core\.panel\.sources.*frisket\.investigative\.panel\.friendly_filters/s,
  );
  await expect(leftSidebar).not.toHaveAttribute(
    'data-contribution-ids',
    /frisket\.core\.panel\.search/,
  );
  await expect(mainView).toHaveAttribute(
    'data-contribution-ids',
    /frisket\.core\.view\.grid.*frisket\.geo\.view\.map/s,
  );
  // Actions are EXPRESSED by the shell's overlay ActionDrawer, never PLACED as
  // a rightInspector contribution — the retired
  // panel descriptor is gone from the resolved layout entirely. Cluster resolve
  // is the genuine default rightInspector panel that remains.
  await expect(rightInspector).toHaveAttribute(
    'data-contribution-ids',
    /frisket\.investigative\.panel\.cluster_resolve/,
  );
  await expect(rightInspector).not.toHaveAttribute(
    'data-contribution-ids',
    /frisket\.core\.panel\.actions/,
  );
  await expect(bottomDock).toHaveAttribute('data-contribution-ids', /frisket\.core\.panel\.jobs/);
  await expect(modalOrPeek).toHaveAttribute('data-contribution-ids', /frisket\.core\.view\.evidence/);

  // Sources renders in the Discover panel's Sources tab (its re-home since
  // workbench-ia-right-edge-v1), still declaring its leftSidebar placement.
  await openDiscoverTab(page, 'Sources');
  const sources = page.getByTestId('workbench-contribution-frisket-core-panel-sources');
  await expect(sources).toBeVisible();
  await expect(sources).toHaveAttribute('data-host', 'leftSidebar');

  const grid = page.getByTestId('workbench-contribution-frisket-core-view-grid');
  await expect(grid).toBeVisible();
  await expect(grid).toHaveAttribute('data-host', 'mainView');

  const jobs = page.getByTestId('workbench-contribution-frisket-core-panel-jobs');
  await expect(jobs).toBeVisible();
  await expect(jobs).toHaveAttribute('data-host', 'bottomDock');

  // Pin revision (geo-bundled-plugin-v1): the map view arrives from the
  // bundled frisket.geo plugin through the runtime index
  // (src/frisket/authoring/bundled_plugins/frisket.geo/workbench-descriptors.json:
  // mainView pane in the work.companion slot), no longer a first-party
  // descriptor.
  const mapItem = mainView.getByTestId('workbench-resolved-layout-item-frisket-geo-view-map');
  await expect(mapItem).toHaveAttribute('data-host', 'mainView');
  await expect(mapItem).toHaveAttribute('data-slot', 'work.companion');
  await expect(mapItem).toHaveAttribute('data-runtime-source', 'runtimeIndex');

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await palette.getByTestId('workbench-visibility-command-hide-frisket-geo-view-map').click();
  await page.getByLabel('Close command palette').click();

  await expect(mainView).toHaveAttribute('data-hidden-contribution-ids', /frisket\.geo\.view\.map/);
  await expect(mapItem).toHaveAttribute('data-status', 'hidden');
  await expect(mapItem).toHaveAttribute('data-reason', 'hidden_by_profile');

  // Reveal through the ⌘K palette (the Window menu + preset reset retired,
  // workbench-ia-toolbar-diet-v1).
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const revealPalette = page.getByTestId('workbench-region-commandPalette');
  await revealPalette.getByTestId('workbench-visibility-command-reveal-frisket-geo-view-map').click();
  await page.getByLabel('Close command palette').click();
  await expect(mainView).toHaveAttribute('data-enabled-contribution-ids', /frisket\.geo\.view\.map/);
  await expect(mapItem).toHaveAttribute('data-status', 'enabled');
  await expect(mapItem).not.toHaveAttribute('data-reason');
});
