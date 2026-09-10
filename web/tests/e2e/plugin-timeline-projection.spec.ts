import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  setColumnType,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

type ProjectionSpyEvent =
  | { kind: 'status'; contributionId: string; projectionKind: string }
  | {
      kind: 'build';
      contributionId: string;
      projectionKind: string;
      mode: 'refresh' | 'rebuild';
    }
  | { kind: 'artifactRead'; contributionId: string; projectionKind: string; artifactId: string }
  | { kind: 'openRow'; contributionId: string; rowId: string };

declare global {
  interface Window {
    __FRISKET_PLUGIN_PROJECTION_VIEW_TEST_EVENTS__?: ProjectionSpyEvent[];
    __FRISKET_DISABLED_PLUGIN_PROJECTION_VIEW_CAPABILITIES__?: string[];
    __FRISKET_PLUGIN_PROJECTION_VIEW_TEST_SPIES__?: {
      status?: (args: { contributionId: string; projectionKind: string }) => void;
      build?: (args: {
        contributionId: string;
        projectionKind: string;
        mode: 'refresh' | 'rebuild';
      }) => void;
      artifactRead?: (args: {
        contributionId: string;
        projectionKind: string;
        artifactId: string;
      }) => void;
      openRow?: (args: { contributionId: string; rowId: string }) => void;
    };
  }
}

const PLUGIN_ID = 'demo.timeline';
const VIEW_ID = 'demo.timeline.view.timeline';
const VIEW_TEST_ID = 'workbench-contribution-demo-timeline-view-timeline';
const COMPONENT_KEY = 'trustedLocal.demoTimeline.TimelineView';
const PROJECTION_KIND = 'demo.timeline.projection.timeline';
const ARTIFACT_ID = 'projection_artifact:demo.timeline:e2e';

interface TimelineFixture {
  sheetId: number;
  firstRowId: string;
  dateColumnId: number;
  titleColumnId: number;
  caseColumnId: number;
}

async function createTimelineSheet(
  request: APIRequestContext,
  projectId: string,
): Promise<TimelineFixture> {
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
  const titleColumn = columns.find((column) => column.name === 'title');
  const caseColumn = columns.find((column) => column.name === 'case_id');
  expect(dateColumn).toBeTruthy();
  expect(titleColumn).toBeTruthy();
  expect(caseColumn).toBeTruthy();
  await setColumnType(request, projectId, dateColumn!.id, 'date');
  const data = await sheetData(request, projectId, sheetId, 0, 3);
  return {
    sheetId,
    firstRowId: String(data.rows[0].id),
    dateColumnId: dateColumn!.id,
    titleColumnId: titleColumn!.id,
    caseColumnId: caseColumn!.id,
  };
}

async function installProjectionSpies(page: Page) {
  await page.addInitScript(() => {
    window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_EVENTS__ = [];
    window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_SPIES__ = {
      status: (args) => {
        window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_EVENTS__?.push({
          kind: 'status',
          ...args,
        });
      },
      build: (args) => {
        window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_EVENTS__?.push({
          kind: 'build',
          ...args,
        });
      },
      artifactRead: (args) => {
        window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_EVENTS__?.push({
          kind: 'artifactRead',
          ...args,
        });
      },
      openRow: (args) => {
        window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_EVENTS__?.push({
          kind: 'openRow',
          ...args,
        });
      },
    };
  });
}

async function projectionEvents(page: Page): Promise<ProjectionSpyEvent[]> {
  return page.evaluate(() => window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_EVENTS__ ?? []);
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

async function routeProjectionApi(page: Page, projectId: string, fixture: TimelineFixture) {
  let built = false;
  const items = [
    {
      sourceRowId: fixture.firstRowId,
      date: '2026-01-05',
      title: 'Initial filing',
      caseId: 'CASE-001',
    },
    {
      sourceRowId: String(Number(fixture.firstRowId) + 1),
      date: '2026-01-07',
      title: 'Interview complete',
      caseId: 'CASE-002',
    },
    {
      sourceRowId: String(Number(fixture.firstRowId) + 2),
      date: '2026-01-11',
      title: 'Records received',
      caseId: 'CASE-003',
    },
  ];

  await page.route(`**/api/projects/${projectId}/projections/runtime/status`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schemaVersion: 'frisket.runtime_projection_status.v1',
        status: built ? 'ready' : 'missing',
        freshness: {
          state: built ? 'fresh' : 'missing',
          generation: built ? 'e2e-generation-1' : null,
          transient: false,
        },
        outputs: {
          artifactRefs: built
            ? [
                {
                  kind: 'projection_artifact',
                  projectionKind: PROJECTION_KIND,
                  artifactId: ARTIFACT_ID,
                },
              ]
            : [],
          metrics: built ? { sourceRowCount: 3, timelineItemCount: 3 } : {},
        },
        warnings: [],
      }),
    });
  });

  await page.route(`**/api/projects/${projectId}/projections/runtime/build`, async (route) => {
    built = true;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schemaVersion: 'frisket.runtime_projection_build_plan.v1',
        status: 'accepted',
        build: {
          operation: 'refresh',
          idempotencyKey: 'timeline-projection-e2e-refresh',
        },
        outputs: {
          artifactRefs: [
            {
              kind: 'projection_artifact',
              projectionKind: PROJECTION_KIND,
              artifactId: ARTIFACT_ID,
            },
          ],
          metrics: { sourceRowCount: 3, timelineItemCount: 3 },
        },
        warnings: [],
      }),
    });
  });

  await page.route(`**/api/projects/${projectId}/projections/runtime/artifact`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schemaVersion: 'frisket.timeline_projection_artifact.v1',
        projectionKind: PROJECTION_KIND,
        artifactId: ARTIFACT_ID,
        generation: 'e2e-generation-1',
        target: {
          sheetId: String(fixture.sheetId),
          dateColumnId: String(fixture.dateColumnId),
          titleColumnId: String(fixture.titleColumnId),
          caseColumnId: String(fixture.caseColumnId),
        },
        params: {},
        columns: {
          dateColumnId: String(fixture.dateColumnId),
          titleColumnId: String(fixture.titleColumnId),
          caseColumnId: String(fixture.caseColumnId),
        },
        metrics: {
          sourceRowCount: 3,
          timelineItemCount: 3,
        },
        items,
      }),
    });
  });
}

test('timeline projection view mounts from runtime index and reads a projection artifact', async ({
  page,
}) => {
  await installProjectionSpies(page);
  const projectId = await createProject(page.request, uniqueName('plugin-timeline-projection'));
  const fixture = await createTimelineSheet(page.request, projectId);
  await routeTimelineRuntimeIndex(page, projectId);
  await routeProjectionApi(page, projectId, fixture);

  await openProject(page, projectId, fixture.sheetId);

  const layoutItem = page.getByTestId('workbench-resolved-layout-item-demo-timeline-view-timeline');
  await expect(layoutItem).toHaveAttribute('data-status', 'enabled');
  await expect(layoutItem).toHaveAttribute('data-runtime-source', 'runtimeIndex');

  const frame = page.getByTestId(VIEW_TEST_ID);
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute('data-contribution-id', VIEW_ID);
  await expect(frame).toHaveAttribute('data-host', 'mainView');
  await expect(frame).toHaveAttribute('data-mode', 'pane');
  await expect(frame).toHaveAttribute('data-runtime-component-key', COMPONENT_KEY);
  await expect(frame).toHaveAttribute(
    'data-required-capabilities',
    'projection.status projection.build projection.artifact.read host.navigation.openRow',
  );
  await expect(frame).toHaveAttribute(
    'data-plugin-projection-view-context-schema-version',
    'frisket.plugin_projection_view_context.v1',
  );
  await expect(frame).toHaveAttribute('data-plugin-projection-kind', PROJECTION_KIND);

  const timeline = page.getByTestId('plugin-timeline-projection-view');
  await expect(timeline).toBeVisible();
  await expect(timeline).toHaveAttribute('data-status', 'ready');
  await expect(timeline).toHaveAttribute('data-freshness-state', 'fresh');
  await expect(timeline).toHaveAttribute('data-artifact-id', ARTIFACT_ID);
  await expect(timeline).toHaveAttribute('data-timeline-item-count', '3');
  await expect(timeline).toHaveAttribute('data-date-column-id', String(fixture.dateColumnId));
  await expect(timeline).toContainText('Initial filing');
  await expect(timeline).toContainText('Records received');

  const eventsBeforeOpen = await projectionEvents(page);
  expect(eventsBeforeOpen.filter((event) => event.kind === 'status').length).toBeGreaterThanOrEqual(
    2,
  );
  expect(eventsBeforeOpen.filter((event) => event.kind === 'build').length).toBeGreaterThanOrEqual(
    1,
  );
  expect(
    eventsBeforeOpen.filter((event) => event.kind === 'artifactRead').length,
  ).toBeGreaterThanOrEqual(1);

  await page.getByTestId('timeline-projection-item').first().click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  const events = await projectionEvents(page);
  expect(events).toContainEqual({
    kind: 'openRow',
    contributionId: VIEW_ID,
    rowId: fixture.firstRowId,
  });
});

test('missing required projection capability prevents timeline mount', async ({ page }) => {
  await page.addInitScript(() => {
    window.__FRISKET_DISABLED_PLUGIN_PROJECTION_VIEW_CAPABILITIES__ = [
      'projection.artifact.read',
    ];
  });
  const projectId = await createProject(page.request, uniqueName('plugin-timeline-projection-cap'));
  const fixture = await createTimelineSheet(page.request, projectId);
  await routeTimelineRuntimeIndex(page, projectId);

  await openProject(page, projectId, fixture.sheetId);

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
