import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  clickRunButton,
  createProject,
  importCsv,
  listSheets,
  openProject,
  selectRow,
  sheetData,
  uniqueName,
  type WireData,
  type WireSheet,
  openAction,
} from './helpers';

const PLUGIN_ID = 'demo.star_summary';
const ACTION_KIND = 'demo.star_summary.summarize_stars';
const TRUSTED_LOCAL_CAPABILITY = 'plugin:trusted_local_backend';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const PLUGIN_ROOT = resolve(ROOT, 'tests/fixtures/local_plugins/demo_star_summary');
const SUMMARY_COLUMNS = [
  'constellation',
  'star_count',
  'average_rating',
  'top_star',
  'source_row_count',
] as const;
const SELECTED_CHILD_SHEET = 'Selected star summary';
const ALL_VISIBLE_CHILD_SHEET = 'All visible star summary';

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
    source_requirements?: Array<{
      param?: string;
      accepted_column_types?: string[];
    }>;
    logical_outputs?: Array<{ key?: string; column_type?: string }>;
    typed_action?: { creates_sheet?: boolean };
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
  outputs?: Array<{ kind?: string; name?: string | null; ref?: Record<string, unknown> }>;
  errors?: Array<{ code?: string; message?: string }>;
};

type ReceiptLookup = {
  action_kind?: string;
  status?: string;
  inputs?: Array<{ ref?: Record<string, unknown> }>;
  outputs?: Array<{ kind?: string; name?: string | null; ref?: Record<string, unknown> }>;
  evidence?: Array<{ ref?: Record<string, unknown> }>;
};

type StarRow = {
  name: string;
  constellation: string;
  rating: number;
};

type SummaryExpectation = {
  constellation: string;
  star_count: number;
  average_rating: number;
  top_star: string;
  source_row_count: number;
};

function parseJsonResponse<T>(text: string): T {
  return JSON.parse(text) as T;
}

function csvEscape(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

function starRows(count: number): StarRow[] {
  return Array.from({ length: count }, (_, index) => {
    const group = index % 4;
    return {
      name: `Star ${String(index + 1).padStart(3, '0')}`,
      constellation: ['Lyra', 'Orion', 'Cygnus', 'Draco'][group],
      rating: [4.9, 3.4, 4.2, 2.6][group] + (index % 3) * 0.1,
    };
  });
}

function starCsv(rows: StarRow[]): string {
  return [
    'star,constellation,rating',
    ...rows.map((row) => [
      csvEscape(row.name),
      csvEscape(row.constellation),
      String(row.rating),
    ].join(',')),
  ].join('\n');
}

function summarizeRows(rows: StarRow[]): SummaryExpectation[] {
  const byConstellation = new Map<string, StarRow[]>();
  for (const row of rows) {
    const existing = byConstellation.get(row.constellation) ?? [];
    existing.push(row);
    byConstellation.set(row.constellation, existing);
  }
  return [...byConstellation.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([constellation, group]) => {
      const top = [...group].sort((left, right) => (
        right.rating - left.rating || left.name.localeCompare(right.name)
      ))[0];
      expect(top).toBeTruthy();
      const average = group.reduce((total, row) => total + row.rating, 0) / group.length;
      return {
        constellation,
        star_count: group.length,
        average_rating: Number(average.toFixed(2)),
        top_star: top.name,
        source_row_count: group.length,
      };
    });
}

function valueByColumn(data: WireData, rowIndex: number, columnName: string): unknown {
  const column = data.columns.find((candidate) => candidate.name === columnName);
  expect(column, `missing column ${columnName}`).toBeTruthy();
  return data.rows[rowIndex].cells[String(column!.id)];
}

function expectSummarySheet(data: WireData, expected: SummaryExpectation[]): void {
  expect(data.total).toBe(expected.length);
  for (const name of SUMMARY_COLUMNS) {
    expect(data.columns.some((column) => column.name === name)).toBe(true);
  }
  const actualByConstellation = new Map<string, Record<string, unknown>>();
  for (let index = 0; index < data.rows.length; index += 1) {
    actualByConstellation.set(String(valueByColumn(data, index, 'constellation')), {
      star_count: Number(valueByColumn(data, index, 'star_count')),
      average_rating: Number(valueByColumn(data, index, 'average_rating')),
      top_star: String(valueByColumn(data, index, 'top_star')),
      source_row_count: Number(valueByColumn(data, index, 'source_row_count')),
    });
  }
  for (const row of expected) {
    const actual = actualByConstellation.get(row.constellation);
    expect(actual, `missing summary for ${row.constellation}`).toBeTruthy();
    expect(actual).toMatchObject({
      star_count: row.star_count,
      top_star: row.top_star,
      source_row_count: row.source_row_count,
    });
    expect(Math.abs(Number(actual!.average_rating) - row.average_rating)).toBeLessThanOrEqual(0.01);
  }
}

function receiptContainsSourceMembership(
  receipt: ReceiptLookup,
  sourceSheetId: number,
  sourceRowIds: number[],
  childSheetId: number,
  childRowIds: number[],
): void {
  expect(receipt.action_kind).toBe(ACTION_KIND);
  expect(receipt.status).toBe('completed');
  const materializedSheet = receipt.outputs?.find((output) => (
    output.ref?.kind === 'materialized_sheet' && output.ref.sheet_id === childSheetId
  ))?.ref;
  expect(materializedSheet).toMatchObject({
    kind: 'materialized_sheet',
    sheet_id: childSheetId,
    parent_sheet_id: sourceSheetId,
    row_count: childRowIds.length,
  });

  const columnRefs = (receipt.outputs ?? []).filter((output) => (
    output.ref?.kind === 'materialized_column'
  )).map((output) => output.ref!);
  expect(columnRefs.map((ref) => ref.name).sort()).toEqual([...SUMMARY_COLUMNS].sort());
  expect(columnRefs.every((ref) => (
    ref.sheet_id === childSheetId && typeof ref.column_id === 'number'
  ))).toBe(true);
  const rowsRef = receipt.outputs?.find((output) => (
    output.ref?.kind === 'materialized_rows'
  ))?.ref;
  expect(rowsRef).toMatchObject({
    kind: 'materialized_rows',
    sheet_id: childSheetId,
    row_ids: expect.arrayContaining(childRowIds),
  });

  const sourceRead = receipt.inputs?.find((input) => (
    input.ref?.kind === 'sheet_rows_read'
  ))?.ref;
  expect(sourceRead).toMatchObject({
    kind: 'sheet_rows_read',
    sheet_id: sourceSheetId,
    row_ids: sourceRowIds,
    columns: expect.arrayContaining([
      expect.objectContaining({ name: 'constellation', type: 'text' }),
      expect.objectContaining({ name: 'rating', type: 'number' }),
      expect.objectContaining({ name: 'star', type: 'text' }),
    ]),
  });
  expect(sourceRead?.source_values_hash).toMatch(/^sha256:[0-9a-f]{64}$/);
  expect(sourceRead).not.toHaveProperty('rows');

  const membership = receipt.evidence?.find((item) => (
    item.ref?.kind === 'materialized_row_sources'
  ))?.ref;
  const membershipRows = membership?.rows as Array<Record<string, unknown>> | undefined;
  expect(membership).toMatchObject({
    kind: 'materialized_row_sources',
    row_count: sourceRowIds.length,
  });
  expect(membershipRows).toHaveLength(sourceRowIds.length);
  expect(membershipRows?.every((row) => (
    row.role === 'aggregate_source' && row.source_sheet_id === sourceSheetId
  ))).toBe(true);
  expect((membershipRows ?? []).map((row) => row.source_row_id).sort((left, right) => (
    Number(left) - Number(right)
  ))).toEqual([...sourceRowIds].sort((left, right) => left - right));
  const sourcesBySummaryRow = new Map<number, Set<number>>();
  for (const row of membershipRows ?? []) {
    const materializedRowId = Number(row.materialized_row_id);
    const sources = sourcesBySummaryRow.get(materializedRowId) ?? new Set<number>();
    sources.add(Number(row.source_row_id));
    sourcesBySummaryRow.set(materializedRowId, sources);
  }
  expect([...sourcesBySummaryRow.keys()].sort((left, right) => left - right)).toEqual(
    [...childRowIds].sort((left, right) => left - right),
  );
  expect(new Set(
    [...sourcesBySummaryRow.values()].map((sources) => [...sources].sort().join(',')),
  ).size).toBe(childRowIds.length);
}

async function installAndActivateStarSummaryPlugin(
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
  }, { timeout: 60_000 }).toBe('finished');
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
  expect(runResponse.ok(), responseText).toBeTruthy();
  return {
    result: parseJsonResponse<ActionResult>(responseText),
    posted: runResponse.request().postDataJSON() as Record<string, unknown>,
  };
}

async function loadReceipt(
  request: APIRequestContext,
  projectId: string,
  receiptId: string,
): Promise<ReceiptLookup> {
  const response = await request.get(`/api/projects/${projectId}/actions/v1/receipts/${receiptId}`);
  const text = await response.text();
  expect(response.ok(), text).toBeTruthy();
  return parseJsonResponse<ReceiptLookup>(text);
}

async function waitForChildSheet(
  request: APIRequestContext,
  projectId: string,
  parentSheetId: number,
  name: string,
): Promise<WireSheet> {
  await expect.poll(async () => {
    const sheets = await listSheets(request, projectId);
    return sheets.some((sheet) => sheet.parent_sheet_id === parentSheetId && sheet.name === name);
  }, { timeout: 30_000 }).toBe(true);
  const sheet = (await listSheets(request, projectId)).find((candidate) => (
    candidate.parent_sheet_id === parentSheetId && candidate.name === name
  ));
  expect(sheet).toBeTruthy();
  return sheet!;
}

function expectStarSummaryPost(
  posted: Record<string, unknown>,
  sheetId: number,
  childSheetName: string,
  rowIds?: number[],
): void {
  expect(posted.action_id).toBe(ACTION_KIND);
  expect(posted.scope).toEqual({
    kind: 'sheet_rows',
    sheet_id: sheetId,
    ...(rowIds ? { row_ids: rowIds } : {}),
  });
  const params = posted.params as Record<string, unknown>;
  expect(params).toEqual({
    emit_invalid_row: false,
    group_by: 'constellation',
    value_column: 'rating',
    label_column: 'star',
  });
  expect(posted.sheet_name).toBe(childSheetName);
  expect(posted.output_names).toEqual({});
  expect(posted.idempotency_key).toEqual(expect.any(String));
  expect(posted).not.toHaveProperty('schema_version');
  expect(posted).not.toHaveProperty('capabilities');
}

async function chooseCatalogActionAndParams(
  page: Page,
  actionEntry: CatalogEntry,
  childSheetName: string,
): Promise<void> {
  await openAction(page, ACTION_KIND);
  await expect(page.getByTestId('action-form-title')).toContainText(actionEntry.title);

  await page.getByTestId('field-group_by').selectOption('constellation');
  await page.getByTestId('field-value_column').selectOption('rating');
  await page.getByTestId('field-label_column').selectOption('star');
  await page.getByTestId('field-sheet_name').fill(childSheetName);
}

async function closeRowDrawerIfOpen(page: Page): Promise<void> {
  if (await page.getByTestId('row-drawer').isVisible()) {
    await page.getByLabel('Close drawer').click();
    await expect(page.getByTestId('row-drawer')).not.toBeVisible();
  }
}

test('rowset plugin action materializes selected and all-visible star summaries', async ({
  page,
}) => {
  if (!existsSync(resolve(PLUGIN_ROOT, 'plugin.json'))) {
    throw new Error('demo_star_summary backend fixture is required for this acceptance spec');
  }
  const rows = starRows(130);
  const projectId = await createProject(page.request, uniqueName('plugin-rowset-table'));
  const sheetId = await importCsv(page.request, projectId, 'stars.csv', starCsv(rows));
  await installAndActivateStarSummaryPlugin(page.request, projectId);

  const catalogResponse = await page.request.get(
    `/api/projects/${projectId}/actions/v1/catalog`,
  );
  expect(catalogResponse.ok(), await catalogResponse.text()).toBeTruthy();
  const catalog = await catalogResponse.json() as ActionCatalog;
  const actionEntry = catalog.actions.find((entry) => entry.kind === ACTION_KIND);
  expect(actionEntry).toBeTruthy();
  expect(actionEntry?.required_capabilities).toEqual(['project:write']);
  expect(actionEntry).toMatchObject({
    authoring_contract_version: 1,
    row_scope_policy: {
      kind: 'sheet_rows',
      selectors: expect.arrayContaining(['all_rows', 'exact_membership']),
    },
    ui_hints: {
      form: 'generated',
      semantic_controls: {
        group_by: 'column',
        value_column: 'column',
        label_column: 'column',
      },
      typed_action: { creates_sheet: true },
    },
  });
  expect(Object.keys(actionEntry?.input_schema.properties ?? {})).toEqual(
    expect.arrayContaining(['group_by', 'value_column', 'label_column']),
  );
  expect(actionEntry?.ui_hints?.source_requirements).toEqual([
    { id: 'group_by', param: 'group_by', label: 'Group by', mode: 'column', min: 1,
      accepted_column_types: ['text'] },
    { id: 'value_column', param: 'value_column', label: 'Value column', mode: 'column', min: 1,
      accepted_column_types: ['number'] },
    { id: 'label_column', param: 'label_column', label: 'Label column', mode: 'column', min: 1,
      accepted_column_types: ['text'] },
  ]);
  expect(actionEntry?.ui_hints?.logical_outputs).toEqual([
    { key: 'constellation', column_type: 'text' },
    { key: 'star_count', column_type: 'integer' },
    { key: 'average_rating', column_type: 'number' },
    { key: 'top_star', column_type: 'text' },
    { key: 'source_row_count', column_type: 'integer' },
  ]);

  const sourceData = await sheetData(page.request, projectId, sheetId, 0, rows.length);
  const selectedIndexes = [0, 1, 4, 5, 8, 9];
  const selectedRowIds = selectedIndexes.map((index) => sourceData.rows[index].id);
  const selectedRows = selectedIndexes.map((index) => rows[index]);

  await openProject(page, projectId, sheetId);
  for (const index of selectedIndexes) {
    await selectRow(page, index);
  }
  await closeRowDrawerIfOpen(page);
  await chooseCatalogActionAndParams(page, actionEntry!, SELECTED_CHILD_SHEET);
  await expect(page.locator('.run-scope-summary')).toContainText('6 selected rows');
  await page.getByTestId('generated-action-run-scope-menu-button').click();
  await expect(page.getByTestId('generated-action-row-scope-selected')).toContainText(
    'Run on selected',
  );
  await expect(page.getByTestId('generated-action-row-scope-selected')).toContainText('6 selected');
  await page.getByTestId('generated-action-run-scope-menu-button').click();
  const selectedRun = await runCurrentActionFromPanel(page, projectId);
  expectStarSummaryPost(selectedRun.posted, sheetId, SELECTED_CHILD_SHEET, selectedRowIds);
  expect(selectedRun.result.schema_version).toBe('frisket.action_result.v1');
  expect(selectedRun.result.receipt_id).toBeTruthy();
  if (typeof selectedRun.result.run_id === 'number') {
    await waitForRunToFinish(page, projectId, selectedRun.result.run_id);
  }

  const selectedChild = await waitForChildSheet(
    page.request,
    projectId,
    sheetId,
    SELECTED_CHILD_SHEET,
  );
  const selectedSummaryData = await sheetData(page.request, projectId, selectedChild.id, 0, 20);
  expectSummarySheet(selectedSummaryData, summarizeRows(selectedRows));
  receiptContainsSourceMembership(
    await loadReceipt(page.request, projectId, selectedRun.result.receipt_id!),
    sheetId,
    selectedRowIds,
    selectedChild.id,
    selectedSummaryData.rows.map((row) => row.id),
  );

  await openProject(page, projectId, sheetId);
  await chooseCatalogActionAndParams(page, actionEntry!, ALL_VISIBLE_CHILD_SHEET);
  await expect(page.locator('.run-scope-summary')).toContainText(
    `All ${rows.length} rows will run`,
  );
  const allVisibleRun = await runCurrentActionFromPanel(page, projectId);
  expectStarSummaryPost(allVisibleRun.posted, sheetId, ALL_VISIBLE_CHILD_SHEET);
  expect(allVisibleRun.result.schema_version).toBe('frisket.action_result.v1');
  expect(allVisibleRun.result.receipt_id).toBeTruthy();
  if (typeof allVisibleRun.result.run_id === 'number') {
    await waitForRunToFinish(page, projectId, allVisibleRun.result.run_id);
  }

  const allVisibleChild = await waitForChildSheet(
    page.request,
    projectId,
    sheetId,
    ALL_VISIBLE_CHILD_SHEET,
  );
  const allVisibleSummaryData = await sheetData(page.request, projectId, allVisibleChild.id, 0, 20);
  expectSummarySheet(allVisibleSummaryData, summarizeRows(rows));
  const allVisibleReceipt = await loadReceipt(
    page.request,
    projectId,
    allVisibleRun.result.receipt_id!,
  );
  receiptContainsSourceMembership(
    allVisibleReceipt,
    sheetId,
    sourceData.rows.map((row) => row.id),
    allVisibleChild.id,
    allVisibleSummaryData.rows.map((row) => row.id),
  );
});
