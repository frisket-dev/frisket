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
  enableGeoPluginFromManager,
  openPluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

test('plugin manager lists frontend component bindings from runtime index metadata', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('frontend-component-bindings'));
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
    receiptId: 'receipt_frontend_binding_sentinel',
    manifestSha: 'sha256:frontend-binding-sentinel',
    moduleKey: 'trustedLocal.frisketGeo.receiptModule',
    componentKey: 'trustedLocal.frisketGeo.receiptMapView',
  });

  await openProject(page, pid, sheetId);
  await openPluginManager(page);
  const row = pluginManagerRow(page);
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute('data-workbench-views', 'frisket.geo.view.map');
  await expect(row).toHaveAttribute(
    'data-frontend-bindings',
    'frisket.geo.view.map=trustedLocal.frisketGeo.receiptModule#trustedLocal.frisketGeo.receiptMapView',
  );
  await expect(row).toHaveAttribute('data-manifest-sha256', 'sha256:frontend-binding-sentinel');

  await enableGeoPluginFromManager(page);
  expect(lifecycle.calls).toEqual(['activate']);
  await expect(row).toHaveAttribute('data-install-state', 'enabled');
  await expect(row).toHaveAttribute('data-registry-activated', 'true');
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});
