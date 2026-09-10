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
  openPluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

test('plugin manager enables manifest and backend registration from runtime index receipt', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('backend-plugin-activation'));
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
