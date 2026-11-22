// Workbench plugin runtime E2E suite: frontend component bindings, trusted-local
// runtime boundary, runtime index evidence rendering, and real local plugin smoke.

import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  editCells,
  importCsv,
  listSheets,
  mockBasemapTiles,
  openProject,
  projectIdByName,
  setColumnType,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';
import {
  activateGeoBackendFromManager,
  enableGeoPluginFromManager,
  expectBundledPluginRows,
  openPluginManager,
  pluginManagerRow,
  routeGeoPluginLifecycle,
} from './workbenchPluginLifecycleFixture';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginManifestPath = path.resolve(
  __dirname,
  '../../../tests/fixtures/local_plugins/frisket_geo_smoke/plugin.json',
);
const pluginRootPath = path.dirname(pluginManifestPath);
const smokePluginId = 'frisket.geosmoke';
const smokeActionId = 'frisket.geosmoke.real_local_smoke';
const smokeContributionId = 'frisket.geosmoke.view.real_local_smoke';
const smokeMapContributionId = 'frisket.geosmoke.view.map';

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

test('plugin manager lists frontend component bindings from runtime index metadata', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoPlaceProject(page, 'frontend-component-bindings');
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

test('trusted-local plugin runtime boundary is exposed through generic manager', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const { pid, sheetId, point } = await seedGeoPlaceProject(page, 'trusted-local-map-runtime');
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
  await expect(mapContribution).toHaveAttribute(
    'data-runtime-component-key',
    'trustedLocal.frisketGeo.views.MapView',
  );
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});

test('generic plugin manager reads backend plugin runtime index evidence', async ({
  page,
}) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await page.route(`**/api/projects/${pid}/workbench/plugins`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
        projectId: pid,
        arbitraryPackageLoadAllowed: false,
        receiptScanLimit: 5000,
        skippedInvalidReceipts: 0,
        skippedInvalidManifestRefs: 0,
        loadedPluginCount: 1,
        plugins: [
          {
            schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1',
            pluginId: 'frisket.geo',
            version: '0.1.0',
            installState: 'installed',
            activation: 'manifestLoaded',
            runtimeSource: 'plugin.load_receipt',
            receiptId: 'receipt_plugin_geo_loaded',
            manifestSha256: 'sha256:geo-runtime-index',
            packageSha256: 'sha256:geo-runtime-package',
            byteCount: 512,
            source: {
              kind: 'local_file',
              path: '/Users/example/plugins/frisket-geo/plugin.json',
            },
            contributionSummary: [
              { kind: 'workbench_view', count: 1, ids: ['frisket.geo.view.map'] },
              {
                kind: 'workbench_panel',
                count: 1,
                ids: ['frisket.core.panel.projection_status'],
              },
            ],
            frontendComponentBindings: [
              {
                schemaVersion: 'frisket.workbench_plugin_frontend_component_binding.v1',
                contributionId: 'frisket.geo.view.map',
                moduleKey: 'trustedLocal.frisketGeo.runtimeIndex',
                componentKey: 'trustedLocal.frisketGeo.runtimeIndex.MapView',
              },
            ],
            requires: {
              capabilities: ['project:read', 'local.external.activate'],
              secrets: [],
            },
            arbitraryPackageLoadAllowed: false,
            registryActivated: false,
            installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
            disabledReason: null,
          },
        ],
        firstParty: {
          schemaVersion: 'frisket.workbench_descriptor_package.v1',
          descriptors: [],
        },
      }),
    });
  });
  const sheets = await listSheets(page.request, pid);
  await openProject(page, pid, sheets[0].id);

  const manager = await openPluginManager(page);
  await expect(manager).toBeVisible();
  await expect(manager).toHaveAttribute(
    'data-schema-version',
    'frisket.workbench_plugin_runtime_index.v1',
  );
  await expect(manager).toHaveAttribute('data-project-id', pid);
  await expect(manager).toHaveAttribute('data-plugin-count', '1');

  const row = pluginManagerRow(page);
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute('data-plugin-id', 'frisket.geo');
  await expect(row).toHaveAttribute('data-install-state', 'installed');
  await expect(row).toHaveAttribute('data-activation', 'manifestLoaded');
  await expect(row).toHaveAttribute('data-receipt-id', 'receipt_plugin_geo_loaded');
  await expect(row).toHaveAttribute('data-manifest-sha256', 'sha256:geo-runtime-index');
  await expect(row).toHaveAttribute('data-package-sha256', 'sha256:geo-runtime-package');
  await expect(row).toHaveAttribute('data-workbench-views', 'frisket.geo.view.map');
  await expect(row).toHaveAttribute(
    'data-workbench-panels',
    'frisket.core.panel.projection_status',
  );
  await expect(row).toHaveAttribute(
    'data-contributions',
    'workbench_view:frisket.geo.view.map workbench_panel:frisket.core.panel.projection_status',
  );
  await expect(row).toHaveAttribute(
    'data-frontend-bindings',
    'frisket.geo.view.map=trustedLocal.frisketGeo.runtimeIndex#trustedLocal.frisketGeo.runtimeIndex.MapView',
  );
  await expect(row).not.toHaveAttribute(
    'data-backend-source-path',
    '/Users/example/plugins/frisket-geo/plugin.json',
  );
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);

  await openProject(page, pid, sheets[0].id);
  await expect(page.getByTestId('bottom-dock-tab-plugins')).toHaveCount(0);
});

test('real local plugin load activates visible Map contribution and backend projection binding', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const { pid, sheetId, point } = await seedGeoPlaceProject(page, 'real-local-plugin-smoke');

  await openProject(page, pid, sheetId);
  const manager = await openPluginManager(page);
  await expect(manager).toBeVisible();
  await expectBundledPluginRows(page);

  const bundledRow = pluginManagerRow(page);
  if ((await bundledRow.getAttribute('data-install-state')) === 'enabled') {
    await bundledRow.getByTestId('plugin-manager-disable').click();
    await expect(manager.getByTestId('plugin-manager-status')).toHaveText(
      'disable:frisket.geo',
    );
  }
  await expect(bundledRow).not.toHaveAttribute('data-install-state', 'enabled');
  await manager.getByTestId('plugin-manager-install-plugin-id').fill(smokePluginId);
  await manager.getByTestId('plugin-manager-install-source').fill(pluginRootPath);
  await manager.getByTestId('plugin-manager-install-local').click();
  await expect(manager.getByTestId('plugin-manager-status')).toHaveText(
    `install:${smokePluginId}`,
  );

  type RuntimePlugin = {
    pluginId: string;
    installState: string;
    activation: string;
    registryActivated: boolean;
    receiptId: string;
    manifestSha256: string;
    packageSha256: string;
    source?: { kind?: string; path?: string; value?: string };
    contributionSummary: Array<{ kind: string; ids: string[] }>;
    frontendComponentBindings?: Array<{ contributionId: string; moduleUrl?: string }>;
    workbenchDescriptorManifests?: Array<{ id: string; schemaVersion: string }>;
  };
  const readRuntimePlugin = async (): Promise<RuntimePlugin | undefined> => {
    const runtimeIndex = await page.request.get(`/api/projects/${pid}/workbench/plugins`);
    expect(runtimeIndex.ok()).toBeTruthy();
    const runtimeIndexBody = (await runtimeIndex.json()) as {
      plugins: RuntimePlugin[];
    };
    return runtimeIndexBody.plugins.find((item) => item.pluginId === smokePluginId);
  };

  let plugin = await readRuntimePlugin();
  expect(plugin).toBeTruthy();
  expect(plugin!.receiptId).toBeTruthy();
  expect(plugin!.installState).toBe('installed');
  expect(plugin!.activation).toBe('manifestLoaded');

  const bareModuleUrl = `/api/projects/${pid}/workbench/plugins/${smokePluginId}/frontend-components/${smokeContributionId}/module.js`;
  const blockedModule = await page.request.get(bareModuleUrl);
  expect(blockedModule.status()).toBe(409);

  let managerRow = pluginManagerRow(page, smokePluginId);
  await expect(managerRow).toHaveAttribute('data-install-state', 'installed');
  await managerRow.getByTestId('plugin-manager-enable').click();
  const trustPrompt = managerRow.getByTestId('plugin-manager-trust-prompt');
  await expect(trustPrompt).toBeVisible();
  await expect(trustPrompt).toHaveAttribute('data-plugin-id', smokePluginId);
  await expect(trustPrompt).toHaveAttribute(
    'data-capabilities',
    'plugin:trusted_local_backend',
  );
  await expect(trustPrompt).toHaveAttribute('data-secrets', '');
  await expect(managerRow.getByTestId('plugin-manager-confirm-enable')).toBeDisabled();
  await managerRow
    .getByTestId('plugin-manager-capability-plugin:trusted_local_backend')
    .check();
  await expect(managerRow.getByTestId('plugin-manager-confirm-enable')).toBeEnabled();
  await managerRow.getByTestId('plugin-manager-confirm-enable').click();
  await expect(manager.getByTestId('plugin-manager-status')).toHaveText(
    `enable:${smokePluginId}`,
  );

  plugin = await readRuntimePlugin();
  expect(plugin).toBeTruthy();
  expect(plugin!.installState).toBe('enabled');
  expect(plugin!.registryActivated).toBe(true);
  expect(plugin!.packageSha256).toMatch(/^sha256:[0-9a-f]{64}$/);
  expect(plugin!.source).toMatchObject({ kind: 'localPath', value: pluginRootPath });
  expect(plugin!.contributionSummary.map((item) => `${item.kind}:${item.ids.join(',')}`)).toEqual([
    'workbench_view:frisket.geosmoke.view.real_local_smoke,frisket.geosmoke.view.map',
    'workbench_panel:frisket.core.panel.projection_status',
    `action:${smokeActionId}`,
    'importer:frisket_geosmoke_real_local_smoke_import',
    'operator:frisket.geosmoke.operator.real_local_smoke_equals',
    'projection:frisket.geosmoke.projection.map_points',
  ]);
  expect(plugin!.workbenchDescriptorManifests?.find((item) => item.id === smokeContributionId)).toMatchObject({
    id: smokeContributionId,
    schemaVersion: 'frisket.workbench.view.v1',
  });
  expect(plugin!.workbenchDescriptorManifests?.find((item) => item.id === smokeMapContributionId)).toMatchObject({
    id: smokeMapContributionId,
    schemaVersion: 'frisket.workbench.view.v1',
  });
  const activatedModuleUrl = plugin!.frontendComponentBindings?.find(
    (binding) => binding.contributionId === smokeContributionId,
  )?.moduleUrl;
  expect(activatedModuleUrl?.startsWith(`${bareModuleUrl}?package=sha256%3A`)).toBe(true);
  expect(activatedModuleUrl).toMatch(/[0-9a-f]{64}$/);
  const enabledModule = await page.request.get(activatedModuleUrl!);
  expect(enabledModule.ok()).toBeTruthy();
  expect(await enabledModule.text()).toContain('trusted-local-smoke-map-plugin-ui');

  await openProject(page, pid, sheetId);

  const runtimeView = page.getByTestId('workbench-contribution-frisket-geosmoke-view-real-local-smoke');
  await expect(runtimeView).toBeVisible();
  await expect(runtimeView).toHaveAttribute(
    'data-runtime-component-key',
    'trustedLocal.frisketGeo.realLocalSmoke.MapView',
  );
  const runtimeComponent = page.getByTestId(
    'trusted-local-plugin-component-frisket-geosmoke-view-real-local-smoke',
  );
  await expect(runtimeComponent).toBeVisible();
  await expect(runtimeComponent).toHaveAttribute('data-plugin-id', smokePluginId);
  await expect(runtimeComponent).toHaveAttribute('data-contribution-id', smokeContributionId);
  const pluginUi = page.getByTestId('trusted-local-smoke-map-plugin-ui');
  await expect(pluginUi).toBeVisible();
  await expect(pluginUi).toHaveAttribute('data-loaded-from', 'local-plugin-module');
  await expect(pluginUi).toHaveAttribute('data-schema', 'frisket.plugin_view_context.v1');
  await expect(pluginUi).toHaveAttribute('data-sheet-name', 'places');
  await expect(pluginUi).toHaveAttribute('data-row-count', '1');
  await expect(pluginUi).toHaveAttribute('data-can-open-row', 'true');

  await openPluginManager(page);
  managerRow = pluginManagerRow(page, smokePluginId);
  await managerRow.getByTestId('plugin-manager-activate-backend').click();
  await expect(manager.getByTestId('plugin-manager-status')).toHaveText(
    `backend:${smokePluginId}`,
  );

  managerRow = pluginManagerRow(page, smokePluginId);
  await expect(managerRow).toBeVisible();
  await expect(managerRow).toHaveAttribute('data-install-state', 'enabled');
  await expect(managerRow).toHaveAttribute('data-registry-activated', 'true');
  await expect(managerRow).toHaveAttribute('data-receipt-id', plugin!.receiptId);
  await expect(managerRow).toHaveAttribute(
    'data-workbench-views',
    /frisket\.geosmoke\.view\.real_local_smoke.*frisket\.geosmoke\.view\.map/,
  );
  await expect(managerRow).toHaveAttribute(
    'data-actions',
    smokeActionId,
  );
  await expect(managerRow).toHaveAttribute(
    'data-projections',
    'frisket.geosmoke.projection.map_points',
  );
  await expect(managerRow).toHaveAttribute(
    'data-frontend-bindings',
    new RegExp(`${smokeContributionId}=trustedLocal\\.frisketGeo\\.realLocalSmoke#trustedLocal\\.frisketGeo\\.realLocalSmoke\\.MapView`),
  );
  await page.goto(`/p/${pid}/s/${sheetId}/map/column/${point.id}`);
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geosmoke-view-map')).toHaveCount(0);

  const mapContribution = page.getByTestId('workbench-contribution-frisket-geosmoke-view-map');
  await expect(mapContribution).toBeVisible();
  await expect(mapContribution).toHaveAttribute(
    'data-runtime-component-key',
    'trustedLocal.frisketGeo.realLocalSmoke.MapView',
  );

  const points = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/map/points?column_id=${point.id}`,
  );
  expect(points.ok()).toBeTruthy();
  expect(points.headers()['x-frisket-runtime-projection-kind']).toBe(
    'frisket.geosmoke.projection.map_points',
  );
  expect(points.headers()['x-frisket-runtime-projection-status']).toBe('stale');
  expect(points.headers()['x-frisket-runtime-projection-generation']).toBe(
    'real-local-smoke-gen-1',
  );
  expect(points.headers()['x-frisket-runtime-projection-build-idempotency-key']).toBe(
    'real-local-smoke-map-points@gen-2',
  );

  await openPluginManager(page);
  managerRow = pluginManagerRow(page, smokePluginId);
  await managerRow.getByTestId('plugin-manager-disable').click();
  await expect(page.getByTestId('plugin-manager').getByTestId('plugin-manager-status')).toHaveText(
    `disable:${smokePluginId}`,
  );
  plugin = await readRuntimePlugin();
  expect(plugin?.installState).toBe('disabled');
  managerRow = pluginManagerRow(page, smokePluginId);
  await expect(managerRow).toHaveAttribute('data-install-state', 'disabled');
  await expect(managerRow).toHaveAttribute('data-registry-activated', 'false');

  await managerRow.getByTestId('plugin-manager-uninstall').click();
  await expect(page.getByTestId('plugin-manager').getByTestId('plugin-manager-status')).toHaveText(
    `uninstall:${smokePluginId}`,
  );
  expect(await readRuntimePlugin()).toBeUndefined();
  await expect(pluginManagerRow(page, smokePluginId)).toHaveCount(0);
});
