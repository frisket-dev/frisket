import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  openProject,
  sheetData,
  uniqueName,
} from './helpers';

const PLUGIN_ID = 'demo.receipt_stamp';
const ACTION_KIND = 'demo.receipt_stamp.stamp';
const TRUSTED_LOCAL_CAPABILITY = 'plugin:trusted_local_backend';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const PLUGIN_ROOT = resolve(ROOT, 'tests/fixtures/local_plugins/demo_receipt_stamp');

type CatalogEntry = {
  kind: string;
  title: string;
  authoring_contract_version: number;
  required_capabilities: string[];
  input_schema: {
    properties?: Record<string, unknown>;
  };
  row_scope_policy?: {
    kind?: string;
    selectors?: string[];
  };
  ui_hints?: {
    form?: string;
    semantic_controls?: Record<string, string>;
    logical_outputs?: Array<{ key?: string; column_type?: string }>;
  };
};

type ActionCatalog = {
  actions: CatalogEntry[];
};

type ActionResult = {
  schema_version: string;
  status: string;
  run_id?: number | null;
  receipt_id?: string | null;
};

async function installAndActivateReceiptStampPlugin(
  request: APIRequestContext,
  projectId: string,
): Promise<void> {
  const installed = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${PLUGIN_ID}/install-local`,
    {
      data: {
        source: {
          kind: 'localPath',
          value: PLUGIN_ROOT,
        },
        arbitraryPackageLoadAllowed: false,
      },
    },
  );
  expect(installed.ok(), await installed.text()).toBeTruthy();
  const installBody = await installed.json() as {
    installState: string;
    receiptId?: string | null;
  };
  expect(installBody.installState).toBe('installed');
  expect(installBody.receiptId).toBeTruthy();

  const activated = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${PLUGIN_ID}/activate`,
    {
      data: {
        receiptId: installBody.receiptId,
        trustAcknowledged: true,
        permissionsAccepted: [TRUSTED_LOCAL_CAPABILITY],
        arbitraryPackageLoadAllowed: false,
      },
    },
  );
  expect(activated.ok(), await activated.text()).toBeTruthy();

  const backend = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${PLUGIN_ID}/backend/activate`,
    {
      data: {
        trustAcknowledged: true,
        arbitraryPackageLoadAllowed: false,
        executableHandlersAllowed: true,
      },
    },
  );
  expect(backend.ok(), await backend.text()).toBeTruthy();
}

async function waitForRunToFinish(
  page: Page,
  projectId: string,
  runId: number,
): Promise<void> {
  await expect.poll(async () => {
    const response = await page.request.get(
      `/api/projects/${projectId}/actions/runs/${runId}/status`,
    );
    if (!response.ok()) return 'http-error';
    const body = await response.json() as {
      run?: { status?: string; public_status?: { status?: string; live?: boolean } };
      status?: string;
      live?: boolean;
    };
    const run = body.run ?? {};
    const status = run.status ?? run.public_status?.status ?? body.status ?? null;
    const live = run.public_status?.live ?? body.live ?? false;
    if (status === 'failed' || status === 'cancelled' || status === 'stalled') return status;
    return live || status === 'queued' || status === 'running' ? 'running' : 'finished';
  }, { timeout: 30_000 }).toBe('finished');
}

test('trusted-local receipt stamp plugin action launches from ActionPanel catalog', async ({
  page,
}) => {
  const projectId = await createProject(page.request, uniqueName('plugin-subprocess-op'));
  const sheetId = await importCsv(
    page.request,
    projectId,
    'cases.csv',
    'note\n"first demo row"\n"second demo row"\n',
  );
  await installAndActivateReceiptStampPlugin(page.request, projectId);

  const catalogResponse = await page.request.get(
    `/api/projects/${projectId}/actions/v1/catalog`,
  );
  expect(catalogResponse.ok()).toBeTruthy();
  const catalog = await catalogResponse.json() as ActionCatalog;
  const actionEntry = catalog.actions.find((entry) => entry.kind === ACTION_KIND);
  expect(actionEntry).toBeTruthy();
  expect(actionEntry).toMatchObject({
    authoring_contract_version: 1,
    required_capabilities: ['project:write'],
    row_scope_policy: {
      kind: 'sheet_rows',
      selectors: expect.arrayContaining(['all_rows', 'exact_membership']),
    },
    ui_hints: {
      form: 'generated',
      semantic_controls: { name: 'column' },
      logical_outputs: [{ key: 'receipt_stamp', column_type: 'text' }],
    },
  });
  expect(Object.keys(actionEntry?.input_schema.properties ?? {})).toEqual(['name']);

  await openProject(page, projectId, sheetId);
  await openAction(page, ACTION_KIND);
  await expect(page.getByTestId('action-form-title')).toContainText(actionEntry!.title);

  await page.getByTestId('field-name').selectOption('note');

  const runResponsePromise = page.waitForResponse((response) => (
    response.url().includes(`/api/projects/${projectId}/actions/v1/run`) &&
    response.request().method() === 'POST'
  ));
  await clickRunButton(page, { requireCostConfirmation: false });
  const runResponse = await runResponsePromise;
  expect(runResponse.ok()).toBeTruthy();

  const posted = runResponse.request().postDataJSON() as {
    action_id: string;
    scope: Record<string, unknown>;
    params: Record<string, unknown>;
    output_names: Record<string, string>;
    idempotency_key: string;
  };
  expect(posted).toEqual({
    action_id: ACTION_KIND,
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { name: 'note' },
    output_names: { receipt_stamp: 'receipt_stamp' },
    idempotency_key: expect.any(String),
  });

  const runResult = await runResponse.json() as ActionResult;
  expect(runResult.schema_version).toBe('frisket.action_result.v1');
  expect(runResult.receipt_id).toBeTruthy();
  if (typeof runResult.run_id === 'number') {
    await waitForRunToFinish(page, projectId, runResult.run_id);
  }

  await expect.poll(async () => {
    const data = await sheetData(page.request, projectId, sheetId, 0, 5);
    const column = data.columns.find((candidate) => candidate.name === 'receipt_stamp');
    if (!column) return [];
    return data.rows.map((row) => row.cells[String(column.id)]);
  }, { timeout: 30_000 }).toEqual([
    'first demo row|demo.receipt_stamp|demo.receipt_stamp:stamp',
    'second demo row|demo.receipt_stamp|demo.receipt_stamp:stamp',
  ]);
});
