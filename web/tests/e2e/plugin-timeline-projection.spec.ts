import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

declare global {
  interface Window {
    __FRISKET_DISABLED_PLUGIN_PROJECTION_VIEW_CAPABILITIES__?: string[];
  }
}

const PLUGIN_ID = 'demo.timeline';
const VIEW_ID = 'demo.timeline.view.timeline';
const VIEW_TEST_ID = 'workbench-contribution-demo-timeline-view-timeline';
const COMPONENT_KEY = 'trustedLocal.demoTimeline.TimelineView';
const PROJECTION_KIND = 'demo.timeline.projection.timeline';

async function createTimelineSheet(
  request: APIRequestContext,
  projectId: string,
): Promise<number> {
  const sheetId = await importCsv(
    request,
    projectId,
    'cases.csv',
    [
      'case_id,title,event_date',
      'CASE-001,Initial filing,2026-01-05',
      'CASE-002,Interview complete,2026-01-07',
      'CASE-003,Records received,2026-01-11',
    ].join('\n'),
  );
  const columns = await sheetColumns(request, projectId, sheetId);
  const dateColumn = columns.find((column) => column.name === 'event_date');
  expect(dateColumn).toBeTruthy();
  await setColumnType(request, projectId, dateColumn!.id, 'date');
  return sheetId;
}

async function routeTimelineRuntimeIndex(page: Page, projectId: string) {
  await page.route(`**/api/projects/${projectId}/workbench/plugins`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
        projectId,
        arbitraryPackageLoadAllowed: false,
        receiptScanLimit: 5000,
        skippedInvalidReceipts: 0,
        skippedInvalidManifestRefs: 0,
        loadedPluginCount: 1,
        plugins: [
          {
            schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1',
            pluginId: PLUGIN_ID,
            version: '0.1.0',
            installState: 'enabled',
            activation: 'registryManifestRegistered',
            runtimeSource: 'plugin.load_receipt',
            receiptId: 'receipt_timeline_projection_ui',
            manifestSha256: 'sha256:timeline-projection-ui',
            packageSha256: 'sha256:timeline-projection-package',
            byteCount: 512,
            source: { kind: 'local_file', path: '/plugins/demo_timeline/plugin.json' },
            contributionSummary: [{ kind: 'workbench_view', count: 1, ids: [VIEW_ID] }],
            frontendComponentBindings: [
              {
                schemaVersion: 'frisket.workbench_plugin_frontend_component_binding.v1',
                contributionId: VIEW_ID,
                moduleKey: 'trustedLocal.demoTimeline',
                componentKey: COMPONENT_KEY,
              },
            ],
            workbenchDescriptorPackage: {
              schemaVersion: 'frisket.workbench_descriptor_package.v1',
              sourcePath: 'workbench-descriptors.json',
              descriptorCount: 1,
              runtimeOnlyFieldsStripped: [],
            },
            workbenchDescriptorManifests: [
              {
                schemaVersion: 'frisket.workbench.view.v1',
                id: VIEW_ID,
                kind: 'view',
                ownerPluginId: PLUGIN_ID,
                title: 'Timeline',
                shortTitle: 'Timeline',
                icon: 'Clock',
                placements: [
                  { host: 'mainView', mode: 'pane', slot: 'work.companion', order: 45 },
                ],
                requires: [
                  { kind: 'hostCapability', id: 'projection.status' },
                  { kind: 'hostCapability', id: 'projection.build' },
                  { kind: 'hostCapability', id: 'projection.artifact.read' },
                  { kind: 'hostCapability', id: 'host.navigation.openRow' },
                ],
                dataRequirements: [
                  { kind: 'activeSheet' },
                  { kind: 'sheetHasColumnType', columnType: 'date' },
                ],
                projectionKind: PROJECTION_KIND,
              },
            ],
            requires: {
              capabilities: ['project:read', 'plugin:trusted_local_backend'],
              secrets: [],
            },
            arbitraryPackageLoadAllowed: false,
            registryActivated: true,
            // Required-but-nullable in the wire contract (the backend always
            // serializes these two keys — src/frisket/authoring/workbench/plugin_runtime.py
            // build_runtime_plugin_entry — never omits them).
            installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
            disabledReason: null,
          },
        ],
        // Required by WorkbenchPluginRuntimeIndex (web/src/api/types.ts) and
        // enforced at runtime by the generated contract validator
        // (validateWorkbenchPluginRuntimeIndex, web/src/generated/openHttpContracts.ts,
        // wired in real.ts's getWorkbenchPluginRuntimeIndex). The real backend
        // (src/frisket/authoring/workbench/plugin_runtime.py) always includes this section;
        // omitting it fails contract validation and silently nulls the runtime
        // index (pluginLayout.setRuntimeIndex(null)), so the mocked plugin never
        // resolves into a layout item.
        firstParty: {
          schemaVersion: 'frisket.workbench_descriptor_package.v1',
          descriptors: [],
        },
      }),
    });
  });
}

test('missing required projection capability prevents timeline mount', async ({ page }) => {
  await page.addInitScript(() => {
    window.__FRISKET_DISABLED_PLUGIN_PROJECTION_VIEW_CAPABILITIES__ = [
      'projection.artifact.read',
    ];
  });
  const projectId = await createProject(page.request, uniqueName('plugin-timeline-projection-cap'));
  const sheetId = await createTimelineSheet(page.request, projectId);
  await routeTimelineRuntimeIndex(page, projectId);

  await openProject(page, projectId, sheetId);

  await expect(page.getByTestId('plugin-timeline-projection-view')).toHaveCount(0);
  const unavailable = page.getByTestId('plugin-view-unavailable-demo-timeline-view-timeline');
  await expect(unavailable).toBeVisible();
  await expect(unavailable).toHaveAttribute(
    'data-reason',
    'missing_capability:projection.artifact.read',
  );
  await expect(page.getByTestId(VIEW_TEST_ID)).toHaveAttribute(
    'data-plugin-view-unavailable-reason',
    'missing_capability:projection.artifact.read',
  );
});
