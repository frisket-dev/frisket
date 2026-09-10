import { expect, test, type Page } from '@playwright/test';
import {
  clickCell,
  createProject,
  listSheets,
  openAction,
  sheetColumns,
  simplePdf,
  uniqueName,
} from './helpers';

function crc32(buf: Buffer): number {
  let crc = 0xffffffff;
  for (const byte of buf) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function zipStore(files: Array<{ name: string; body: string }>): Buffer {
  const localParts: Buffer[] = [];
  const centralParts: Buffer[] = [];
  let offset = 0;

  for (const file of files) {
    const name = Buffer.from(file.name, 'utf8');
    const data = Buffer.from(file.body, 'utf8');
    const crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 6);
    local.writeUInt16LE(0, 8);
    local.writeUInt16LE(0, 10);
    local.writeUInt16LE(0, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(name.length, 26);
    local.writeUInt16LE(0, 28);
    localParts.push(local, name, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt16LE(0, 12);
    central.writeUInt16LE(0, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(name.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt16LE(0, 34);
    central.writeUInt16LE(0, 36);
    central.writeUInt32LE(0, 38);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, name);

    offset += local.length + name.length + data.length;
  }

  const centralDirectory = Buffer.concat(centralParts);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(files.length, 8);
  end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(centralDirectory.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...localParts, centralDirectory, end]);
}

function simpleDocx(): Buffer {
  return zipStore([
    {
      name: '[Content_Types].xml',
      body:
        '<?xml version="1.0" encoding="UTF-8"?>' +
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
        '<Default Extension="xml" ContentType="application/xml"/>' +
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' +
        '</Types>',
    },
    {
      name: '_rels/.rels',
      body:
        '<?xml version="1.0" encoding="UTF-8"?>' +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' +
        '</Relationships>',
    },
    {
      name: 'word/document.xml',
      body:
        '<?xml version="1.0" encoding="UTF-8"?>' +
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
        '<w:body>' +
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Budget Memo</w:t></w:r></w:p>' +
        '<w:p><w:r><w:t>Spending rose sharply.</w:t></w:r></w:p>' +
        '</w:body>' +
        '</w:document>',
    },
  ]);
}

async function stubActionRun(
  page: Page,
  pid: string,
): Promise<{
  legacyPosts: Array<Record<string, unknown>>;
  v1Posts: Array<Record<string, unknown>>;
  statusPolls: string[];
}> {
  const legacyPosts: Array<Record<string, unknown>> = [];
  const v1Posts: Array<Record<string, unknown>> = [];
  const statusPolls: string[] = [];
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    legacyPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'to_markdown should not use legacy /run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    v1Posts.push(body);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.action_id, action_id: 'act-web-to-markdown' },
        status: 'queued',
        project_id: pid,
        run_id: 9821,
        job_id: 4821,
        receipt_id: 'receipt-web-to-markdown',
        op_ids: [],
        outputs: [],
        warnings: [],
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/*/status`, async (route) => {
    statusPolls.push(route.request().url());
    const match = route.request().url().match(/\/runs\/([^/]+)\/status$/);
    const id = Number(match?.[1] ?? 9821);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.actions.v1',
        action: 'run_status',
        project_id: pid,
        run: {
          id,
          sheet_id: 1,
          action_kind: 'media.to_markdown',
          action_name: 'To Markdown',
          status: 'queued',
          total_rows: 1,
          completed_rows: 0,
          failed_rows: 0,
          cost_actual: 0,
          cost_estimate: 0,
          public_status: {
            run_id: id,
            action_kind: 'media.to_markdown',
            action_name: 'To Markdown',
            status: 'queued',
            total: 1,
            completed: 0,
            failed: 0,
            cost: 0,
            live: false,
          },
        },
      }),
    });
  });
  return { legacyPosts, v1Posts, statusPolls };
}

async function recordLiveActionRun(
  page: Page,
  pid: string,
): Promise<{
  legacyPosts: Array<Record<string, unknown>>;
  v1Posts: Array<Record<string, unknown>>;
  v1Results: Array<Record<string, unknown>>;
}> {
  const legacyPosts: Array<Record<string, unknown>> = [];
  const v1Posts: Array<Record<string, unknown>> = [];
  const v1Results: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    legacyPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'to_markdown should not use legacy /run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    v1Posts.push(body);
    const response = await route.fetch();
    const result = await response.json() as Record<string, unknown>;
    v1Results.push(result);
    await route.fulfill({
      status: response.status(),
      contentType: response.headers()['content-type'] ?? 'application/json',
      body: JSON.stringify(result),
    });
  });
  return { legacyPosts, v1Posts, v1Results };
}

async function markToMarkdownCatalogEngine(page: Page): Promise<void> {
  await page.route('**/actions/v1/catalog', async (route) => {
    const response = await route.fetch();
    const catalog = await response.json();
    const patched = catalog.actions.map((action: Record<string, unknown>) => {
      if (action.kind !== 'media.to_markdown') return action;
      const uiHints = (action.ui_hints as Record<string, unknown> | undefined) ?? {};
      const engines = Array.isArray(uiHints.engines)
        ? uiHints.engines.map((engine: Record<string, unknown>) => {
            if (engine.id !== 'docling') return engine;
            return {
              ...engine,
              label: 'Docling catalog proof',
              available: true,
              error: null,
            };
          })
        : uiHints.engines;
      return {
        ...action,
        ui_hints: {
          ...uiHints,
          engines,
        },
      };
    });
    await route.fulfill({
      status: response.status(),
      contentType: 'application/json',
      body: JSON.stringify({ ...catalog, actions: patched }),
    });
  });
}

test('to_markdown launches media.to_markdown through the v1 action endpoint', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-v1-to-markdown'));
  const posts = await stubActionRun(page, pid);

  const importRes = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=documents`, {
    multipart: {
      files: {
        name: 'report.pdf',
        mimeType: 'application/pdf',
        buffer: simplePdf(['Quarterly Report', 'Revenue doubled.']),
      },
    },
  });
  expect(importRes.ok()).toBeTruthy();

  const sheets = await listSheets(page.request, pid);
  const docSheet = sheets.find((sheet) => sheet.name === 'documents');
  expect(docSheet).toBeTruthy();

  await markToMarkdownCatalogEngine(page);
  await page.goto(`/p/${pid}/s/${docSheet!.id}`);
  await openAction(page, 'media.to_markdown');
  await expect(page.getByTestId('field-source')).toBeVisible();
  await expect(page.getByTestId('field-source')).toHaveValue('media');
  await page.getByTestId('engine-picker-button').click();
  await page.getByTestId('engine-picker-tier-sidecar').click();
  await expect(page.getByTestId('engine-option-docling')).toContainText('Docling catalog proof');
  await page.getByTestId('engine-option-docling').click();
  await page.getByTestId('generated-action-run').click();

  await expect.poll(() => posts.v1Posts.length, { timeout: 5000 }).toBe(1);
  expect(posts.legacyPosts).toHaveLength(0);
  const posted = posts.v1Posts[0];
  expect(posted.action_id).toBe('media.to_markdown');
  expect(typeof posted.idempotency_key).toBe('string');

  const params = posted.params as Record<string, unknown>;
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: docSheet!.id });
  expect(params.source).toBe('media');
  expect(params.engine).toBe('docling');
  expect(posted.output_names).toMatchObject({ markdown: 'markdown' });
  expect(params).not.toHaveProperty('confirmed');
  expect(posted).not.toHaveProperty('confirmation');
  await expect
    .poll(() => posts.statusPolls.length, { timeout: 5000 })
    .toBeGreaterThan(0);
  expect(posts.statusPolls[0]).toContain(`/api/projects/${pid}/actions/runs/9821/status`);
});

test('to_markdown converts DOCX through local markitdown and renders markdown output', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-v1-to-markdown-docx'));
  const posts = await recordLiveActionRun(page, pid);

  const importRes = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=documents`, {
    multipart: {
      files: {
        name: 'budget.docx',
        mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        buffer: simpleDocx(),
      },
    },
  });
  expect(importRes.ok()).toBeTruthy();

  const sheets = await listSheets(page.request, pid);
  const docSheet = sheets.find((sheet) => sheet.name === 'documents');
  expect(docSheet).toBeTruthy();

  await page.goto(`/p/${pid}/s/${docSheet!.id}`);
  await openAction(page, 'media.to_markdown');
  await expect(page.getByTestId('field-source')).toHaveValue('media');
  await expect(page.getByTestId('field-output-markdown')).toHaveValue('markdown');

  await page.getByTestId('generated-action-run').click();
  await expect.poll(() => posts.v1Results.length, { timeout: 15000 }).toBe(1);
  expect(posts.legacyPosts).toHaveLength(0);

  const posted = posts.v1Posts[0];
  expect(posted.action_id).toBe('media.to_markdown');
  const params = posted.params as Record<string, unknown>;
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: docSheet!.id });
  expect(params.source).toBe('media');
  expect(params.engine).toBe('markitdown');
  expect(posted.output_names).toMatchObject({ markdown: 'markdown' });

  await expect.poll(async () => {
    const columns = await sheetColumns(page.request, pid, docSheet!.id);
    return columns.find((column) => column.name === 'markdown')?.format ?? null;
  }, { timeout: 15000 }).toBe('markdown');

  const columns = await sheetColumns(page.request, pid, docSheet!.id);
  const markdownColumn = columns.find((column) => column.name === 'markdown');
  expect(markdownColumn?.type).toBe('text');
  expect(markdownColumn?.format).toBe('markdown');

  const receiptId = posts.v1Results[0].receipt_id;
  expect(typeof receiptId).toBe('string');
  await expect.poll(async () => {
    const receiptRes = await page.request.get(
      `/api/projects/${pid}/actions/v1/receipts/${receiptId as string}`,
    );
    expect(receiptRes.ok()).toBeTruthy();
    const body = await receiptRes.json() as {
      status?: string;
    };
    return body.status === 'completed' ? 'completed' : JSON.stringify(body);
  }, { timeout: 15000 }).toBe('completed');
  const receiptRes = await page.request.get(
    `/api/projects/${pid}/actions/v1/receipts/${receiptId as string}`,
  );
  expect(receiptRes.ok()).toBeTruthy();
  const receipt = await receiptRes.json() as {
    provider_use?: Array<Record<string, unknown>>;
    outputs?: Array<{ ref?: Record<string, unknown> }>;
  };
  expect(receipt.provider_use).toContainEqual(expect.objectContaining({
    provider: 'local',
    model: 'markitdown',
  }));
  expect(receipt.outputs?.map((output) => output.ref)).toEqual(expect.arrayContaining([
    expect.objectContaining({
      kind: 'map_result_column',
      name: 'markdown',
      column_id: markdownColumn!.id,
    }),
  ]));

  await clickCell(page, columns, 'media', 0);
  await page.keyboard.press('Enter');
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('markdown-value').locator('h1')).toHaveText('Budget Memo');
});
