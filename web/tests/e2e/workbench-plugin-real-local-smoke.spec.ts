import path from 'node:path';
import { fileURLToPath } from 'node:url';

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
  openPluginManager,
  pluginManagerRow,
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

test('real local plugin load activates visible Map contribution and backend projection binding', async ({
  page,
}) => {
  await mockBasemapTiles(page);
  const pid = await createProject(page.request, uniqueName('real-local-plugin-smoke'));
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

  await openProject(page, pid, sheetId);
  const manager = await openPluginManager(page);
  await expect(manager).toBeVisible();
  // Pin revision (geo-bundled-plugin-v1, updated for decision 15's
  // frisket.transliterate bonus plugin): a fresh project ships with every
  // packaged BUNDLED plugin seeded through the public bundled install path at
  // project creation (bootstrap_project_bundled_plugins iterates the whole
  // package directory) — currently frisket.geo, frisket.media, the dormant
  // frisket.ftm importer (ftm-bundled-plugin-v1, auto_enable:false but still
  // installed like the media precedent), and frisket.transliterate
  // (auto_enable:true), so the manager shows four rows. This
  // spec exercises the standalone geo smoke-twin lifecycle. The twin ships under
  // its OWN non-colliding plugin id (frisket.geosmoke) so activating it never
  // shadows the workspace-resident bundled frisket.geo
  // (plugin_activation_registry_conflict — a same-id/different-evidence package
  // is refused by design). We still retire the bundled frisket.geo first —
  // disable, then uninstall — so exactly one map view resolves from the runtime
  // index. frisket.media, frisket.ftm, and frisket.transliterate are left
  // untouched throughout; the bundled row is targeted by frisket.geo, the twin
  // below by frisket.geosmoke.
  await expect(manager).toHaveAttribute('data-plugin-count', '4');
  let bundledRow = pluginManagerRow(page);
  // Normally 'enabled'; a prior in-process registry swap (this shared e2e
  // server hosts every spec) can leave bootstrap at 'installed'.
  if ((await bundledRow.getAttribute('data-install-state')) === 'enabled') {
    await bundledRow.getByTestId('plugin-manager-disable').click();
    await expect(manager.getByTestId('plugin-manager-status')).toHaveText(
      'disable:frisket.geo',
    );
  }
  bundledRow = pluginManagerRow(page);
  await bundledRow.getByTestId('plugin-manager-uninstall').click();
  await expect(manager.getByTestId('plugin-manager-status')).toHaveText(
    'uninstall:frisket.geo',
  );
  await expect(pluginManagerRow(page)).toHaveAttribute(
    'data-install-state',
    'uninstalled',
  );
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

  // The plugin manager lives on the settings route; workbench contributions
  // mount on the project work surface. Return there to observe the mount.
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
  // Pin revision (geo-bundled-plugin-v1): the frame stamps the smoke twin's
  // BINDING componentKey, not the removed first-party registry key.
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
  plugin = await readRuntimePlugin();
  expect(plugin).toBeTruthy();
  expect(plugin!.installState).toBe('uninstalled');
  expect(plugin!.activation).toBe('removed');
  expect(plugin!.registryActivated).toBe(false);
  managerRow = pluginManagerRow(page, smokePluginId);
  await expect(managerRow).toHaveAttribute('data-install-state', 'uninstalled');
  await expect(managerRow.getByTestId('plugin-manager-uninstall')).toBeDisabled();
});
