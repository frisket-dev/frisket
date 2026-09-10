import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  createProject,
  importCsv,
  openProject,
  selectRow,
  sheetData,
  uniqueName,
} from './helpers';

type PluginPanelSpyEvent = {
  kind: 'openRow';
  contributionId: string;
  rowId: string;
};

declare global {
  interface Window {
    __FRISKET_PLUGIN_PANEL_TEST_EVENTS__?: PluginPanelSpyEvent[];
    __FRISKET_DISABLED_PLUGIN_PANEL_CAPABILITIES__?: string[];
    __FRISKET_PLUGIN_PANEL_TEST_SPIES__?: {
      openRow?: (args: { contributionId: string; rowId: string }) => void;
    };
  }
}

const PLUGIN_ID = 'demo.selection_summary';
const PANEL_ID = 'demo.selection_summary.panel.selection_summary';
const PANEL_TEST_ID = 'workbench-contribution-demo-selection-summary-panel-selection-summary';
const COMPONENT_KEY = 'trustedLocal.demoSelectionSummary.SelectionSummaryPanel';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const PLUGIN_MANIFEST = resolve(
  ROOT,
  'tests/fixtures/local_plugins/demo_selection_summary/plugin.json',
);

async function activateSelectionSummaryPlugin(
  request: APIRequestContext,
  projectId: string,
): Promise<void> {
  const load = await request.post(`/api/projects/${projectId}/actions/v1/run`, {
    params: {
    },
    data: {
      action_id: 'plugin.load',
      scope: { kind: 'project' },
      params: {
        manifest: {
          kind: 'local_file',
          path: PLUGIN_MANIFEST,
        },
      },
      idempotency_key: `plugin-selection-summary-panel@sha256:${Date.now()}`,
    },
  });
  expect(load.ok()).toBeTruthy();
  const loadResult = await load.json() as { status: string; receipt_id?: string };
  expect(loadResult.status).toBe('completed');
  expect(loadResult.receipt_id).toBeTruthy();

  const activation = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${PLUGIN_ID}/activate`,
    {
      data: {
        receiptId: loadResult.receipt_id,
        trustAcknowledged: true,
        permissionsAccepted: [],
        arbitraryPackageLoadAllowed: false,
      },
    },
  );
  expect(activation.ok()).toBeTruthy();
}

async function installPanelSpies(page: Page) {
  await page.addInitScript(() => {
    window.__FRISKET_PLUGIN_PANEL_TEST_EVENTS__ = [];
    window.__FRISKET_PLUGIN_PANEL_TEST_SPIES__ = {
      openRow: (args) => {
        window.__FRISKET_PLUGIN_PANEL_TEST_EVENTS__?.push({ kind: 'openRow', ...args });
      },
    };
  });
}

async function panelEvents(page: Page): Promise<PluginPanelSpyEvent[]> {
  return page.evaluate(() => window.__FRISKET_PLUGIN_PANEL_TEST_EVENTS__ ?? []);
}

test('selection summary panel mounts through PluginPanelHost and tracks grid selection', async ({
  page,
}) => {
  await installPanelSpies(page);
  const projectId = await createProject(page.request, uniqueName('plugin-selection-summary'));
  const sheetId = await importCsv(
    page.request,
    projectId,
    'cases.csv',
    'name,status\nAlpha,new\nBeta,open\nGamma,closed\nDelta,new\n',
  );
  const data = await sheetData(page.request, projectId, sheetId, 0, 4);
  const firstRowId = String(data.rows[0].id);
  const thirdRowId = String(data.rows[2].id);

  await activateSelectionSummaryPlugin(page.request, projectId);
  await openProject(page, projectId, sheetId);

  const layoutItem = page.getByTestId(
    'workbench-resolved-layout-item-demo-selection-summary-panel-selection-summary',
  );
  await expect(layoutItem).toHaveAttribute('data-status', 'enabled');
  await expect(layoutItem).toHaveAttribute('data-runtime-source', 'runtimeIndex');

  const frame = page.getByTestId(PANEL_TEST_ID);
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute('data-contribution-id', PANEL_ID);
  await expect(frame).toHaveAttribute('data-host', 'rightInspector');
  await expect(frame).toHaveAttribute('data-mode', 'panel');
  await expect(frame).toHaveAttribute('data-runtime-component-key', COMPONENT_KEY);
  await expect(frame).toHaveAttribute(
    'data-required-capabilities',
    'sheet.active selection.rows host.navigation.openRow',
  );
  await expect(frame).toHaveAttribute(
    'data-plugin-panel-context-schema-version',
    'frisket.plugin_panel_context.v1',
  );
  await expect(frame).toHaveAttribute('data-plugin-panel-selected-count', '0');
  await expect(page.getByTestId('selection-summary-selected-count')).toHaveText(
    '0 selected rows',
  );
  await expect(page.getByTestId('selection-summary-open-first-row')).toBeDisabled();

  await selectRow(page, 0);
  await selectRow(page, 2);

  await expect(frame).toHaveAttribute('data-plugin-panel-selected-count', '2');
  await expect(page.getByTestId('plugin-selection-summary-panel')).toHaveAttribute(
    'data-selected-count',
    '2',
  );
  await expect(page.getByTestId('selection-summary-selected-count')).toHaveText(
    '2 selected rows',
  );
  await expect(page.getByTestId('selection-summary-row-ids')).toContainText(firstRowId);
  await expect(page.getByTestId('selection-summary-row-ids')).toContainText(thirdRowId);

  await page.getByTestId('selection-summary-open-first-row').click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  const events = await panelEvents(page);
  expect(events).toContainEqual({
    kind: 'openRow',
    contributionId: PANEL_ID,
    rowId: firstRowId,
  });
});

test('missing required panel host capability prevents selection summary mount', async ({
  page,
}) => {
  await page.addInitScript(() => {
    window.__FRISKET_DISABLED_PLUGIN_PANEL_CAPABILITIES__ = ['selection.rows'];
  });
  const projectId = await createProject(page.request, uniqueName('plugin-selection-summary-cap'));
  const sheetId = await importCsv(page.request, projectId, 'cases.csv', 'name\nAlpha\n');

  await activateSelectionSummaryPlugin(page.request, projectId);
  await openProject(page, projectId, sheetId);

  await expect(page.getByTestId('plugin-selection-summary-panel')).toHaveCount(0);
  const unavailable = page.getByTestId(
    'plugin-panel-unavailable-demo-selection-summary-panel-selection-summary',
  );
  await expect(unavailable).toBeVisible();
  await expect(unavailable).toHaveAttribute('data-reason', 'missing_capability:selection.rows');
  await expect(page.getByTestId(PANEL_TEST_ID)).toHaveAttribute(
    'data-plugin-panel-unavailable-reason',
    'missing_capability:selection.rows',
  );
});
