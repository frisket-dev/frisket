import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';
import {
  activateGeoBackendFromManager,
  enableGeoPluginFromManager,
  geoPluginSourceValue,
  installGeoPluginFromManager,
  openPluginManager,
  pluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

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

test('generic plugin manager uses backend lifecycle and resolved runtime metadata', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('plugin-lifecycle-product'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'places.csv',
    'headline,url,point\n"Eiffel Tower","https://example.test/eiffel",\n',
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
  // Pin revision (geo-bundled-plugin-v1): the frame stamps the plugin
  // BINDING's componentKey, not the removed first-party registry key.
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
