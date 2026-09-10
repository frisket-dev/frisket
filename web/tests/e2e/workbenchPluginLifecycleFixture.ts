import { expect, type Page, type Route } from '@playwright/test';

export const geoPluginId = 'frisket.geo';
export const geoContributionId = 'frisket.geo.view.map';
export const geoPluginSourceValue = '/Users/example/plugins/frisket-geo';

type RuntimeState = 'missing' | 'installed' | 'enabled' | 'disabled' | 'uninstalled';

interface RouteGeoPluginLifecycleOptions {
  receiptId?: string;
  manifestSha?: string;
  moduleKey?: string;
  componentKey?: string;
  moduleUrl?: string;
  initialState?: RuntimeState;
}

interface RouteGeoPluginLifecycleResult {
  calls: string[];
  installBody: () => unknown;
  activationBody: () => unknown;
  backendActivationBody: () => unknown;
  state: () => RuntimeState;
}

function runtimePlugin(
  installState: 'installed' | 'enabled' | 'disabled' | 'uninstalled',
  options: Required<Omit<RouteGeoPluginLifecycleOptions, 'initialState'>>,
) {
  return {
    schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1',
    pluginId: geoPluginId,
    version: '0.1.0',
    installState,
    activation:
      installState === 'enabled'
        ? 'registryManifestRegistered'
        : installState === 'disabled'
          ? 'blocked'
          : installState === 'uninstalled'
            ? 'removed'
            : 'manifestLoaded',
    runtimeSource: 'plugin.load_receipt',
    receiptId: options.receiptId,
    manifestSha256: options.manifestSha,
    byteCount: 512,
    source: {
      kind: 'local_file',
      path: `${geoPluginSourceValue}/plugin.json`,
    },
    contributionSummary: [
      { kind: 'workbench_view', count: 1, ids: [geoContributionId] },
      {
        kind: 'workbench_panel',
        count: 1,
        ids: ['frisket.core.panel.projection_status'],
      },
      { kind: 'action', count: 1, ids: ['frisket.geo.action.real_local_smoke'] },
      {
        kind: 'importer',
        count: 1,
        ids: ['frisket_geo_real_local_smoke_import'],
      },
      {
        kind: 'operator',
        count: 1,
        ids: ['frisket.geo.operator.real_local_smoke_equals'],
      },
      { kind: 'projection', count: 1, ids: ['frisket.geo.projection.map_points'] },
    ],
    frontendComponentBindings: [
      {
        schemaVersion: 'frisket.workbench_plugin_frontend_component_binding.v1',
        contributionId: geoContributionId,
        moduleKey: options.moduleKey,
        componentKey: options.componentKey,
        moduleUrl: options.moduleUrl,
      },
    ],
    workbenchDescriptorPackage: {
      schemaVersion: 'frisket.workbench_descriptor_package.v1',
      sourcePath: `${geoPluginSourceValue}/workbench-descriptors.json`,
      descriptorCount: 1,
      runtimeOnlyFieldsStripped: [],
    },
    // A FULL map view descriptor (geo-bundled-plugin-v1): the app shell now
    // derives its map affordances from the runtime index, so the mocked
    // descriptor must parse like the real bundled one
    // (src/frisket/authoring/bundled_plugins/frisket.geo/workbench-descriptors.json) —
    // title/requires/dataRequirements/projectionKind included.
    workbenchDescriptorManifests: [
      {
        schemaVersion: 'frisket.workbench.view.v1',
        id: geoContributionId,
        kind: 'view',
        ownerPluginId: geoPluginId,
        title: 'Map',
        icon: 'MapPin',
        projectionKind: 'frisket.geo.projection.map_points',
        placements: [
          {
            host: 'mainView',
            mode: 'pane',
            slot: 'work.companion',
            placementId: 'frisket-geo-map-companion',
          },
        ],
        requires: [
          { kind: 'hostCapability', id: 'projection.status' },
          { kind: 'hostCapability', id: 'grid.filter.applyBbox' },
          { kind: 'hostCapability', id: 'host.navigation.openRow' },
        ],
        dataRequirements: [
          { kind: 'sheetHasColumnType', columnType: 'geo_point' },
        ],
      },
    ],
    requires: {
      capabilities: ['project:read', 'local.external.activate'],
      secrets: [],
    },
    arbitraryPackageLoadAllowed: false,
    registryActivated: installState === 'enabled',
    packageSha256: 'sha256:geo-plugin-package',
    installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
    disabledReason: installState === 'disabled' ? 'plugin_disabled' : null,
  };
}

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

export async function routeGeoPluginLifecycle(
  page: Page,
  projectId: string,
  options: RouteGeoPluginLifecycleOptions = {},
): Promise<RouteGeoPluginLifecycleResult> {
  const fixture = {
    receiptId: options.receiptId ?? 'receipt_geo_plugin_lifecycle',
    manifestSha: options.manifestSha ?? 'sha256:geo-plugin-lifecycle',
    moduleKey: options.moduleKey ?? 'trustedLocal.frisketGeo.lifecycle',
    componentKey: options.componentKey ?? 'trustedLocal.frisketGeo.lifecycle.MapView',
    moduleUrl: options.moduleUrl ?? '',
  };
  let state = options.initialState ?? 'missing';
  const calls: string[] = [];
  let installBody: unknown = null;
  let activationBody: unknown = null;
  let backendActivationBody: unknown = null;

  await page.route(`**/api/projects/${projectId}/workbench/plugins`, async (route) => {
    const plugins =
      state === 'missing'
        ? []
        : [runtimePlugin(state, fixture)];
    await fulfillJson(route, {
      schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
      projectId,
      arbitraryPackageLoadAllowed: false,
      receiptScanLimit: 5000,
      skippedInvalidReceipts: 0,
      skippedInvalidManifestRefs: 0,
      loadedPluginCount: plugins.length,
      plugins,
      firstParty: {
        schemaVersion: 'frisket.workbench_descriptor_package.v1',
        descriptors: [],
      },
    });
  });

  await page.route(
    `**/api/projects/${projectId}/workbench/plugins/${geoPluginId}/install-local`,
    async (route) => {
      calls.push('install-local');
      installBody = route.request().postDataJSON();
      state = 'installed';
      await fulfillJson(route, {
        schemaVersion: 'frisket.plugin_install_plan_execution.v1',
        projectId,
        pluginId: geoPluginId,
        source: { kind: 'localPath', value: geoPluginSourceValue },
        installState: 'installed',
        activation: 'manifestLoaded',
        runtimeSource: 'plugin.load_receipt',
        receiptId: fixture.receiptId,
        manifestSha256: fixture.manifestSha,
        packageSha256: 'sha256:geo-plugin-package',
        arbitraryPackageLoadAllowed: false,
        installFailure: null,
        layoutMutated: false,
      });
    },
  );

  await page.route(
    `**/api/projects/${projectId}/workbench/plugins/${geoPluginId}/activate`,
    async (route) => {
      calls.push('activate');
      activationBody = route.request().postDataJSON();
      state = 'enabled';
      await fulfillJson(route, {
        schemaVersion: 'frisket.workbench_plugin_activation.v1',
        projectId,
        pluginId: geoPluginId,
        receiptId: fixture.receiptId,
        manifestSha256: fixture.manifestSha,
        packageSha256: 'sha256:geo-plugin-package',
        runtimeSource: 'plugin.load_receipt',
        activation: 'registryManifestRegistered',
        installState: 'enabled',
        registryActivated: true,
        arbitraryPackageLoadAllowed: false,
        permissionsAccepted: ['project:read', 'local.external.activate'],
        registeredPluginManifests: [geoPluginId],
      });
    },
  );

  await page.route(
    `**/api/projects/${projectId}/workbench/plugins/${geoPluginId}/backend/activate`,
    async (route) => {
      calls.push('backend-activate');
      backendActivationBody = route.request().postDataJSON();
      await fulfillJson(route, {
        schemaVersion: 'frisket.workbench_plugin_backend_activation.v1',
        projectId,
        pluginId: geoPluginId,
        receiptId: fixture.receiptId,
        manifestSha256: fixture.manifestSha,
        packageSha256: 'sha256:geo-plugin-package',
        registeredRuntimeBindings: {
          actions: ['frisket.geo.action.real_local_smoke'],
          importers: ['frisket_geo_real_local_smoke_import'],
          operators: ['frisket.geo.operator.real_local_smoke_equals'],
          projections: ['frisket.geo.projection.map_points'],
          columnTypes: [],
          jobHandlers: [],
        },
        trustedRuntimeBindingsRegistered: true,
        arbitraryPackageLoadAllowed: false,
      });
    },
  );

  await page.route(
    `**/api/projects/${projectId}/workbench/plugins/${geoPluginId}/disable`,
    async (route) => {
      calls.push('disable');
      state = 'disabled';
      await fulfillJson(route, {
        schemaVersion: 'frisket.workbench_plugin_install_state.v1',
        projectId,
        pluginId: geoPluginId,
        receiptId: fixture.receiptId,
        manifestSha256: fixture.manifestSha,
        packageSha256: 'sha256:geo-plugin-package',
        installState: 'disabled',
        activation: 'blocked',
        runtimeSource: 'plugin.load_receipt',
        permissionsAccepted: ['project:read', 'local.external.activate'],
        registryActivated: false,
        arbitraryPackageLoadAllowed: false,
        disabledReason: 'plugin_disabled',
      });
    },
  );

  await page.route(
    `**/api/projects/${projectId}/workbench/plugins/${geoPluginId}/uninstall`,
    async (route) => {
      calls.push('uninstall');
      state = 'uninstalled';
      await fulfillJson(route, {
        schemaVersion: 'frisket.workbench_plugin_install_state.v1',
        projectId,
        pluginId: geoPluginId,
        receiptId: fixture.receiptId,
        manifestSha256: fixture.manifestSha,
        packageSha256: 'sha256:geo-plugin-package',
        installState: 'uninstalled',
        activation: 'removed',
        runtimeSource: 'plugin.load_receipt',
        permissionsAccepted: ['project:read', 'local.external.activate'],
        registryActivated: false,
        arbitraryPackageLoadAllowed: false,
        disabledReason: null,
      });
    },
  );

  return {
    calls,
    installBody: () => installBody,
    activationBody: () => activationBody,
    backendActivationBody: () => backendActivationBody,
    state: () => state,
  };
}

export function pluginManager(page: Page) {
  return page.getByTestId('plugin-manager');
}

export async function openPluginManager(page: Page) {
  const match = page.url().match(/\/p\/([^/?#]+)/);
  if (!match) throw new Error(`cannot infer project id from ${page.url()}`);
  await page.goto(`/p/${decodeURIComponent(match[1])}/settings/project/plugins`);
  const manager = pluginManager(page);
  await expect(manager).toBeVisible();
  return manager;
}

export function pluginManagerRow(page: Page, pluginId = geoPluginId) {
  return pluginManager(page)
    .getByTestId('plugin-manager-installed-plugin')
    .filter({ hasText: pluginId });
}

export async function installGeoPluginFromManager(page: Page) {
  const manager = await openPluginManager(page);
  await manager.getByTestId('plugin-manager-install-plugin-id').fill(geoPluginId);
  await manager.getByTestId('plugin-manager-install-source').fill(geoPluginSourceValue);
  await manager.getByTestId('plugin-manager-install-local').click();
  await expect(manager.getByTestId('plugin-manager-status')).toHaveText(
    `install:${geoPluginId}`,
  );
  const row = pluginManagerRow(page);
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute('data-install-state', 'installed');
  return row;
}

export async function enableGeoPluginFromManager(page: Page) {
  await openPluginManager(page);
  const row = pluginManagerRow(page);
  await row.getByTestId('plugin-manager-enable').click();
  const trustPrompt = row.getByTestId('plugin-manager-trust-prompt');
  await expect(trustPrompt).toBeVisible();
  await expect(trustPrompt).toHaveAttribute('data-plugin-id', geoPluginId);
  await expect(trustPrompt).toHaveAttribute(
    'data-capabilities',
    'project:read local.external.activate',
  );
  await expect(row.getByTestId('plugin-manager-confirm-enable')).toBeDisabled();
  await row.getByTestId('plugin-manager-capability-project:read').check();
  await row.getByTestId('plugin-manager-capability-local.external.activate').check();
  await expect(row.getByTestId('plugin-manager-confirm-enable')).toBeEnabled();
  await row.getByTestId('plugin-manager-confirm-enable').click();
  await expect(pluginManager(page).getByTestId('plugin-manager-status')).toHaveText(
    `enable:${geoPluginId}`,
  );
  await expect(row).toHaveAttribute('data-install-state', 'enabled');
  return row;
}

export async function activateGeoBackendFromManager(page: Page) {
  await openPluginManager(page);
  const row = pluginManagerRow(page);
  await row.getByTestId('plugin-manager-activate-backend').click();
  await expect(pluginManager(page).getByTestId('plugin-manager-status')).toHaveText(
    `backend:${geoPluginId}`,
  );
  return row;
}
