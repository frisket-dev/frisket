// RED-FIRST (authored 2026-06-13): a brand-new empty project should expose the
// same import/source choices as the normal toolbar import surface.

import { expect, test, type Page } from '@playwright/test';
import { createProject, listSheets, openDiscoverTab, sheetData, uniqueName } from './helpers';

function minimalPdf(text: string): Buffer {
  const content = `BT /F1 24 Tf 72 700 Td (${text}) Tj ET`;
  const objects = [
    '1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n',
    '2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n',
    '3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n',
    `4 0 obj<</Length ${Buffer.byteLength(content)}>>stream\n${content}\nendstream\nendobj\n`,
    '5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n',
  ];
  let out = '%PDF-1.4\n';
  const offsets: number[] = [];
  for (const object of objects) {
    offsets.push(Buffer.byteLength(out));
    out += object;
  }
  const xref = Buffer.byteLength(out);
  out += 'xref\n0 6\n0000000000 65535 f \n';
  for (const offset of offsets) {
    out += `${offset.toString().padStart(10, '0')} 00000 n \n`;
  }
  out += `trailer<</Size 6/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF`;
  return Buffer.from(out, 'utf-8');
}

async function dropFiles(
  page: Page,
  selector: string,
  files: Array<{ name: string; mimeType: string; buffer: Buffer }>,
) {
  const dataTransfer = await page.evaluateHandle((browserFiles) => {
    const transfer = new DataTransfer();
    for (const browserFile of browserFiles) {
      const bytes = Uint8Array.from(
        atob(browserFile.base64),
        (char) => char.charCodeAt(0),
      );
      transfer.items.add(
        new File([bytes], browserFile.name, { type: browserFile.mimeType }),
      );
    }
    return transfer;
  }, files.map((file) => ({
    name: file.name,
    mimeType: file.mimeType,
    base64: file.buffer.toString('base64'),
  })));

  const target = page.locator(selector);
  await target.dispatchEvent('dragover', { dataTransfer });
  await target.dispatchEvent('drop', { dataTransfer });
}

test('empty project start surface exposes all import modes, not only CSV', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-empty-import-surface'));
  await page.goto(`/p/${pid}`);

  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Drop a CSV here')).toHaveCount(0);
  await page.getByTestId('import-workspace-open').click();
  await expect(page.getByTestId('import-workspace')).toBeVisible();

  for (const mode of ['csv', 'files', 'feed']) {
    await expect(
      page.getByTestId(`import-mode-${mode}`),
      `empty-project import mode '${mode}' should be visible`,
    ).toBeVisible();
  }
});

test('empty project CSV drop opens preview and waits for confirmation', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-empty-csv-drop'));
  let csvImports = 0;
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (
      request.method() === 'POST'
      && url.pathname.endsWith(`/api/projects/${pid}/import/csv`)
    ) {
      csvImports += 1;
    }
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
  await dropFiles(page, '[data-testid="import-dropzone"]', [
    {
      name: 'dropped.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from('name,score\nAda,7\n', 'utf-8'),
    },
  ]);

  await expect(page.getByTestId('import-workspace')).toBeVisible();
  await expect(page.getByTestId('import-csv-preview')).toBeVisible();
  await expect(page.getByTestId('import-csv-preview')).toContainText('dropped.csv');
  expect(csvImports).toBe(0);

  await page.getByTestId('import-csv-confirm').click();
  await expect.poll(() => csvImports).toBe(1);
  await expect
    .poll(async () => (await listSheets(page.request, pid)).some((sheet) => sheet.name === 'dropped'))
    .toBeTruthy();
});

test('empty project dropzone routes dropped PDF selections through file import, not CSV', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-empty-pdf-drop'));
  let csvUploadRequests = 0;
  await page.route(`**/api/projects/${pid}/import/csv`, async (route) => {
    csvUploadRequests += 1;
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'PDF drops must not use multipart /import/csv' }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });

  const fileImportRequest = page.waitForRequest(
    (request) =>
      request.url().includes(`/api/projects/${pid}/import/files`) &&
      request.method() === 'POST',
    { timeout: 5_000 },
  );
  await dropFiles(page, '[data-testid="import-dropzone"]', [
    {
      name: 'notice-a.pdf',
      mimeType: 'application/pdf',
      buffer: minimalPdf('Dropped PDF A'),
    },
    {
      name: 'notice-b.pdf',
      mimeType: 'application/pdf',
      buffer: minimalPdf('Dropped PDF B'),
    },
  ]);
  await fileImportRequest;

  await expect
    .poll(async () => (await listSheets(page.request, pid)).some((s) => s.name === 'files'), {
      timeout: 15_000,
    })
    .toBeTruthy();
  expect(csvUploadRequests).toBe(0);

  const filesSheet = (await listSheets(page.request, pid)).find((s) => s.name === 'files');
  expect(filesSheet).toBeTruthy();
  const data = await sheetData(page.request, pid, filesSheet!.id);
  const filenameCol = data.columns.find((c) => c.name === 'filename');
  expect(filenameCol).toBeTruthy();
  expect(data.rows.map((row) => row.cells[String(filenameCol!.id)])).toEqual([
    'notice-a.pdf',
    'notice-b.pdf',
  ]);
});

test('Feed source creation is discoverable from the primary import/start surface', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-feed-discovery'));
  await page.goto(`/p/${pid}`);

  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
  await page.getByTestId('import-workspace-open').click();
  await page.getByTestId('import-mode-feed').click();

  await expect(page.getByTestId('source-form')).toBeVisible();
  await expect(page.getByTestId('source-url')).toBeVisible();
  await expect(page.getByTestId('source-kind')).toBeVisible();
  await expect(page.getByTestId('source-interval')).toBeVisible();
  await expect(page.getByTestId('source-interval')).toHaveValue('');

  const urlBox = await page.getByTestId('source-url').boundingBox();
  const kindBox = await page.getByTestId('source-kind').boundingBox();
  expect(urlBox).toBeTruthy();
  expect(kindBox).toBeTruthy();
  expect(urlBox!.y).toBeLessThan(kindBox!.y);

  await page.getByTestId('source-url').fill('http://example.gov/api/items.json');
  await expect(page.getByTestId('source-kind')).toHaveValue('api_list_dicts');
  await expect(page.getByTestId('source-api-list-path')).toBeVisible();
  await expect(page.getByTestId('source-create')).toBeDisabled();
  await expect(page.getByTestId('source-form')).toContainText('API list sources require HTTPS');

  await page.getByTestId('source-url').fill('https://example.gov/api/items.json');
  await page.getByTestId('source-api-list-path').fill('results');
  await expect(page.getByTestId('source-create')).toBeDisabled();
  await expect(page.getByTestId('source-form')).toContainText('Response list path must start with /');
  await page.getByTestId('source-api-list-path').fill('/results');

  const apiListCreate = page.waitForRequest((request) =>
    request.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    request.method() === 'POST' &&
    request.postDataJSON().action_id === 'source.create' &&
    request.postDataJSON().params.kind === 'api_list_dicts',
  );
  await page.getByTestId('source-create').click();
  const apiPayload = (await apiListCreate).postDataJSON();
  expect(apiPayload.params).toMatchObject({
    kind: 'api_list_dicts',
    url: 'https://example.gov/api/items.json',
    schedule: null,
    config: {
      schema_version: 'frisket.source.api_list_dicts.v1',
      method: 'GET',
      url: 'https://example.gov/api/items.json',
      list_path: '/results',
      schema_policy: 'additive',
    },
  });

  // feed-add-populate-prompt-v1: creation swaps to a "Populate feed?" prompt
  // instead of closing outright — decline it (this test is about the create
  // payload, not the populate run).
  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible();
  await page.getByTestId('feed-populate-dismiss').click();
  await expect(page.getByTestId('import-workspace')).toHaveCount(0, { timeout: 10_000 });
  await page.getByTestId('import-workspace-open').click();
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('source-url').fill('https://www.youtube.com/@frisketdemo');
  await expect(page.getByTestId('source-kind')).toHaveValue('youtube_channel');

  const youtubeCreate = page.waitForRequest((request) =>
    request.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    request.method() === 'POST' &&
    request.postDataJSON().action_id === 'source.create' &&
    request.postDataJSON().params.kind === 'youtube_channel',
  );
  await page.getByTestId('source-create').click();
  const youtubePayload = (await youtubeCreate).postDataJSON();
  expect(youtubePayload.params).toMatchObject({
    kind: 'youtube_channel',
    url: 'https://www.youtube.com/@frisketdemo',
    schedule: null,
    config: {
      schema_version: 'frisket.source.youtube.v1',
      channel_url: 'https://www.youtube.com/@frisketdemo',
    },
  });

  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible();
  await page.getByTestId('feed-populate-dismiss').click();

  await openDiscoverTab(page, 'Sources');
  await expect(page.getByTestId('source-add-button')).toHaveCount(0);
});
