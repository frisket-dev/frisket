import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

type PostedAction = {
  schema_version?: string;
  kind?: string;
  action_id?: string;
  scope?: { kind: 'project' };
  sheet_name?: string;
  capabilities?: string[];
  params: Record<string, unknown>;
  idempotency_key?: string;
};

async function stubV1Composite(
  page: Page,
  pid: string,
): Promise<{
  derivePosts: Array<Record<string, unknown>>;
  legacyRunPosts: Array<Record<string, unknown>>;
  v1Posts: PostedAction[];
  statusPolls: string[];
  receiptLookups: string[];
}> {
  const derivePosts: Array<Record<string, unknown>> = [];
  const legacyRunPosts: Array<Record<string, unknown>> = [];
  const v1Posts: PostedAction[] = [];
  const statusPolls: string[] = [];
  const receiptLookups: string[] = [];

  const extractOutput = {
    kind: 'named_result',
    name: 'sponsors',
    sheet_id: 1,
    column_id: 44,
    row_ids: [1, 2],
    ref: {
      kind: 'named_result',
      sheet_id: 1,
      column_id: 44,
      run_id: 9901,
      op_id: 91,
      route: 'sponsors',
      schema: 'sponsors_list',
      item_schema: {
        type: 'object',
        properties: {
          company: { type: 'string' },
          coupon: { type: 'string' },
          product: { type: 'string' },
        },
        required: ['company', 'coupon', 'product'],
      },
      source_action_kind: 'map.extract',
      row_ids: [1, 2],
      may_feed: ['derive.table_from_list'],
    },
  };

  await page.route(`**/api/projects/${pid}/derive`, async (route) => {
    derivePosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'derive rows should compose v1 actions' }),
    });
  });
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    legacyRunPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'derive rows should not use legacy /run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as PostedAction;
    v1Posts.push(body);
    if (body.kind === 'map.extract') {
      extractOutput.sheet_id = Number(body.params.sheet_id);
      extractOutput.ref.sheet_id = Number(body.params.sheet_id);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.action_result.v1',
          action: { kind: 'map.extract', action_id: 'act-web-derive-extract' },
          status: 'queued',
          project_id: pid,
          run_id: 9901,
          receipt_id: 'receipt-web-derive-extract',
          outputs: [],
          errors: [],
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'derive.table_from_list', action_id: 'act-web-derive-table' },
        status: 'completed',
        project_id: pid,
        run_id: null,
        receipt_id: 'receipt-web-derive-table',
        outputs: [
          {
            kind: 'sheet',
            name: 'Sponsors',
            sheet_id: 77,
            ref: { kind: 'materialized_sheet', sheet_id: 77, op_id: 92 },
          },
        ],
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/9901/status`, async (route) => {
    statusPolls.push(route.request().url());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.actions.v1',
        action: 'run_status',
        project_id: pid,
        run: {
          id: 9901,
          sheet_id: extractOutput.sheet_id,
          action_kind: 'map.extract',
          action_name: 'Extract structured fields',
          status: 'completed',
          total_rows: 2,
          completed_rows: 2,
          failed_rows: 0,
          cost_actual: 0,
          cost_estimate: 0,
          public_status: {
            run_id: 9901,
            action_kind: 'map.extract',
            action_name: 'Extract structured fields',
            status: 'completed',
            total: 2,
            completed: 2,
            failed: 0,
            cost: 0,
            live: false,
          },
        },
      }),
    });
  });
  await page.route(
    `**/api/projects/${pid}/actions/v1/receipts/receipt-web-derive-extract`,
    async (route) => {
      receiptLookups.push(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.receipt.v1',
          receipt_id: 'receipt-web-derive-extract',
          project_id: pid,
          action_id: 'act-web-derive-extract',
          action_kind: 'map.extract',
          run_id: 9901,
          op_ids: [91],
          idempotency_key: 'web-derive-extract-key',
          params_hash: 'sha256:extract',
          status: 'completed',
          inputs: [],
          outputs: [{ name: 'sponsors', ref: extractOutput.ref }],
          provider_use: [],
          evidence: [],
          errors: [],
        }),
      });
    },
  );

  return { derivePosts, legacyRunPosts, v1Posts, statusPolls, receiptLookups };
}

test('derive rows composes map.extract and derive.table_from_list through v1', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-v1-derive-composite'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'episodes.csv',
    [
      'episode,description',
      '"Fitness episode","The host reads an AG1 ad with code ROGAN and a Cash App spot."',
      '"Tools episode","A Squarespace segment offers 10% off, then AG1 appears again."',
    ].join('\n'),
  );
  const posts = await stubV1Composite(page, pid);

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'derive.table_from_list');
  await page.getByTestId('action-prompt').fill(
    'List every sponsor mentioned in this row. For each sponsor, return the company, coupon, and product.',
  );
  await page.getByTestId('output-field-name').fill('sponsors');
  await page.getByTestId('field-item_field').fill('sponsors');
  await page.getByTestId('field-child_sheet').fill('Sponsors');
  await page.getByTestId('list-item-kind').selectOption('object');
  await page.getByTestId('list-item-field-name').nth(0).fill('company');
  await page.getByTestId('list-item-field-name').nth(1).fill('coupon');
  await page.getByTestId('list-item-field-add').click();
  await page.getByTestId('list-item-field-name').nth(2).fill('product');

  await page.getByTestId('run-button').click();

  await expect.poll(() => posts.v1Posts.length, { timeout: 5000 }).toBe(2);
  await expect.poll(() => posts.statusPolls.length, { timeout: 5000 }).toBeGreaterThan(0);
  await expect.poll(() => posts.receiptLookups.length, { timeout: 5000 }).toBe(1);
  expect(posts.derivePosts).toHaveLength(0);
  expect(posts.legacyRunPosts).toHaveLength(0);

  const [extract, materialize] = posts.v1Posts;
  expect(extract.schema_version).toBe('frisket.action.v2');
  expect(extract.kind).toBe('map.extract');
  expect(extract.capabilities).toEqual(['project:write', 'model:complete']);
  expect(extract.params.sheet_id).toBe(sheetId);
  expect(extract.params.input_columns).toEqual(['episode', 'description']);
  expect(extract.params.instruction).toContain('List every sponsor mentioned');
  expect(extract.params.confirmed).toBe(false);
  expect(extract.idempotency_key).toContain('map.extract');

  const fields = extract.params.fields as Array<Record<string, unknown>>;
  expect(fields[0]).toMatchObject({
    name: 'sponsors',
    type: 'list',
    items: {
      type: 'object',
      properties: {
        company: { type: 'string' },
        coupon: { type: 'string' },
        product: { type: 'string' },
      },
      required: ['company', 'coupon', 'product'],
    },
  });

  expect(materialize).not.toHaveProperty('schema_version');
  expect(materialize).not.toHaveProperty('kind');
  expect(materialize).not.toHaveProperty('capabilities');
  expect(materialize.action_id).toBe('derive.table_from_list');
  expect(materialize.scope).toEqual({ kind: 'project' });
  expect(materialize.idempotency_key).toContain('derive.table_from_list');
  expect(materialize.params.source).toEqual({
    kind: 'named_result',
    sheet_id: sheetId,
    column_id: 44,
    run_id: 9901,
    route: 'sponsors',
    schema: 'sponsors_list',
  });
  expect(materialize.sheet_name).toBe('Sponsors');
  expect(materialize.params).not.toHaveProperty('target_sheet_name');
  expect(materialize.params.item_schema).toMatchObject({
    type: 'object',
    properties: {
      company: { type: 'string' },
      coupon: { type: 'string' },
      product: { type: 'string' },
    },
  });
  expect(materialize.params.columns).toEqual([
    { name: 'company', path: '$.company', type: 'text' },
    { name: 'coupon', path: '$.coupon', type: 'text' },
    { name: 'product', path: '$.product', type: 'text' },
  ]);
});
