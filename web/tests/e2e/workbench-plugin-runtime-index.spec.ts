import { expect, test } from '@playwright/test';
import { listSheets, openProject, projectIdByName } from './helpers';
import {
  openPluginManager,
  pluginManagerRow,
} from './workbenchPluginLifecycleFixture';

let pid: string;

test.beforeEach(async ({ page }) => {
  pid = await projectIdByName(page.request, 'Local stories');
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
            // Required (non-optional) in the generated wire contract
            // (HttpWorkbenchPluginRuntimeIndex_WorkbenchPluginRuntimePluginV1,
            // web/src/generated/openHttpContracts.ts) even though this installed
            // (not yet activated) plugin has not registered with the process
            // registry.
            registryActivated: false,
            // Required-but-nullable in the wire contract (the backend always
            // serializes these two keys — src/frisket/authoring/workbench/plugin_runtime.py
            // build_runtime_plugin_entry — never omits them; see 533e8cb3's fix
            // to workbench-contribution-visibility-controls.spec.ts for the
            // same omission).
            installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
            disabledReason: null,
          },
        ],
        // Required by WorkbenchPluginRuntimeIndex (web/src/api/types.ts) and
        // enforced at runtime by the generated contract validator
        // (validateWorkbenchPluginRuntimeIndex, web/src/generated/openHttpContracts.ts).
        // Omitting this fails contract validation and silently nulls out the
        // runtime index (pluginLayout.setRuntimeIndex(null) / the manager's
        // runtimeIndex prop), which is why data-schema-version read null.
        firstParty: {
          schemaVersion: 'frisket.workbench_descriptor_package.v1',
          descriptors: [],
        },
      }),
    });
  });
  const sheets = await listSheets(page.request, pid);
  await openProject(page, pid, sheets[0].id);
});

test('generic plugin manager reads backend plugin runtime index evidence', async ({
  page,
}) => {
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

  // The dock's Plugins tab retired — it was a read-only duplicate of this same
  // Settings manager (mode="status" vs. this route's mode="settings"), so
  // there is no longer a second surface to prove parity against; Settings is
  // the sole manager now, and the dock has no Plugins tab at all.
  const sheets = await listSheets(page.request, pid);
  await openProject(page, pid, sheets[0].id);
  await expect(page.getByTestId('bottom-dock-tab-plugins')).toHaveCount(0);
});
