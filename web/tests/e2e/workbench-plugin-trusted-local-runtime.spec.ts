import { expect, test } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';
import {
  activateGeoBackendFromManager,
  enableGeoPluginFromManager,
  openPluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

test('trusted-local plugin runtime boundary is exposed through generic manager', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('trusted-local-map-runtime'));
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
    initialState: 'installed',
    receiptId: 'receipt_trusted_local_runtime',
    manifestSha: 'sha256:trusted-local-runtime',
    moduleKey: 'trustedLocal.frisketGeo',
    componentKey: 'trustedLocal.frisketGeo.views.MapView',
  });

  await openProject(page, pid, sheetId);
  await openPluginManager(page);
  const row = pluginManagerRow(page);
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute('data-receipt-id', 'receipt_trusted_local_runtime');
  await expect(row).toHaveAttribute('data-manifest-sha256', 'sha256:trusted-local-runtime');
  await expect(row).toHaveAttribute(
    'data-frontend-bindings',
    'frisket.geo.view.map=trustedLocal.frisketGeo#trustedLocal.frisketGeo.views.MapView',
  );

  await enableGeoPluginFromManager(page);
  await activateGeoBackendFromManager(page);
  expect(lifecycle.calls).toEqual(['activate', 'backend-activate']);
  await expect(row).toHaveAttribute('data-install-state', 'enabled');
  await expect(row).toHaveAttribute('data-registry-activated', 'true');

  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  const mapContribution = page.getByTestId('workbench-contribution-frisket-geo-view-map');
  await expect(mapContribution).toBeVisible();
  await expect(mapContribution).toHaveAttribute('data-contribution-id', 'frisket.geo.view.map');
  // Pin revision (geo-bundled-plugin-v1): the frame stamps the plugin
  // BINDING's componentKey, not the removed first-party registry key.
  await expect(mapContribution).toHaveAttribute(
    'data-runtime-component-key',
    'trustedLocal.frisketGeo.views.MapView',
  );
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});
