import { expect, test, type APIRequestContext } from '@playwright/test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  clickCell,
  createProject,
  importCsv,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

const PLUGIN_ID = 'demo.case_files';
const PLUGIN_CAPABILITY = 'plugin:trusted_local_backend';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const PLUGIN_MANIFEST = resolve(ROOT, 'tests/fixtures/local_plugins/demo_case_files/plugin.json');

async function activateCaseFilesPlugin(request: APIRequestContext, pid: string): Promise<void> {
  const load = await request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'plugin.load',
      scope: { kind: 'project' },
      params: {
        manifest: {
          kind: 'local_file',
          path: PLUGIN_MANIFEST,
        },
      },
      idempotency_key: `plugin-case-files-e2e@sha256:${Date.now()}`,
    },
  });
  expect(load.ok()).toBeTruthy();
  const loadResult = await load.json() as { status: string; receipt_id?: string };
  expect(loadResult.status).toBe('completed');
  expect(loadResult.receipt_id).toBeTruthy();

  const enabled = await request.post(`/api/projects/${pid}/workbench/plugins/${PLUGIN_ID}/activate`, {
    data: {
      receiptId: loadResult.receipt_id,
      trustAcknowledged: true,
      permissionsAccepted: [PLUGIN_CAPABILITY],
      arbitraryPackageLoadAllowed: false,
    },
  });
  expect(enabled.ok()).toBeTruthy();

  const backend = await request.post(
    `/api/projects/${pid}/workbench/plugins/${PLUGIN_ID}/backend/activate`,
    { data: { trustAcknowledged: true, arbitraryPackageLoadAllowed: false } },
  );
  expect(backend.ok()).toBeTruthy();
  const body = await backend.json() as {
    registeredBackendContributions: { columnTypes: string[] };
  };
  expect(body.registeredBackendContributions.columnTypes).toEqual(['case_id']);
}

test('demo_case_files registers case_id, displays fallback text, and canonicalizes edits', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('plugin-case-files'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'cases.csv',
    'title,case_id\nAlpha,CASE-0001\nBeta,CASE-0002\n',
  );

  await activateCaseFilesPlugin(page.request, pid);

  const registry = await page.request.get(`/api/projects/${pid}/column-types`);
  expect(registry.ok()).toBeTruthy();
  const types = await registry.json() as Array<{
    name: string;
    core: boolean;
    plugin: string;
    has_validator: boolean;
    has_parser: boolean;
    presentation?: Record<string, unknown>;
  }>;
  const caseType = types.find((type) => type.name === 'case_id');
  expect(caseType).toMatchObject({
    core: false,
    plugin: PLUGIN_ID,
    has_validator: true,
    has_parser: true,
    presentation: {
      base: 'text',
      owner: 'plugin',
      plugin: PLUGIN_ID,
    },
  });

  let columns = await sheetColumns(page.request, pid, sheetId);
  const caseIdColumn = columns.find((column) => column.name === 'case_id');
  expect(caseIdColumn).toBeTruthy();
  await setColumnType(page.request, pid, caseIdColumn!.id, 'case_id');

  await openProject(page, pid, sheetId);
  columns = await sheetColumns(page.request, pid, sheetId);
  await clickCell(page, columns, 'case_id', 0);
  await page.keyboard.press('Enter');

  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  const caseField = drawer.getByTestId('row-field-case_id');
  await expect(caseField.locator('.row-field-value')).toContainText('CASE-0001');

  await page.getByTestId('row-field-case_id').hover();
  await caseField.getByTestId('cell-edit-case_id').click();
  await caseField.getByTestId('cell-editor-case_id').fill('case 42');
  await caseField.getByTestId('cell-save-case_id').click();

  const loadFirstRow = async (): Promise<{ id: number; cells: Record<string, unknown> }> => {
    const data = await page.request.get(
      `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`,
    );
    expect(data.ok()).toBeTruthy();
    return (await data.json()).rows[0] as { id: number; cells: Record<string, unknown> };
  };
  await expect.poll(async () => {
    const firstRow = await loadFirstRow();
    return firstRow.cells[String(caseIdColumn!.id)];
  }).toBe('CASE-0042');
  const firstRow = await loadFirstRow();

  const invalid = await page.request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'cell.edit',
      scope: { kind: 'project' },
      params: {
        edits: [
          {
            row_id: firstRow.id,
            column_id: caseIdColumn!.id,
            value: 'not a case',
          },
        ],
      },
      output_names: {},
      idempotency_key: `plugin-case-files-invalid-e2e@sha256:${Date.now()}`,
    },
  });
  expect(invalid.status()).toBe(400);
  const invalidBody = await invalid.json() as { errors: Array<{ code: string }> };
  expect(invalidBody.errors[0].code).toBe('column_value_validation_failed');
});
