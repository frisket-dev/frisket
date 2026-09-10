import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  clickRunButton,
  createProject,
  importCsv,
  openProject,
  sheetData,
  uniqueName,
  openAction,
} from './helpers';

const PLUGIN_ID = 'demo.scalar_params';
const ACTION_KIND = 'demo.scalar_params.format_text';
const TRUSTED_LOCAL_CAPABILITY = 'plugin:trusted_local_backend';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const PLUGIN_ROOT = resolve(ROOT, 'tests/fixtures/local_plugins/demo_scalar_params');

test.use({ screenshot: 'off', trace: 'off' });

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
  receipt_id?: string | null;
};

async function installAndActivateScalarPlugin(
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
  const backendBody = await backend.json() as {
    registeredRuntimeBindings?: { actions?: string[] };
  };
  expect(backendBody.registeredRuntimeBindings?.actions).toContain(ACTION_KIND);
}

async function runCurrentActionFromPanel(
  page: Page,
  projectId: string,
): Promise<{ result: ActionResult; posted: Record<string, unknown> }> {
  const runResponsePromise = page.waitForResponse((response) => (
    response.url().includes(`/api/projects/${projectId}/actions/v1/run`) &&
    response.request().method() === 'POST'
  ));
  await clickRunButton(page, { requireCostConfirmation: false });
  const runResponse = await runResponsePromise;
  const responseText = await runResponse.text();
  return {
    result: JSON.parse(responseText) as ActionResult,
    posted: runResponse.request().postDataJSON() as Record<string, unknown>,
  };
}

test('plugin scalar params stay separate from source-column inputs in the UI action spec', async ({
  page,
}) => {
  if (!existsSync(resolve(PLUGIN_ROOT, 'plugin.json'))) {
    throw new Error('demo_scalar_params backend fixture is required for this spec');
  }
  const projectId = await createProject(page.request, uniqueName('plugin-scalar-params'));
  const sheetId = await importCsv(
    page.request,
    projectId,
    'scalar-params.csv',
    'text,extra\nalpha,one\nbeta,two\n',
  );
  await installAndActivateScalarPlugin(page.request, projectId);

  const catalogResponse = await page.request.get(
    `/api/projects/${projectId}/actions/v1/catalog`,
  );
  expect(catalogResponse.ok(), await catalogResponse.text()).toBeTruthy();
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
      semantic_controls: { text: 'column' },
      logical_outputs: [{ key: 'formatted', column_type: 'text' }],
    },
  });
  expect(Object.keys(actionEntry?.input_schema.properties ?? {}).sort()).toEqual([
    'prefix',
    'repeat',
    'text',
    'uppercase',
  ]);

  await openProject(page, projectId, sheetId);
  await openAction(page, ACTION_KIND);
  await expect(page.getByTestId('action-form-title')).toContainText(actionEntry!.title);

  await page.getByTestId('field-text').selectOption('text');
  await page.getByTestId('field-prefix').fill('web');
  // Boolean params render as ToggleRow checkboxes in the drawer form.
  await page.getByTestId('field-uppercase').check();
  await page.getByTestId('field-repeat').fill('2');

  const { result, posted } = await runCurrentActionFromPanel(page, projectId);
  expect(result.schema_version).toBe('frisket.action_result.v1');
  expect(result.status).toBe('completed');
  expect(result.receipt_id).toBeTruthy();

  expect(posted.action_id).toBe(ACTION_KIND);
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: sheetId });
  const params = posted.params as Record<string, unknown>;
  expect(params).toEqual({
    text: 'text',
    prefix: 'web',
    uppercase: true,
    repeat: 2,
  });
  expect(posted.output_names).toEqual({ formatted: 'formatted' });
  expect(posted.idempotency_key).toEqual(expect.any(String));
  expect(posted).not.toHaveProperty('schema_version');
  expect(posted).not.toHaveProperty('capabilities');

  await expect.poll(async () => {
    const data = await sheetData(page.request, projectId, sheetId, 0, 5);
    const formatted = data.columns.find((column) => column.name === 'formatted');
    if (!formatted) return [];
    return data.rows.map((row) => row.cells[String(formatted.id)]);
  }, { timeout: 30_000 }).toEqual([
    'web:ALPHA,ALPHA:control=none',
    'web:BETA,BETA:control=none',
  ]);
});
