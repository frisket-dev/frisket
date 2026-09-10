// Derive object-list UI. The form must preserve a nested list-item schema,
// then the real v1 map.extract -> derive.table_from_list composite must
// materialize object items into a typed child sheet.

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  editCells,
  importCsv,
  listSheets,
  openAction,
  sheetData,
  type WireSheet,
  uniqueName,
} from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();
const PYTHON = process.env.FRISKET_E2E_PYTHON ?? path.join(REPO_ROOT, '.venv/bin/python');

const CSV =
  'episode,description\n' +
  '"Fitness episode","The host reads an AG1 ad with code ROGAN and a Cash App spot offering a signup bonus."\n' +
  '"Tools episode","A Squarespace segment offers 10% off, then the host mentions AG1 again with code ROGAN."\n';

const PROMPT =
  'List every sponsor mentioned in this row. For each sponsor, return the company, any coupon code or discount, and the product being advertised. Only use facts present in the row.';
const MODEL = 'gemini/gemini-3.5-flash-lite';

async function exposeReplayModel(page: Page): Promise<void> {
  await page.route('**/api/providers', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schemaVersion: 'frisket.providers.v1',
      tier: 'local',
      providers: [{
        id: 'gemini', label: 'Gemini', kind: 'platform_api', configured: true,
        source: 'env', hint: null,
        models: [{ id: MODEL, label: 'Gemini 3.5 Flash-Lite', price: null }],
      }],
    }),
  }));
}

async function selectDescriptionSource(page: Page): Promise<void> {
  const picker = page.getByTestId('text-source-columns');
  await picker.click();
  await page.getByTestId('text-source-columns-menu')
    .getByRole('option', { name: /^description\b/ }).click();
  await page.getByLabel('Filter columns').press('Escape');
  await expect(picker.getByRole('button', { name: 'Remove description' })).toBeVisible();
}

// Add a JSON column to an existing sheet (no model, no run) and return its id.
// Mirrors the extract_pdf_tables end-state: a materialized JSON list column.
function addJsonColumn(pid: string, sheetId: number, name: string): number {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import sys
from pathlib import Path
from frisket.engine.store import Project

workspace, pid, sheet_id_raw, name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    column_id = project.add_column(int(sheet_id_raw), name, "json")
    project.db.commit()
    print(column_id)
finally:
    project.close()
`;
  const out = execFileSync(PYTHON, ['-c', script, workspace, pid, String(sheetId), name], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
  return Number(out.toString().trim());
}

function primeDeriveObjectCache(pid: string, sheetId: number): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import sys
from pathlib import Path
from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter, ResponseCache, request_key
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project

workspace, pid, sheet_id_raw = sys.argv[1], sys.argv[2], sys.argv[3]
sheet_id = int(sheet_id_raw)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    input_columns = [
        c["name"] for c in project.columns(sheet_id) if not c["ai_generated"]
    ]
    action = {
        "action_id": "map.extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": input_columns,
            "model": "gemini/gemini-3.5-flash-lite",
            "instruction": ${JSON.stringify(PROMPT)},
            "fields": [
                {
                    "name": "sponsors",
                    "type": "list",
                    "description": "Sponsors mentioned in this episode",
                    "items": {
                        "type": "object",
                        "properties": {
                            "company": {
                                "type": "string",
                                "description": "Sponsor company name",
                            },
                            "coupon": {
                                "type": "string",
                                "description": "Coupon code or discount",
                            },
                            "product": {
                                "type": "string",
                                "description": "Product being advertised",
                            },
                        },
                        "required": ["company", "coupon", "product"],
                    },
                }
            ],
        },
        "output_names": {"sponsors": "sponsors"},
        "idempotency_key": "e2e-cache-prime",
    }
    plan = build_typed_map_rows_plan(project, typed_action_for_request(action))
    spec = plan.spec_dict()
    columns = {c["name"]: c["id"] for c in project.columns(sheet_id)}
    values = {
        name: project.get_values(sheet_id, columns[name])
        for name in input_columns
    }
    payloads = [
        {
            "sponsors": [
                {
                    "company": "AG1",
                    "coupon": "ROGAN",
                    "product": "daily greens supplement",
                },
                {
                    "company": "Cash App",
                    "coupon": "signup bonus",
                    "product": "mobile payments app",
                },
            ],
        },
        {
            "sponsors": [
                {
                    "company": "Squarespace",
                    "coupon": "10% off",
                    "product": "website builder",
                },
                {
                    "company": "AG1",
                    "coupon": "ROGAN",
                    "product": "daily greens supplement",
                },
            ],
        },
    ]
    cache = ResponseCache(project.path / "project.cache.db")
    try:
        for row_id, payload in zip(project.visible_row_ids(sheet_id), payloads, strict=True):
            row_values = {name: values[name].get(row_id) for name in input_columns}
            call = plan.program.render(row_values, spec)
            req = LLMRequest(
                model=spec["model"],
                messages=call.messages,
                schema=call.schema,
                max_tokens=call.max_tokens,
            )
            cache.put(
                request_key(req, plan.program.version),
                LLMResponse(
                    content=None,
                    data=payload,
                    tokens_in=180,
                    tokens_out=90,
                    cost=0.0004,
                    model=spec["model"],
                ),
            )
    finally:
        cache.close()
finally:
    project.close()
`;
  execFileSync(PYTHON, ['-c', script, workspace, pid, String(sheetId)], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
}

async function waitForSheet(
  request: Parameters<typeof listSheets>[0],
  pid: string,
  name: string,
): Promise<WireSheet> {
  for (let i = 0; i < 60; i += 1) {
    const sheet = (await listSheets(request, pid)).find((s) => s.name === name);
    if (sheet) return sheet;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`sheet ${name} did not appear`);
}

test('derive materializes arrays of typed objects into a child sheet', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-derive-object-list'));
  const sheetId = await importCsv(page.request, pid, 'episodes.csv', CSV);
  primeDeriveObjectCache(pid, sheetId);
  await exposeReplayModel(page);

  const postedActions: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/derive`, async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'derive object-list should use v1 actions' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    postedActions.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.continue();
  });

  await page.goto(`/p/${pid}`);
  await openAction(page, 'derive.table_from_list');
  await page.getByTestId('derive-source-mode-ai').click();
  await selectDescriptionSource(page);
  await page.getByTestId('field-instruction').fill(PROMPT);
  await page.getByTestId('output-field-name').fill('sponsors');
  await page.getByTestId('output-field-description').fill('Sponsors mentioned in this episode');
  await page.locator('#derive-target-sheet').fill('Sponsors');

  await expect(page.getByTestId('list-item-schema')).toBeVisible();
  await page.getByTestId('list-item-kind').selectOption('object');
  await page.getByTestId('list-item-field-name').nth(0).fill('company');
  await page.getByTestId('list-item-field-description').nth(0).fill('Sponsor company name');
  await page.getByTestId('list-item-field-add').click();
  await page.getByTestId('list-item-field-name').nth(1).fill('coupon');
  await page.getByTestId('list-item-field-description').nth(1).fill('Coupon code or discount');
  await page.getByTestId('list-item-field-add').click();
  await page.getByTestId('list-item-field-name').nth(2).fill('product');
  await page.getByTestId('list-item-field-description').nth(2).fill('Product being advertised');

  await clickRunButton(page);

  await expect.poll(() => postedActions.length, { timeout: 10_000 }).toBe(2);
  const extract = postedActions.find((action) => action.action_id === 'map.extract');
  const materialize = postedActions.find((action) => action.action_id === 'derive.table_from_list');
  expect(extract).toBeTruthy();
  expect(materialize).toBeTruthy();
  const extractParams = extract?.params as Record<string, unknown>;
  const fields = extractParams.fields as Array<Record<string, unknown>>;
  const sponsors = fields[0];
  expect(sponsors.name).toBe('sponsors');
  expect(sponsors.type).toBe('list');
  expect(sponsors.items).toMatchObject({
    type: 'object',
    properties: {
      company: { type: 'string', description: 'Sponsor company name' },
      coupon: { type: 'string', description: 'Coupon code or discount' },
      product: { type: 'string', description: 'Product being advertised' },
    },
    required: ['company', 'coupon', 'product'],
  });
  const materializeParams = materialize?.params as Record<string, unknown>;
  expect(materializeParams.source).toMatchObject({
    kind: 'named_result',
    sheet_id: sheetId,
    route: 'sponsors',
    schema: 'sponsors_list',
  });
  expect(materialize?.sheet_name).toBe('Sponsors');
  expect(materialize?.scope).toEqual({ kind: 'project' });
  expect(materializeParams).not.toHaveProperty('target_sheet_name');
  expect(materializeParams.columns).toEqual([
    { name: 'company', path: '$.company', type: 'text' },
    { name: 'coupon', path: '$.coupon', type: 'text' },
    { name: 'product', path: '$.product', type: 'text' },
  ]);

  const child = await waitForSheet(page.request, pid, 'Sponsors');
  await page.goto(`/p/${pid}/s/${child.id}`);
  await expect(page.getByText('Sponsors').first()).toBeVisible();
  await expect(page.getByTestId('sheet-breadcrumb')).toContainText('via derive');
  await expect(page.getByTestId('sheet-stats')).toHaveText('4 rows · 3 columns');

  const childData = await sheetData(page.request, pid, child.id);
  expect(childData.columns.map((c) => [c.name, c.type])).toEqual([
    ['company', 'text'],
    ['coupon', 'text'],
    ['product', 'text'],
  ]);
  expect(childData.rows.map((r) => r.cells)).toEqual([
    { [String(childData.columns[0].id)]: 'AG1', [String(childData.columns[1].id)]: 'ROGAN', [String(childData.columns[2].id)]: 'daily greens supplement' },
    { [String(childData.columns[0].id)]: 'Cash App', [String(childData.columns[1].id)]: 'signup bonus', [String(childData.columns[2].id)]: 'mobile payments app' },
    { [String(childData.columns[0].id)]: 'Squarespace', [String(childData.columns[1].id)]: '10% off', [String(childData.columns[2].id)]: 'website builder' },
    { [String(childData.columns[0].id)]: 'AG1', [String(childData.columns[1].id)]: 'ROGAN', [String(childData.columns[2].id)]: 'daily greens supplement' },
  ]);
});

const LIST_CSV = 'title\n"Q1 filing"\n"Q2 filing"\n';

test('derive "From existing list column" materializes a JSON list column with no model call', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-derive-list-column'));
  const sheetId = await importCsv(page.request, pid, 'filings.csv', LIST_CSV);
  const columnId = addJsonColumn(pid, sheetId, 'pdf_tables');

  // Write list-of-object cells with the real cell.edit action — no model.
  const before = await sheetData(page.request, pid, sheetId);
  const [rowA, rowB] = before.rows.map((r) => r.id);
  await editCells(page.request, pid, [
    {
      rowId: rowA,
      columnId,
      value: [
        { vendor: 'Acme', amount: '1200' },
        { vendor: 'Globex', amount: '800' },
      ],
    },
    {
      rowId: rowB,
      columnId,
      value: [{ vendor: 'Initech', amount: '450' }],
    },
  ]);

  // Fail loudly if any model/cache endpoint is touched.
  let modelCalls = 0;
  await page.route('**/api/projects/**/actions/v1/run', async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    const caps = (body?.capabilities as string[]) ?? [];
    if (caps.includes('model:complete') || body?.kind === 'map.extract') modelCalls += 1;
    await route.continue();
  });

  await page.goto(`/p/${pid}`);
  await openAction(page, 'derive.table_from_list');

  // Switch to the new "From existing list column" mode.
  await page.getByTestId('derive-source-mode-column').click();
  await page.getByTestId('derive-source-column-select').selectOption('pdf_tables');
  // No prompt / model / fields in this mode; footer advertises a local run.
  await expect(page.getByTestId('action-prompt')).toHaveCount(0);
  await expect(page.getByTestId('model-picker-button')).toHaveCount(0);
  await expect(page.getByTestId('cost-estimate')).toContainText('No execution charge: $0.00');

  await page.getByTestId('field-sheet_name').fill('PDF Rows');
  await clickRunButton(page);

  const child = await waitForSheet(page.request, pid, 'PDF Rows');
  await page.goto(`/p/${pid}/s/${child.id}`);
  await expect(page.getByTestId('sheet-breadcrumb')).toContainText('via derive');
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 2 columns');

  const childData = await sheetData(page.request, pid, child.id);
  expect(childData.columns.map((c) => c.name)).toEqual(['vendor', 'amount']);
  expect(childData.rows.map((r) => r.cells[String(childData.columns[0].id)])).toEqual([
    'Acme',
    'Globex',
    'Initech',
  ]);
  expect(modelCalls).toBe(0);
});
