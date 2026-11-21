// Workbench plugin manager lifecycle E2E suite: local install & trust prompt,
// per-project enable/disable/uninstall, backend activation, product lifecycle
// metadata, marketplace-free local install validation, and command-palette recovery.

import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  openProject,
  setColumnType,
  setHiddenContributions,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';
import {
  activateGeoBackendFromManager,
  enableGeoPluginFromManager,
  expectBundledPluginRows,
  geoPluginId,
  geoPluginSourceValue,
  installGeoPluginFromManager,
  openPluginManager,
  pluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

async function seedGeoPlaceProject(page: Page, namePrefix: string) {
  const pid = await createProject(page.request, uniqueName(namePrefix));
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
  return { pid, sheetId, point };
}

test('generic plugin manager installs and enables a trusted-local plugin', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoPlaceProject(page, 'plugin-install-trust');
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

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('view-switch-map')).toBeEnabled();
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});

test('generic plugin manager tracks workspace install and per-project enablement state', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoPlaceProject(page, 'backend-plugin-install-state');
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

test('generic plugin manager disables per project and uninstalls the workspace package', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoPlaceProject(page, 'plugin-disable-uninstall');
  const lifecycle = await routeGeoPluginLifecycle(page, pid, {
    receiptId: 'receipt_plugin_disable_uninstall',
    manifestSha: 'sha256:plugin-disable-uninstall',
  });
  await openProject(page, pid, sheetId);

  await installGeoPluginFromManager(page);
  await enableGeoPluginFromManager(page);
  let row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'enabled');

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

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('bottom-dock-tab-errors')).toBeVisible();
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});

test('plugin manager enables manifest and backend registration from runtime index receipt', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoPlaceProject(page, 'backend-plugin-activation');
  const lifecycle = await routeGeoPluginLifecycle(page, pid, {
    initialState: 'installed',
    receiptId: 'receipt_backend_runtime_ui_sentinel',
    manifestSha: 'sha256:backend-runtime-ui-sentinel',
  });

  await openProject(page, pid, sheetId);
  await openPluginManager(page);
  const row = pluginManagerRow(page);
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute('data-install-state', 'installed');
  await expect(row).toHaveAttribute('data-receipt-id', 'receipt_backend_runtime_ui_sentinel');
  await expect(row).toHaveAttribute('data-manifest-sha256', 'sha256:backend-runtime-ui-sentinel');
  await expect(row).toHaveAttribute('data-workbench-views', 'frisket.geo.view.map');

  await enableGeoPluginFromManager(page);
  expect(lifecycle.activationBody()).toEqual({
    receiptId: 'receipt_backend_runtime_ui_sentinel',
    trustAcknowledged: true,
    permissionsAccepted: ['project:read', 'local.external.activate'],
    arbitraryPackageLoadAllowed: false,
  });

  await activateGeoBackendFromManager(page);
  expect(lifecycle.calls).toEqual(['activate', 'backend-activate']);
  expect(lifecycle.backendActivationBody()).toEqual({
    trustAcknowledged: true,
    arbitraryPackageLoadAllowed: false,
    executableHandlersAllowed: true,
  });
  await expect(row).toHaveAttribute('data-install-state', 'enabled');
  await expect(row).toHaveAttribute('data-registry-activated', 'true');
  await expect(row).toHaveAttribute(
    'data-actions',
    'frisket.geo.action.real_local_smoke',
  );
  await expect(row).toHaveAttribute(
    'data-projections',
    'frisket.geo.projection.map_points',
  );
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});

test('generic plugin manager uses backend lifecycle and resolved runtime metadata', async ({
  page,
}) => {
  const receiptId = 'receipt_plugin_lifecycle_product';
  const manifestSha = 'sha256:plugin-lifecycle-product';
  const moduleKey = 'trustedLocal.frisketGeo.productLifecycle';
  const componentKey = 'trustedLocal.frisketGeo.productLifecycle.MapView';
  const moduleUrl = `data:text/javascript;charset=utf-8,${encodeURIComponent(`
    export function MapView({ React, contributionId, pluginId }) {
      return React.createElement('div', {
        'data-testid': 'trusted-local-plugin-product-boundary',
        'data-contribution-id': contributionId,
        'data-plugin-id': pluginId
      }, 'Plugin map runtime');
    }
  `)}`;

  const { pid, sheetId, point } = await seedGeoPlaceProject(page, 'plugin-lifecycle-product');
  const lifecycle = await routeGeoPluginLifecycle(page, pid, {
    receiptId,
    manifestSha,
    moduleKey,
    componentKey,
    moduleUrl,
  });

  await openProject(page, pid, sheetId);
  await installGeoPluginFromManager(page);
  expect(lifecycle.calls).toEqual(['install-local']);
  expect(lifecycle.installBody()).toMatchObject({
    source: { kind: 'localPath', value: geoPluginSourceValue },
    arbitraryPackageLoadAllowed: false,
  });
  let row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'installed');
  await expect(row).toHaveAttribute('data-receipt-id', receiptId);
  await expect(row).toHaveAttribute(
    'data-frontend-bindings',
    `frisket.geo.view.map=${moduleKey}#${componentKey}`,
  );

  await enableGeoPluginFromManager(page);
  expect(lifecycle.calls).toEqual(['install-local', 'activate']);
  expect(lifecycle.activationBody()).toMatchObject({
    receiptId,
    trustAcknowledged: true,
    arbitraryPackageLoadAllowed: false,
  });
  row = pluginManagerRow(page);
  await expect(row).toHaveAttribute('data-install-state', 'enabled');
  await expect(row).toHaveAttribute('data-registry-activated', 'true');

  await activateGeoBackendFromManager(page);
  expect(lifecycle.calls).toEqual(['install-local', 'activate', 'backend-activate']);
  expect(lifecycle.backendActivationBody()).toMatchObject({
    trustAcknowledged: true,
    arbitraryPackageLoadAllowed: false,
    executableHandlersAllowed: true,
  });

  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  const mapContribution = page.getByTestId('workbench-contribution-frisket-geo-view-map');
  await expect(mapContribution).toHaveAttribute('data-runtime-component-key', componentKey);

  await openPluginManager(page);
  row = pluginManagerRow(page);
  await row.getByTestId('plugin-manager-disable').click();
  expect(lifecycle.calls).toEqual([
    'install-local',
    'activate',
    'backend-activate',
    'disable',
  ]);
  await expect(pluginManager(page).getByTestId('plugin-manager-status')).toHaveText(
    'disable:frisket.geo',
  );
  await expect(row).toHaveAttribute('data-install-state', 'disabled');
  await expect(row).toHaveAttribute('data-registry-activated', 'false');

  await row.getByTestId('plugin-manager-uninstall').click();
  expect(lifecycle.calls).toEqual([
    'install-local',
    'activate',
    'backend-activate',
    'disable',
    'uninstall',
  ]);
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

test('plugin manager exposes explicit local install without marketplace repair path', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-workbench-plugin-manager'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'name\nAda\n');
  await openProject(page, pid, sheetId);

  const manager = await openPluginManager(page);
  await expect(manager).toBeVisible();
  await expect(manager).toHaveAttribute('data-schema-version', 'frisket.workbench_plugin_runtime_index.v1');
  await expect(manager).toHaveAttribute('data-project-id', pid);
  await expectBundledPluginRows(page);
  await expect(page.getByTestId('workbench-plugin-marketplace-discovery')).toHaveCount(0);
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);

  await manager.getByTestId('plugin-manager-install-local').click();
  await expect(manager.getByTestId('plugin-manager-error')).toHaveText(
    'Plugin id and local source path are required.',
  );
});

test('Map layout recovery is hide and reveal through the command palette', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const { pid, sheetId } = await seedGeoPlaceProject(page, 'map-layout-recovery');
  await setHiddenContributions(page, pid, ['frisket.geo.view.map']);
  await openProject(page, pid, sheetId);

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  const revealMap = palette.getByTestId('workbench-visibility-command-reveal-frisket-geo-view-map');
  await expect(revealMap).toHaveAttribute('data-availability-status', 'hidden');
  await expect(revealMap).toHaveAttribute('data-availability-reason', 'hidden_by_profile');
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);

  await revealMap.click();
  await expect(revealMap).toHaveCount(0);
  await page.getByLabel('Close command palette').click();

  await page.getByTestId('view-switch-map').click();
  await expect(page.getByTestId('workbench-contribution-frisket-geo-view-map')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${sheetId}$`));

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  await expect(palette).toBeVisible();
  await palette.getByTestId('workbench-visibility-command-hide-frisket-geo-view-map').click();
  await expect(
    palette.getByTestId('workbench-visibility-command-reveal-frisket-geo-view-map'),
  ).toHaveAttribute('data-availability-status', 'hidden');
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});
