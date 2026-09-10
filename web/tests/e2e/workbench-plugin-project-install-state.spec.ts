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
  openPluginManager,
  pluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

test('generic plugin manager tracks workspace install and per-project enablement state', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('backend-plugin-install-state'));
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
    initialState: 'installed',
    receiptId: 'receipt_plugin_geo_loaded',
    manifestSha: 'sha256:geo-runtime-index',
  });
  await openProject(page, pid, sheetId);
  await openPluginManager(page);

  let row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'installed');
  await expect(row).toHaveAttribute('data-activation', 'manifestLoaded');
  await expect(row).toHaveAttribute('data-registry-activated', 'false');

  await enableGeoPluginFromManager(page);
  expect(lifecycle.calls).toEqual(['activate']);
  row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'enabled');
  await expect(row).toHaveAttribute('data-registry-activated', 'true');

  await row.getByTestId('plugin-manager-disable').click();
  expect(lifecycle.calls).toEqual(['activate', 'disable']);
  await expect(pluginManager(page).getByTestId('plugin-manager-status')).toHaveText(
    'disable:frisket.geo',
  );
  row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'disabled');
  await expect(row).toHaveAttribute('data-activation', 'blocked');
  await expect(row).toHaveAttribute('data-registry-activated', 'false');

  await row.getByTestId('plugin-manager-uninstall').click();
  expect(lifecycle.calls).toEqual(['activate', 'disable', 'uninstall']);
  await expect(pluginManager(page).getByTestId('plugin-manager-status')).toHaveText(
    'uninstall:frisket.geo',
  );
  row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'uninstalled');
  await expect(row).toHaveAttribute('data-activation', 'removed');
  await expect(row).toHaveAttribute('data-registry-activated', 'false');
  await expect(row.getByTestId('plugin-manager-uninstall')).toBeDisabled();
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});
