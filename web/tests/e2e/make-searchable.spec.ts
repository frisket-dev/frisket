import { readFileSync } from 'node:fs';
import path from 'node:path';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import {
  createProject,
  importCsv,
  listSheets,
  openImportWorkspace,
  openPalette,
  sheetData,
  textPdf,
  uniqueName,
} from './helpers';

test.use({
  deviceScaleFactor: 2,
  viewport: { width: 1600, height: 900 },
});

const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const MAKE_SEARCHABLE_FIXTURES = path.resolve(process.cwd(), 'tests', 'e2e', 'fixtures');

type ImportFilesResult = {
  sheet_id: number;
  rows: number;
  search_preparation_batch_id: string | null;
};

type PreparationBatch = {
  id: string;
  sheet_id: number;
  plan_hash: string;
  status: 'planning' | 'needs_consent' | 'preparing' | 'complete' | 'stopped' | 'failed';
  groups: Array<{
    kind: 'native_pdf' | 'ocr' | 'transcribe';
    count: number;
    status: string;
  }>;
  models: Array<{
    setup_ref: string;
    purpose: string;
    required: boolean;
    ready: boolean;
    pull_id: number | null;
  }>;
  jobs: Array<{ job_id: number; status: string; action_kind: string | null }>;
  errors: string[];
};

type SearchResponse = {
  groups: Array<{
    primary: {
      snippet: string;
      navigation:
        | { kind: 'document'; sheet_id: number; row_id: number; column_id: number; page: number | null }
        | { kind: string };
    };
  }>;
};

async function modelPullInventory(request: APIRequestContext): Promise<string[]> {
  const response = await request.get('/api/providers/models/pulls');
  expect(response.ok()).toBeTruthy();
  const body = await response.json() as {
    pulls: Array<{ id: number; model: string }>;
  };
  return body.pulls
    .map((pull) => `${pull.id}:${pull.model}`)
    .sort();
}

async function preparationBatch(
  request: APIRequestContext,
  projectId: string,
  batchId: string,
): Promise<PreparationBatch> {
  const response = await request.get(
    `/api/projects/${projectId}/search-preparation/${batchId}`,
  );
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function waitForPreparation(
  request: APIRequestContext,
  projectId: string,
  batchId: string,
  status: PreparationBatch['status'],
  timeout = 30_000,
): Promise<PreparationBatch> {
  let latest: PreparationBatch | null = null;
  await expect.poll(async () => {
    latest = await preparationBatch(request, projectId, batchId);
    return latest.status;
  }, {
    message: `search preparation ${batchId} did not reach ${status}`,
    timeout,
    intervals: [100, 250, 500, 1_000],
  }).toBe(status);
  return latest!;
}

async function importFilesFromWorkspace(
  page: Page,
  projectId: string,
  files: Array<{ name: string; mimeType: string; buffer: Buffer }>,
): Promise<ImportFilesResult> {
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-files').click();
  const imported = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${projectId}/import/files`
  ));
  await page.getByTestId('import-file-input').setInputFiles(files);
  const response = await imported;
  expect(response.ok()).toBeTruthy();
  return response.json();
}

test('declining missing transcription setup starts no pull and remains recoverable from Search', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('make-searchable-decline'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  const pullsBefore = await modelPullInventory(page.request);
  const setupRequests: string[] = [];
  page.on('request', (request) => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'POST' && (
      pathname === '/api/providers/models/setup'
      || pathname === '/api/providers/models/pull'
    )) setupRequests.push(pathname);
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  const imported = await importFilesFromWorkspace(page, pid, [{
    name: 'short-recording.wav',
    mimeType: 'audio/wav',
    buffer: readFileSync(path.join(MEDIA_DIR, 'tiny-audio.wav')),
  }]);

  expect(imported.search_preparation_batch_id).toBeTruthy();
  let planned: PreparationBatch | null = null;
  await expect.poll(async () => {
    planned = await preparationBatch(
      page.request,
      pid,
      imported.search_preparation_batch_id!,
    );
    return planned.status;
  }, {
    message: 'audio preparation did not finish planning',
    timeout: 15_000,
  }).not.toBe('planning');
  const missingTranscription = planned!.models.find((model) => (
    model.purpose === 'transcribe' && !model.ready
  ));
  test.skip(
    !missingTranscription,
    'This consent-boundary case requires Parakeet to be absent from the server cache.',
  );
  const receipt = page.getByTestId('search-preparation-receipt');
  const consent = receipt.getByTestId('search-preparation-model-consent');
  await expect(consent).toBeVisible();
  await expect(consent.getByTestId('search-preparation-model-transcribe')).toContainText(
    /Parakeet|transcrib/i,
  );
  const missingOptionalModel = planned!.models.some((model) => !model.required && !model.ready);
  if (missingOptionalModel) {
    await expect(consent.getByTestId('search-preparation-semantic-model')).not.toBeChecked();
  } else {
    await expect(consent.getByTestId('search-preparation-semantic-model')).toHaveCount(0);
  }
  expect(setupRequests).toEqual([]);
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);

  const decisionResponse = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.endsWith('/decision')
  ));
  await consent.getByTestId('search-preparation-decision-not-now').click();
  const declinedResponse = await decisionResponse;
  expect(declinedResponse.ok()).toBeTruthy();
  expect(declinedResponse.request().postDataJSON()).toMatchObject({
    decision: 'not_now',
    selected_setup_refs: [],
  });
  const declined = await declinedResponse.json() as PreparationBatch;
  expect(declined.groups).toContainEqual({
    kind: 'transcribe',
    count: 1,
    status: 'not_prepared',
  });
  expect(setupRequests).toEqual([]);
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);

  await receipt.getByTestId('search-preparation-done').click();
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);

  const palette = await openPalette(page);
  await palette.getByTestId('search-mode-select').selectOption('lexical');
  await palette.getByTestId('command-palette-input').fill('recordingwordthatcannotexist');
  const gap = palette.getByTestId('search-preparation-gap');
  await expect(gap).toContainText(/recording|transcrib|prepare/i);
  const explicitPreparation = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${pid}/search-preparation`
  ));
  await gap.getByTestId('search-preparation-gap-action').click();
  const explicitResponse = await explicitPreparation;
  expect(explicitResponse.ok()).toBeTruthy();
  expect(explicitResponse.request().postDataJSON()).toEqual({
    sheet_id: imported.sheet_id,
  });
  await expect(page.getByTestId('search-preparation-receipt')).toBeVisible();
  expect(setupRequests).toEqual([]);
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);
});

test('native PDF import becomes searchable without starting a model setup', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('make-searchable-native-pdf'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  const pullsBefore = await modelPullInventory(page.request);
  const setupRequests: string[] = [];
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && (
      path === '/api/providers/models/setup'
      || path === '/api/providers/models/pull'
    )) setupRequests.push(path);
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  const imported = await importFilesFromWorkspace(page, pid, [{
    name: 'harbor-minutes.pdf',
    mimeType: 'application/pdf',
    buffer: textPdf([
      ['Harbor minutes', 'The first page records attendance.'],
      ['Repair decision', 'The council approved the bluewharf repair schedule.'],
    ], { fontSize: 20, startX: 54, startY: 720, leading: 28 }),
  }]);

  expect(imported.search_preparation_batch_id).toBeTruthy();
  const batchId = imported.search_preparation_batch_id!;
  const receipt = page.getByTestId('search-preparation-receipt');
  await expect(receipt).toBeVisible();
  await expect(receipt.getByTestId('search-preparation-model-consent')).toHaveCount(0);
  await expect(receipt.getByTestId('search-preparation-group-native_pdf')).toContainText(
    /searchable|reading text|complete/i,
  );

  const completed = await waitForPreparation(page.request, pid, batchId, 'complete');
  expect(completed.groups).toContainEqual({
    kind: 'native_pdf',
    count: 1,
    status: 'complete',
  });
  expect(completed.models).toEqual([]);
  expect(setupRequests).toEqual([]);
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);

  await receipt.getByTestId('search-preparation-done').click();
  await expect(receipt).toBeHidden();
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const sheets = await listSheets(page.request, pid);
  const filesSheet = sheets.find((sheet) => sheet.id === imported.sheet_id);
  expect(filesSheet?.name).toBe('files');

  const palette = await openPalette(page);
  await palette.getByTestId('search-mode-select').selectOption('lexical');
  const searched = page.waitForResponse((response) => {
    if (response.request().method() !== 'POST') return false;
    if (new URL(response.url()).pathname !== `/api/projects/${pid}/search`) return false;
    const body = response.request().postDataJSON() as {
      query?: { kind?: string; text?: string };
    };
    return body.query?.kind === 'text' && body.query.text === 'bluewharf';
  });
  await palette.getByTestId('command-palette-input').fill('bluewharf');
  const search = await searched;
  expect(search.ok()).toBeTruthy();
  const result = await search.json() as SearchResponse;
  const hit = result.groups.find((group) => group.primary.snippet.includes('bluewharf'))?.primary;
  expect(hit).toBeTruthy();
  expect(hit?.navigation).toMatchObject({
    kind: 'document',
    sheet_id: imported.sheet_id,
    page: 2,
  });

  const visibleHit = palette.getByTestId('search-hit').filter({ hasText: 'bluewharf' }).first();
  await expect(visibleHit).toBeVisible();
  await visibleHit.click();
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('document-page-indicator')).toContainText('2 / 2');
});

test('Check text explicitly requests replacement OCR without changing the original PDF', async ({
  page,
}) => {
  if (process.env.FRISKET_E2E_LIVE_SEARCH_PREPARATION === '1') {
    test.setTimeout(180_000);
  }
  const pid = await createProject(page.request, uniqueName('make-searchable-check-text'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const imported = await importFilesFromWorkspace(page, pid, [{
    name: 'questionable-text.pdf',
    mimeType: 'application/pdf',
    buffer: readFileSync(path.join(MAKE_SEARCHABLE_FIXTURES, 'bad-text-layer.pdf')),
  }]);
  expect(imported.search_preparation_batch_id).toBeTruthy();
  await waitForPreparation(
    page.request,
    pid,
    imported.search_preparation_batch_id!,
    'complete',
  );

  const before = await sheetData(page.request, pid, imported.sheet_id);
  const mediaColumn = before.columns.find((column) => column.name === 'media');
  expect(mediaColumn).toBeTruthy();
  const originalMedia = before.rows[0].cells[String(mediaColumn!.id)] as {
    blob: string;
    filename: string;
  };
  expect(originalMedia.blob).toMatch(/^[a-f0-9]{64}$/);
  const originalBlobResponse = await page.request.get(
    `/api/projects/${pid}/blobs/${originalMedia.blob}`,
  );
  expect(originalBlobResponse.ok()).toBeTruthy();
  const originalBytes = await originalBlobResponse.body();

  const receipt = page.getByTestId('search-preparation-receipt');
  await receipt.getByTestId('search-preparation-check-text').click();
  const preview = page.getByTestId('search-preparation-text-preview');
  await expect(preview).toBeVisible();
  await expect(preview).toContainText('garb1ed XXv0 vvv meaningless existing OCR');
  await expect(preview).toContainText(
    'This replaces Frisket’s search text. It does not change the original PDF.',
  );
  await expect(preview.getByTestId('search-preparation-force-ocr')).toBeVisible();
  await expect(preview.getByTestId('search-preparation-force-scope-this')).toBeChecked();

  const replacementRequest = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${pid}/search-preparation`
    && response.request().postDataJSON().force_ocr === true
  ));
  await preview.getByTestId('search-preparation-force-ocr').click();
  const replacementResponse = await replacementRequest;
  expect(replacementResponse.ok()).toBeTruthy();
  expect(replacementResponse.request().postDataJSON()).toEqual({
    sheet_id: imported.sheet_id,
    row_ids: [before.rows[0].id],
    force_ocr: true,
  });
  const replacement = await replacementResponse.json() as PreparationBatch;
  expect(replacement).toMatchObject({
    sheet_id: imported.sheet_id,
    groups: [{ kind: 'ocr', count: 1 }],
  });

  const after = await sheetData(page.request, pid, imported.sheet_id);
  const unchangedMedia = after.rows[0].cells[String(mediaColumn!.id)] as {
    blob: string;
    filename: string;
  };
  expect(unchangedMedia).toEqual(originalMedia);
  const unchangedBlobResponse = await page.request.get(
    `/api/projects/${pid}/blobs/${unchangedMedia.blob}`,
  );
  expect(unchangedBlobResponse.ok()).toBeTruthy();
  expect(await unchangedBlobResponse.body()).toEqual(originalBytes);

  if (process.env.FRISKET_E2E_LIVE_SEARCH_PREPARATION === '1') {
    let activeReplacement = replacement;
    if (activeReplacement.status === 'planning') {
      await expect.poll(async () => {
        activeReplacement = await preparationBatch(page.request, pid, replacement.id);
        return activeReplacement.status;
      }, {
        message: 'forced OCR preparation did not finish planning',
        timeout: 15_000,
      }).not.toBe('planning');
    }
    if (activeReplacement.status === 'needs_consent') {
      const consent = page.getByTestId('search-preparation-model-consent');
      await expect(consent.getByTestId('search-preparation-model-read_text')).toBeVisible();
      const requiredRefs = activeReplacement.models
        .filter((model) => model.required && !model.ready)
        .map((model) => model.setup_ref);
      const decision = page.waitForResponse((response) => (
        response.request().method() === 'POST'
        && new URL(response.url()).pathname === (
          `/api/projects/${pid}/search-preparation/${replacement.id}/decision`
        )
      ));
      await consent.getByTestId('search-preparation-decision-prepare').click();
      const decisionResult = await decision;
      expect(decisionResult.ok()).toBeTruthy();
      expect(decisionResult.request().postDataJSON()).toEqual({
        plan_hash: activeReplacement.plan_hash,
        decision: 'prepare',
        selected_setup_refs: requiredRefs,
      });
    }

    const completedOcr = await waitForPreparation(
      page.request,
      pid,
      replacement.id,
      'complete',
      120_000,
    );
    expect(completedOcr.jobs.length).toBeGreaterThan(0);
    const postOcrData = await sheetData(page.request, pid, imported.sheet_id);
    expect(postOcrData.rows[0].cells[String(mediaColumn!.id)]).toEqual(originalMedia);
    const postOcrBlob = await page.request.get(
      `/api/projects/${pid}/blobs/${originalMedia.blob}`,
    );
    expect(postOcrBlob.ok()).toBeTruthy();
    expect(await postOcrBlob.body()).toEqual(originalBytes);

    await page.getByTestId('search-preparation-done').click();
    await page.getByTestId('bottom-dock-tab-jobs').click();
    for (const job of completedOcr.jobs) {
      await expect(page.getByTestId(`bottom-dock-job-${job.job_id}`)).toContainText(
        '(automatic)',
      );
    }
    await page.reload();
    await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
    await page.getByTestId('bottom-dock-tab-jobs').click();
    await expect(page.getByTestId(`bottom-dock-job-${completedOcr.jobs[0].job_id}`)).toContainText(
      '(automatic)',
    );
    const palette = await openPalette(page);
    await palette.getByTestId('search-mode-select').selectOption('lexical');
    const staleSearch = page.waitForResponse((response) => {
      if (response.request().method() !== 'POST') return false;
      if (new URL(response.url()).pathname !== `/api/projects/${pid}/search`) return false;
      const body = response.request().postDataJSON() as {
        query?: { kind?: string; text?: string };
      };
      return body.query?.kind === 'text' && body.query.text === 'meaningless existing OCR';
    });
    await palette.getByTestId('command-palette-input').fill('meaningless existing OCR');
    const staleResult = await staleSearch;
    expect(staleResult.ok()).toBeTruthy();
    const staleBody = await staleResult.json() as SearchResponse;
    expect(staleBody.groups.some((group) => (
      group.primary.snippet.replaceAll('<b>', '').replaceAll('</b>', '')
        .includes('meaningless existing OCR')
    ))).toBeFalsy();

    const correctedSearch = page.waitForResponse((response) => {
      if (response.request().method() !== 'POST') return false;
      if (new URL(response.url()).pathname !== `/api/projects/${pid}/search`) return false;
      const body = response.request().postDataJSON() as {
        query?: { kind?: string; text?: string };
      };
      return body.query?.kind === 'text' && body.query.text === 'turquoise lighthouse';
    });
    await palette.getByTestId('command-palette-input').fill('turquoise lighthouse');
    expect((await correctedSearch).ok()).toBeTruthy();
    const correctedHit = palette.getByTestId('search-hit').filter({
      hasText: 'turquoise lighthouse',
    }).first();
    await expect(correctedHit).toBeVisible({ timeout: 20_000 });
    await correctedHit.click();
    await expect(page.getByTestId('document-view')).toBeVisible();
    await expect(page.getByTestId('document-page-indicator')).toContainText('1 / 1');
  }
});
