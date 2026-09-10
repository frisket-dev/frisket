import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  openProject,
  setColumnType,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';
import {
  enableGeoPluginFromManager,
  installGeoPluginFromManager,
  openPluginManager,
  pluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

test('generic plugin manager disables per project and uninstalls the workspace package', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('plugin-disable-uninstall'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'headline,url,point\n"Eiffel Tower","https://example.test/eiffel",\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  const data = await sheetData(page.request, pid, sheetId, 0, 1);
  await setColumnType(page.request, pid, point.id, 'geo_point');
  await editCells(page.request, pid, [
    { rowId: data.rows[0].id, columnId: point.id, value: { lat: 48.8584, lon: 2.2945 } },
  ]);
  const lifecycle = await routeGeoPluginLifecycle(page, pid, {
    receiptId: 'receipt_plugin_disable_uninstall',
    manifestSha: 'sha256:plugin-disable-uninstall',
  });
  await openProject(page, pid, sheetId);

  await installGeoPluginFromManager(page);
  await enableGeoPluginFromManager(page);
  let row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'enabled');
  // The settings revamp moved the manager to /settings/project/plugins, so
  // workbench assertions must run on the workbench (the old in-place assertion
  // was a navigation-race flake). With a geo column present and the plugin
  // enabled, the map is AVAILABLE: the view-switcher segment is live (the
  // rail launcher retired with the redesign).
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('view-switch-map')).toBeEnabled();
  await openPluginManager(page);
  row = pluginManagerRow(page);

  await row.getByTestId('plugin-manager-disable').click();
  expect(lifecycle.calls).toEqual(['install-local', 'activate', 'disable']);
  await expect(pluginManager(page).getByTestId('plugin-manager-status')).toHaveText(
    'disable:frisket.geo',
  );
  row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'disabled');
  await expect(row).toHaveAttribute('data-activation', 'blocked');
  await expect(row).toHaveAttribute('data-registry-activated', 'false');

  await row.getByTestId('plugin-manager-uninstall').click();
  expect(lifecycle.calls).toEqual(['install-local', 'activate', 'disable', 'uninstall']);
  await expect(pluginManager(page).getByTestId('plugin-manager-status')).toHaveText(
    'uninstall:frisket.geo',
  );
  row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'uninstalled');
  await expect(row).toHaveAttribute('data-activation', 'removed');
  await expect(row).toHaveAttribute('data-registry-activated', 'false');
  await expect(row.getByTestId('plugin-manager-uninstall')).toBeDisabled();
  // Back on the workbench: uninstalling the plugin never hides unrelated
  // contributions — the Errors dock tab is still present — and no repair
  // affordance appears for the uninstalled map.
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('bottom-dock-tab-errors')).toBeVisible();
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});
