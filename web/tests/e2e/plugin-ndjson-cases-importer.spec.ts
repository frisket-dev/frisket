import { expect, test, type APIRequestContext } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createProject, sheetData, uniqueName, type WireData } from './helpers';

const CASE_FILES_PLUGIN_ID = 'demo.case_files';
const NDJSON_PLUGIN_ID = 'demo.ndjson_cases';
const IMPORTER_KIND = 'demo.ndjson_cases.importer.cases';
const TRUSTED_LOCAL_CAPABILITY = 'plugin:trusted_local_backend';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const CASE_FILES_PLUGIN_ROOT = resolve(ROOT, 'tests/fixtures/local_plugins/demo_case_files');
const NDJSON_PLUGIN_ROOT = resolve(ROOT, 'tests/fixtures/local_plugins/demo_ndjson_cases');

type ActionResult = {
  status: string;
  receipt_id?: string | null;
  outputs?: Array<{ name?: string | null; ref?: Record<string, unknown> }>;
  errors?: Array<{ code?: string; details?: Record<string, unknown> }>;
};

type ReceiptLookup = {
  inputs?: Array<{ ref?: Record<string, unknown> }>;
};

async function installActivatePlugin(
  request: APIRequestContext,
  projectId: string,
  pluginId: string,
  pluginRoot: string,
  executableHandlersAllowed = false,
): Promise<void> {
  const installed = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${pluginId}/install-local`,
    {
      data: {
        source: { kind: 'localPath', value: pluginRoot },
        arbitraryPackageLoadAllowed: false,
      },
    },
  );
  expect(installed.ok()).toBeTruthy();
  const { receiptId } = await installed.json() as { receiptId: string };

  const activated = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${pluginId}/activate`,
    {
      data: {
        receiptId,
        trustAcknowledged: true,
        permissionsAccepted: [TRUSTED_LOCAL_CAPABILITY],
        arbitraryPackageLoadAllowed: false,
      },
    },
  );
  expect(activated.ok()).toBeTruthy();

  const backend = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${pluginId}/backend/activate`,
    {
      data: {
        trustAcknowledged: true,
        arbitraryPackageLoadAllowed: false,
        executableHandlersAllowed,
      },
    },
  );
  expect(backend.ok()).toBeTruthy();
}

function valuesByColumn(data: WireData): Record<string, unknown[]> {
  const columns = new Map(data.columns.map((column) => [String(column.id), column.name]));
  const values: Record<string, unknown[]> = Object.fromEntries(
    data.columns.map((column) => [column.name, []]),
  );
  for (const row of data.rows) {
    for (const column of data.columns) {
      values[column.name].push(row.cells[String(column.id)]);
    }
  }
  expect(columns.size).toBeGreaterThan(0);
  return values;
}

test('demo_ndjson_cases imports streamed case rows through import.runtime', async ({
  page,
}) => {
  const projectId = await createProject(page.request, uniqueName('plugin-ndjson-cases'));
  await installActivatePlugin(
    page.request,
    projectId,
    CASE_FILES_PLUGIN_ID,
    CASE_FILES_PLUGIN_ROOT,
  );
  await installActivatePlugin(
    page.request,
    projectId,
    NDJSON_PLUGIN_ID,
    NDJSON_PLUGIN_ROOT,
    true,
  );

  const tempDir = resolve(ROOT, 'web/test-results/plugin-ndjson-cases');
  mkdirSync(tempDir, { recursive: true });
  const sourcePath = resolve(tempDir, `cases-${Date.now()}.ndjson`);
  writeFileSync(
    sourcePath,
    [
      JSON.stringify({ id: 'case 7', title: 'Alpha', summary: 'First', priority: 2 }),
      JSON.stringify({ case_id: 'CASE-0008', title: 'Beta', priority: 3 }),
      '',
      JSON.stringify({ case_id: 'case_9', title: 'Gamma', summary: 'Third', priority: 1 }),
    ].join('\n'),
    'utf8',
  );

  const imported = await page.request.post(`/api/projects/${projectId}/actions/v1/run`, {
    data: {
      action_id: 'import.runtime',
      scope: { kind: 'project' },
      sheet_name: 'E2E Case Import',
      params: {
        importer_kind: IMPORTER_KIND,
        source: {
          kind: 'runtime',
          label: 'cases.ndjson',
          fingerprint: `sha256:e2e-${Date.now()}`,
        },
        handler_params: { path: sourcePath },
      },
      idempotency_key: `plugin-ndjson-cases-e2e@sha256:${Date.now()}`,
    },
  });
  expect(imported.ok()).toBeTruthy();
  const result = await imported.json() as ActionResult;
  expect(result.status).toBe('completed');
  expect(result.receipt_id).toBeTruthy();

  const sheetRef = result.outputs?.find((output) => output.name === 'E2E Case Import')?.ref;
  expect(sheetRef).toBeTruthy();
  const data = await sheetData(
    page.request,
    projectId,
    Number(sheetRef!['sheet_id']),
    0,
    10,
  );
  const values = valuesByColumn(data);
  expect(values['case_id']).toEqual(['CASE-0007', 'CASE-0008', 'CASE-0009']);
  expect(values['title']).toEqual(['Alpha', 'Beta', 'Gamma']);
  expect(values['summary']).toEqual(['First', '', 'Third']);
  expect(values['priority']).toEqual([2, 3, 1]);

  const receiptResponse = await page.request.get(
    `/api/projects/${projectId}/actions/v1/receipts/${result.receipt_id}`,
  );
  expect(receiptResponse.ok()).toBeTruthy();
  const receipt = await receiptResponse.json() as ReceiptLookup;
  const runtimeRef = receipt.inputs
    ?.map((item) => item.ref)
    .find((ref) => ref?.['kind'] === 'workbench_runtime_binding');
  expect(runtimeRef).toMatchObject({
    handler_key: 'demo.ndjson_cases:cases',
    streaming: {
      spooled: true,
      row_count: 3,
    },
  });

  const invalidSourcePath = resolve(tempDir, `bad-cases-${Date.now()}.ndjson`);
  writeFileSync(
    invalidSourcePath,
    [
      JSON.stringify({ case_id: 'case 10', title: 'Good', summary: 'Fine', priority: 1 }),
      JSON.stringify({ case_id: 'bad', title: 'Bad', summary: 'Nope', priority: 2 }),
    ].join('\n'),
    'utf8',
  );
  const failed = await page.request.post(`/api/projects/${projectId}/actions/v1/run`, {
    data: {
      action_id: 'import.runtime',
      scope: { kind: 'project' },
      sheet_name: 'Broken Case Import',
      params: {
        importer_kind: IMPORTER_KIND,
        source: {
          kind: 'runtime',
          label: 'bad-cases.ndjson',
          fingerprint: `sha256:e2e-bad-${Date.now()}`,
        },
        handler_params: { path: invalidSourcePath },
      },
      idempotency_key: `plugin-ndjson-cases-bad-e2e@sha256:${Date.now()}`,
    },
  });
  expect(failed.status()).toBe(400);
  const failure = await failed.json() as ActionResult;
  expect(failure.status).toBe('failed');
  expect(failure.errors?.[0].code).toBe('invalid_case_id');
  expect(JSON.stringify(failure.errors?.[0].details)).toContain('"line":2');
});
