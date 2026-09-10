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
  geoPluginId,
  geoPluginSourceValue,
  installGeoPluginFromManager,
  openPluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

test('generic plugin manager installs and enables a trusted-local plugin', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('plugin-install-trust'));
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
    receiptId: 'receipt_plugin_install_trust',
    manifestSha: 'sha256:plugin-install-trust',
  });
  await openProject(page, pid, sheetId);

  const manager = await openPluginManager(page);
  await expect(manager).toHaveAttribute('data-plugin-count', '0');
  const installed = await installGeoPluginFromManager(page);
  expect(lifecycle.calls).toEqual(['install-local']);
  expect(lifecycle.installBody()).toMatchObject({
    source: { kind: 'localPath', value: geoPluginSourceValue },
    arbitraryPackageLoadAllowed: false,
  });
  await expect(installed).toHaveAttribute('data-plugin-id', geoPluginId);
  await expect(installed).toHaveAttribute('data-receipt-id', 'receipt_plugin_install_trust');
  await expect(installed).toHaveAttribute('data-manifest-sha256', 'sha256:plugin-install-trust');
  await expect(installed).toHaveAttribute('data-capabilities', 'project:read local.external.activate');

  await installed.getByTestId('plugin-manager-enable').click();
  expect(lifecycle.calls).toEqual(['install-local']);
  const prompt = installed.getByTestId('plugin-manager-trust-prompt');
  await expect(prompt).toBeVisible();
  await expect(prompt).toHaveAttribute('data-plugin-id', geoPluginId);
  await expect(prompt).toHaveAttribute(
    'data-capabilities',
    'project:read local.external.activate',
  );
  await expect(prompt).toHaveAttribute('data-secrets', '');
  await expect(prompt).toHaveAttribute('data-manifest-sha256', 'sha256:plugin-install-trust');
  await expect(prompt).toHaveAttribute('data-package-sha256', 'sha256:geo-plugin-package');
  await expect(installed.getByTestId('plugin-manager-confirm-enable')).toBeDisabled();
  await installed.getByTestId('plugin-manager-capability-project:read').check();
  await expect(installed.getByTestId('plugin-manager-confirm-enable')).toBeDisabled();
  await installed
    .getByTestId('plugin-manager-capability-local.external.activate')
    .check();
  await expect(installed.getByTestId('plugin-manager-confirm-enable')).toBeEnabled();
  await installed.getByTestId('plugin-manager-confirm-enable').click();
  await expect(manager.getByTestId('plugin-manager-status')).toHaveText(
    `enable:${geoPluginId}`,
  );
  const enabled = pluginManagerRow(page);
  expect(lifecycle.calls).toEqual(['install-local', 'activate']);
  expect(lifecycle.activationBody()).toMatchObject({
    receiptId: 'receipt_plugin_install_trust',
    trustAcknowledged: true,
    permissionsAccepted: ['project:read', 'local.external.activate'],
    arbitraryPackageLoadAllowed: false,
  });
  await expect(enabled).toHaveAttribute('data-install-state', 'enabled');
  await expect(enabled).toHaveAttribute('data-registry-activated', 'true');
  // Back on the workbench (the manager lives on the settings screen): with a
  // geo column present and the plugin enabled, the Map view is AVAILABLE — the
  // view-switcher segment is live and no repair affordance renders (the rail's
  // recovery entry retired with the redesign).
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('view-switch-map')).toBeEnabled();
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});
