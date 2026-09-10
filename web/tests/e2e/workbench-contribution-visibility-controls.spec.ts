import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  addRow,
  createProject,
  editCells,
  importCsv,
  listSheets,
  openProject,
  setColumnType,
  setHiddenContributions,
  sheetColumns,
  sheetData,
  solidTilePng,
  uniqueName,
} from './helpers';

const IMAGE_GALLERY_ID = 'frisket.media.view.image_gallery';
const IMAGE_GALLERY_TEST_ID = 'workbench-contribution-frisket-media-view-image-gallery';
const GRAPH_ID = 'frisket.investigative.view.graph_neighborhood';
const GRAPH_TEST_ID = 'workbench-contribution-frisket-investigative-view-graph-neighborhood';
const REVIEW_QUEUE_ID = 'frisket.core.view.review_queue';
const RUNTIME_VIEW_ID = 'demo.visibility.view.runtime_hidden';
const RUNTIME_PLUGIN_ID = 'demo.visibility';
const RUNTIME_VIEW_TEST_ID = 'trusted-local-plugin-component-demo-visibility-view-runtime-hidden';

interface ImageSheetFixture {
  sheetId: number;
  firstRowId: number;
  mediaColumnId: number;
  mediaColumnName: string;
  firstMediaCell: Record<string, unknown>;
}

async function importImageSheet(
  request: APIRequestContext,
  pid: string,
  count = 3,
): Promise<ImageSheetFixture> {
  const png = solidTilePng(16, [68, 120, 178, 255]);
  const response = await request.post(`/api/projects/${pid}/import/files?sheet_name=gallery`, {
    multipart: {
      files: { name: 'gallery-01.png', mimeType: 'image/png', buffer: png },
    },
  });
  expect(response.ok()).toBeTruthy();
  const sheets = await listSheets(request, pid);
  const sheet = sheets.find((candidate) => candidate.name === 'gallery');
  expect(sheet).toBeTruthy();
  const columns = await sheetColumns(request, pid, sheet!.id);
  const mediaColumn = columns.find((column) => column.name === 'media');
  expect(mediaColumn).toBeTruthy();
  const firstPage = await sheetData(request, pid, sheet!.id, 0, 1);
  const firstRow = firstPage.rows[0];
  expect(firstRow).toBeTruthy();
  const firstCell = firstRow?.cells[String(mediaColumn!.id)];
  expect(firstCell).toBeTruthy();
  for (let index = 2; index <= count; index += 1) {
    await addRow(request, pid, sheet!.id, {
      media: {
        ...(firstCell as Record<string, unknown>),
        filename: `gallery-${String(index).padStart(2, '0')}.png`,
      },
    });
  }
  return {
    sheetId: sheet!.id,
    firstRowId: firstRow!.id,
    mediaColumnId: mediaColumn!.id,
    mediaColumnName: mediaColumn!.name,
    firstMediaCell: firstCell as Record<string, unknown>,
  };
}

async function preferColumnOrder(
  page: Page,
  pid: string,
  sheetId: number,
  order: string[],
) {
  await page.addInitScript(
    ({ pid, sheetId, order }) => {
      localStorage.setItem(`frisket:column-order:${pid}:${sheetId}`, JSON.stringify(order));
    },
    { pid, sheetId, order },
  );
}

async function openCommandPalette(page: Page) {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  return palette;
}

async function routeRuntimeMainViewPlugin(
  page: Page,
  projectId: string,
  options: { contributionId?: string; initiallyPending?: boolean; title?: string } = {},
): Promise<() => void> {
  const contributionId = options.contributionId ?? RUNTIME_VIEW_ID;
  const title = options.title ?? 'Runtime hidden view';
  const moduleUrl = `data:text/javascript;charset=utf-8,${encodeURIComponent(`
    export function RuntimeHiddenView({ React, contributionId, pluginId }) {
      return React.createElement(
        'div',
        {
          'data-testid': 'runtime-hidden-view-inner',
          'data-contribution-id': contributionId,
          'data-plugin-id': pluginId,
        },
        'Runtime hidden view'
      );
    }
  `)}`;
  let releaseRoute = () => {};
  const delayedRoute = options.initiallyPending === true;
  let released = !delayedRoute;
  const releasePromise = new Promise<void>((resolve) => {
    releaseRoute = () => {
      released = true;
      resolve();
    };
  });

  await page.route(`**/api/projects/${projectId}/workbench/plugins`, async (route) => {
    if (!released) {
      await releasePromise;
    }
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
            pluginId: RUNTIME_PLUGIN_ID,
            version: '0.1.0',
            installState: 'enabled',
            activation: 'registryManifestRegistered',
            runtimeSource: 'plugin.load_receipt',
            receiptId: 'receipt_visibility_runtime_hidden',
            manifestSha256: 'sha256:visibility-runtime-hidden',
            packageSha256: 'sha256:visibility-runtime-package',
            byteCount: 512,
            source: { kind: 'local_file', path: '/plugins/demo_visibility/plugin.json' },
            contributionSummary: [{ kind: 'workbench_view', count: 1, ids: [contributionId] }],
            frontendComponentBindings: [
              {
                schemaVersion: 'frisket.workbench_plugin_frontend_component_binding.v1',
                contributionId,
                moduleKey: 'trustedLocal.demoVisibility',
                componentKey: 'trustedLocal.demoVisibility.RuntimeHiddenView',
                moduleUrl,
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
                id: contributionId,
                kind: 'view',
                ownerPluginId: RUNTIME_PLUGIN_ID,
                title,
                shortTitle: 'Runtime hidden',
                icon: 'PanelRight',
                placements: [
                  {
                    host: 'mainView',
                    mode: 'pane',
                    slot: 'work.companion',
                    order: 4,
                    placementId: 'demo-runtime-hidden-view',
                  },
                ],
                requires: [{ kind: 'hostCapability', id: 'sheet.rows.read' }],
                dataRequirements: [{ kind: 'activeSheet' }],
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
        // wired in real.ts's getWorkbenchPluginRuntimeIndex since "Generate
        // frontend HTTP contract artifacts", 862a913f). The real backend
        // (src/frisket/authoring/workbench/plugin_runtime.py) always includes this
        // section; omitting it here fails contract validation and silently
        // nulls out the runtime index (pluginLayout.setRuntimeIndex(null)),
        // which is why the mocked plugin never showed up as a resolved
        // layout item.
        firstParty: {
          schemaVersion: 'frisket.workbench_descriptor_package.v1',
          descriptors: [],
        },
      }),
    });
  });

  return releaseRoute;
}

test('image gallery visibility is user controlled across reloads and sheet changes', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-workbench-visibility-gallery'));
  const imageFixture = await importImageSheet(page.request, pid, 3);
  const imageSheetId = imageFixture.sheetId;
  const imageColumns = await sheetColumns(page.request, pid, imageSheetId);
  const filenameColumn = imageColumns.find((column) => column.name === 'filename');
  expect(filenameColumn).toBeTruthy();
  await setColumnType(page.request, pid, filenameColumn!.id, 'image');
  await editCells(page.request, pid, [
    {
      rowId: imageFixture.firstRowId,
      columnId: filenameColumn!.id,
      value: {
        ...imageFixture.firstMediaCell,
        filename: 'visible-first-01.png',
      },
    },
  ]);
  await preferColumnOrder(page, pid, imageSheetId, [
    filenameColumn!.name,
    imageFixture.mediaColumnName,
    ...imageColumns
      .map((column) => column.name)
      .filter((name) => name !== filenameColumn!.name && name !== imageFixture.mediaColumnName),
  ]);
  const textSheetId = await importCsv(
    page.request,
    pid,
    'notes.csv',
    'title\nNo images here\nStill no media\n',
  );

  await openProject(page, pid, imageSheetId);

  await expect(page.getByTestId(IMAGE_GALLERY_TEST_ID)).toBeVisible();
  await expect(page.getByTestId('plugin-image-gallery')).toBeVisible();
  await expect(page.getByTestId('workbench-mainView-split')).toBeVisible();

  let palette = await openCommandPalette(page);
  await palette
    .getByTestId('workbench-visibility-command-hide-frisket-media-view-image-gallery')
    .click();
  await page.getByLabel('Close command palette').click();

  await expect(page.getByTestId('plugin-image-gallery')).toHaveCount(0);
  await expect(page.getByTestId(IMAGE_GALLERY_TEST_ID)).toHaveCount(0);
  await expect(page.getByTestId('workbench-mainView-split')).toHaveCount(0);
  const hiddenLayoutItem = page.getByTestId(
    'workbench-resolved-layout-item-frisket-media-view-image-gallery',
  );
  await expect(hiddenLayoutItem).toHaveAttribute('data-status', 'hidden');
  await expect(hiddenLayoutItem).toHaveAttribute('data-reason', 'hidden_by_profile');

  const stored = await page.evaluate((projectId) => {
    const raw = localStorage.getItem(`frisket:contribution-visibility:${projectId}`);
    if (!raw) throw new Error('contribution visibility storage missing');
    return JSON.parse(raw) as { hiddenContributionIds: string[] };
  }, pid);
  expect(stored.hiddenContributionIds).toContain(IMAGE_GALLERY_ID);

  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('plugin-image-gallery')).toHaveCount(0);
  await expect(page.getByTestId(IMAGE_GALLERY_TEST_ID)).toHaveCount(0);
  await expect(hiddenLayoutItem).toHaveAttribute('data-status', 'hidden');
  await expect(hiddenLayoutItem).toHaveAttribute('data-reason', 'hidden_by_profile');

  await page.getByTestId(`workbench-mainView-tab-${textSheetId}`).click();
  await expect(page.getByTestId('plugin-image-gallery')).toHaveCount(0);
  await expect(page.getByTestId(IMAGE_GALLERY_TEST_ID)).toHaveCount(0);
  await page.getByTestId(`workbench-mainView-tab-${imageSheetId}`).click();
  await expect(page.getByTestId('plugin-image-gallery')).toHaveCount(0);
  await expect(page.getByTestId(IMAGE_GALLERY_TEST_ID)).toHaveCount(0);
  await expect(hiddenLayoutItem).toHaveAttribute('data-status', 'hidden');

  palette = await openCommandPalette(page);
  await palette
    .getByTestId('workbench-visibility-command-reveal-frisket-media-view-image-gallery')
    .click();
  await page.getByLabel('Close command palette').click();

  await expect(page.getByTestId(IMAGE_GALLERY_TEST_ID)).toBeVisible();
  await expect(page.getByTestId('plugin-image-gallery')).toBeVisible();

  await page.getByTestId(`workbench-mainView-tab-${textSheetId}`).click();
  await expect(page.getByTestId('plugin-image-gallery')).toHaveCount(0);
  await expect(page.getByTestId(IMAGE_GALLERY_TEST_ID)).toHaveCount(0);
  const disabledLayoutItem = page.getByTestId(
    'workbench-resolved-layout-item-frisket-media-view-image-gallery',
  );
  await expect(disabledLayoutItem).toHaveAttribute('data-status', 'disabled');
  await expect(disabledLayoutItem).toHaveAttribute(
    'data-reason',
    'data_requirement_unmet:sheetHasColumnType:image',
  );
});

test('pending hidden runtime mainView ids are preserved until reveal', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-workbench-visibility-runtime'));
  const sheetId = await importCsv(page.request, pid, 'runtime.csv', 'title\nRuntime row\n');
  await setHiddenContributions(page, pid, [RUNTIME_VIEW_ID]);
  const releaseRuntimeIndex = await routeRuntimeMainViewPlugin(page, pid, {
    initiallyPending: true,
  });

  await openProject(page, pid, sheetId);

  await expect(page.getByTestId(RUNTIME_VIEW_TEST_ID)).toHaveCount(0);
  await expect(
    page.getByTestId('workbench-resolved-layout-item-demo-visibility-view-runtime-hidden'),
  ).toHaveCount(0);
  const pendingStored = await page.evaluate((projectId) => {
    const raw = localStorage.getItem(`frisket:contribution-visibility:${projectId}`);
    if (!raw) throw new Error('contribution visibility storage missing');
    return JSON.parse(raw) as { hiddenContributionIds: string[] };
  }, pid);
  expect(pendingStored.hiddenContributionIds).toContain(RUNTIME_VIEW_ID);

  releaseRuntimeIndex();

  const hiddenLayoutItem = page.getByTestId(
    'workbench-resolved-layout-item-demo-visibility-view-runtime-hidden',
  );
  await expect(hiddenLayoutItem).toHaveAttribute('data-status', 'hidden');
  await expect(hiddenLayoutItem).toHaveAttribute('data-reason', 'hidden_by_profile');
  await expect(hiddenLayoutItem).toHaveAttribute('data-runtime-source', 'runtimeIndex');

  const stored = await page.evaluate((projectId) => {
    const raw = localStorage.getItem(`frisket:contribution-visibility:${projectId}`);
    if (!raw) throw new Error('contribution visibility storage missing');
    return JSON.parse(raw) as { hiddenContributionIds: string[] };
  }, pid);
  expect(stored.hiddenContributionIds).toContain(RUNTIME_VIEW_ID);

  const palette = await openCommandPalette(page);
  const reveal = palette.getByTestId(
    'workbench-visibility-command-reveal-demo-visibility-view-runtime-hidden',
  );
  await expect(reveal).toHaveAttribute('data-target-contribution-id', RUNTIME_VIEW_ID);
  await expect(reveal).toHaveAttribute('data-availability-status', 'hidden');
  await reveal.click();
  await page.getByLabel('Close command palette').click();

  await expect(page.getByTestId(RUNTIME_VIEW_TEST_ID)).toBeVisible();
  await expect(page.getByTestId('runtime-hidden-view-inner')).toContainText('Runtime hidden view');
  await expect(hiddenLayoutItem).toHaveAttribute('data-status', 'enabled');
});

// The graph mainView pane is now data-keyed to materialized edge/join sheets
// (graph-view-availability-signal-v1), so this reveal round-trip must seed an
// edge sheet (a derive.join output carries two-sided membership) for the graph
// contribution to actually mount.
async function seedJoinEdgeSheet(
  request: APIRequestContext,
  pid: string,
): Promise<number> {
  const leftId = await importCsv(
    request,
    pid,
    'states.csv',
    'code,name\nNY,New York\nCA,California\n',
  );
  const rightId = await importCsv(
    request,
    pid,
    'population.csv',
    'code,pop\nNY,100\nCA,200\n',
  );
  const spec = {
    schema_version: 'frisket.action.v2',
    kind: 'derive.join',
    capabilities: ['project:write'],
    params: {
      left_sheet_id: leftId,
      right_sheet_id: rightId,
      join_keys: [{ left_column: 'code', right_column: 'code' }],
      how: 'inner',
      target_sheet_name: 'States x Population',
    },
    idempotency_key: `e2e-derive.join@sha256:${Math.random().toString(16).slice(2)}`,
  };
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: spec });
  const body = (await res.json()) as Record<string, unknown>;
  expect(res.ok(), JSON.stringify(body)).toBeTruthy();
  const output = (body.outputs as Array<Record<string, unknown>>).find(
    (o) => o.kind === 'sheet',
  );
  return Number(output!.sheet_id);
}

test('first-party route-backed mainView panes honor hidden profile state', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-workbench-visibility-routes'));
  const sheetId = await seedJoinEdgeSheet(page.request, pid);

  await openProject(page, pid, sheetId);

  let palette = await openCommandPalette(page);
  await expect(
    palette.getByTestId('workbench-visibility-command-hide-frisket-core-view-review-queue'),
  ).toHaveCount(0);
  const hideGraph = palette.getByTestId(
    'workbench-visibility-command-hide-frisket-investigative-view-graph-neighborhood',
  );
  await expect(hideGraph).toHaveAttribute('data-target-contribution-id', GRAPH_ID);
  await hideGraph.click();
  await page.getByLabel('Close command palette').click();

  // The graph descriptor also declares an activityRail launcher placement
  // (plugin-activityrail-launcher-parity-v1), so scope to the mainView item.
  const hiddenGraphLayoutItem = page
    .getByTestId('workbench-resolved-layout-region-mainView')
    .getByTestId('workbench-resolved-layout-item-frisket-investigative-view-graph-neighborhood');
  await expect(hiddenGraphLayoutItem).toHaveAttribute('data-status', 'hidden');
  await expect(hiddenGraphLayoutItem).toHaveAttribute('data-reason', 'hidden_by_profile');
  // The sheet IS edge-shaped (a derive.join output), so hiding the graph
  // contribution leaves the segment VISIBLE but DISABLED naming the reason —
  // the honest hidden-but-data-satisfied pattern (mirrors Map). The PANE
  // (GRAPH_TEST_ID) is what must be absent while hidden.
  const graphSegment = page.getByTestId('view-switch-graph');
  await expect(graphSegment).toBeVisible();
  await expect(graphSegment).toBeDisabled();
  await expect(page.getByTestId(GRAPH_TEST_ID)).toHaveCount(0);

  await page.goto(`/p/${pid}/s/${sheetId}/graph`);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('workbench-region-mainView')).toHaveAttribute(
    'data-active-contribution-id',
    'frisket.core.view.grid',
  );
  await expect(page.getByTestId(GRAPH_TEST_ID)).toHaveCount(0);
  await expect(hiddenGraphLayoutItem).toHaveAttribute('data-status', 'hidden');

  palette = await openCommandPalette(page);
  const revealGraph = palette.getByTestId(
    'workbench-visibility-command-reveal-frisket-investigative-view-graph-neighborhood',
  );
  await expect(revealGraph).toHaveAttribute('data-target-contribution-id', GRAPH_ID);
  await revealGraph.click();
  await page.getByLabel('Close command palette').click();

  await expect(page.getByTestId(GRAPH_TEST_ID)).toBeVisible();
  await expect(page.getByTestId('workbench-region-mainView')).toHaveAttribute(
    'data-active-contribution-id',
    GRAPH_ID,
  );
});

test('runtime id collisions do not turn unsupported first-party panes into visibility targets', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-workbench-visibility-collision'));
  const sheetId = await importCsv(page.request, pid, 'collision.csv', 'title\nCollision row\n');
  await routeRuntimeMainViewPlugin(page, pid, {
    contributionId: REVIEW_QUEUE_ID,
    title: 'Runtime review queue collision',
  });

  await openProject(page, pid, sheetId);

  const reviewQueueLayoutItem = page
    .getByTestId('workbench-resolved-layout-region-mainView')
    .getByTestId('workbench-resolved-layout-item-frisket-core-view-review-queue');
  await expect(reviewQueueLayoutItem).toHaveAttribute('data-runtime-source', 'firstParty');

  const palette = await openCommandPalette(page);
  await expect(
    palette.getByTestId('workbench-visibility-command-hide-frisket-core-view-review-queue'),
  ).toHaveCount(0);
});
