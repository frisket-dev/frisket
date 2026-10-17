import { readFileSync } from 'node:fs';
import path from 'node:path';

import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
} from '@playwright/test';

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

type ImportSessionStatus = {
  import_ref: string;
  state: 'admitting' | 'running' | 'paused' | 'cancelling' | 'cancelled' | 'completed' | 'kept' | 'removed';
  sealed: boolean;
  sheet_id: number | null;
  committed_rows: number;
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
    can_setup: boolean;
    pull_id: number | null;
  }>;
  jobs: Array<{ job_id: number; status: string; action_kind: string | null }>;
  errors: string[];
};

type SearchResponse = {
  groups: Array<{
    primary: {
      snippet: string;
      binding: { sheet_id: number; row_id: number; column_id: number | null };
      navigation:
        | { kind: 'document'; sheet_id: number; row_id: number; column_id: number; page: number | null }
        | { kind: 'evidence'; evidence_link_id: string; span_id: string }
        | { kind: 'cell'; sheet_id: number; row_id: number; column_id: number | null };
    };
  }>;
};

type SearchHit = SearchResponse['groups'][number]['primary'];

async function expectSearchHitOpensPage(
  page: Page,
  projectId: string,
  hit: SearchHit,
  hitControl: Locator,
  expected: { sheetId: number; page: number; pageCount: number },
): Promise<void> {
  expect(hit.binding.sheet_id).toBe(expected.sheetId);
  const navigation = hit.navigation;
  if (navigation.kind === 'evidence') {
    const evidence = await page.request.get(
      `/api/projects/${projectId}/evidence/links/${encodeURIComponent(navigation.evidence_link_id)}/viewer`,
    );
    expect(evidence.ok()).toBeTruthy();
    const payload = await evidence.json() as {
      artifacts: Array<{
        source_cell: { sheet_id: number; row_id: number; column_id: number } | null;
        spans: Array<{
          stable_id: string;
          selector: { page_start?: number; page_end?: number };
        }>;
      }>;
    };
    const artifact = payload.artifacts.find((candidate) => (
      candidate.spans.some((span) => span.stable_id === navigation.span_id)
    ));
    expect(artifact?.source_cell?.sheet_id).toBe(expected.sheetId);
    const citedSpan = artifact?.spans.find((span) => span.stable_id === navigation.span_id);
    expect(citedSpan?.selector).toMatchObject({
      page_start: expected.page,
      page_end: expected.page,
    });

    await hitControl.click();
    const viewer = page.getByTestId('evidence-viewer');
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId('evidence-pdf')).toBeVisible();
    await expect(viewer.getByTestId('document-pdf')).toHaveAttribute(
      'data-page-count',
      String(expected.pageCount),
    );
    await expect(
      viewer.locator(
        `[data-testid="document-pdf-page"][data-page="${expected.page}"]`,
      ),
    ).toBeInViewport();
    return;
  }

  expect(navigation).toMatchObject({
    kind: 'document',
    sheet_id: expected.sheetId,
    page: expected.page,
  });
  await hitControl.click();
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('document-page-indicator')).toContainText(
    `${expected.page} / ${expected.pageCount}`,
  );
}

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

async function preparationBatches(
  request: APIRequestContext,
  projectId: string,
): Promise<PreparationBatch[]> {
  const response = await request.get(`/api/projects/${projectId}/search-preparation`);
  expect(response.ok()).toBeTruthy();
  const body = await response.json() as { batches: PreparationBatch[] };
  return body.batches;
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

async function importFilesThroughSession(
  page: Page,
  projectId: string,
  files: Array<{ name: string; mimeType: string; buffer: Buffer }>,
): Promise<ImportSessionStatus> {
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-files').click();
  const created = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${projectId}/import/files/sessions`
  ));
  await page.getByTestId('import-file-input').setInputFiles(files);
  const response = await created;
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function reviewImportedFiles(page: Page): Promise<Locator> {
  await expect(page.getByTestId('import-files-summary')).toBeVisible();
  await expect(page.getByTestId('search-preparation-receipt')).toHaveCount(0);
  await page.getByTestId('import-files-next').click();
  const receipt = page.getByTestId('search-preparation-receipt');
  await expect(receipt).toBeVisible();
  return receipt;
}

test('closing missing transcription setup starts no pull and remains recoverable from Search', async ({
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
    !missingTranscription || !missingTranscription.can_setup,
    'This consent-boundary case requires the standard transcription runtime with Parakeet absent from the model cache.',
  );
  const receipt = await reviewImportedFiles(page);
  const consent = receipt.getByTestId('search-preparation-model-consent');
  await expect(consent).toBeVisible();
  await expect(consent.getByTestId('search-preparation-model-transcribe')).toContainText(
    /Parakeet|transcrib/i,
  );
  const missingOptionalModel = planned!.models.some((model) => !model.required && !model.ready);
  if (missingOptionalModel) {
    const semantic = consent.getByTestId('search-preparation-semantic-model');
    if (planned!.models.some((model) => !model.required && !model.ready && model.can_setup)) {
      await expect(semantic).toBeChecked();
    } else {
      await expect(semantic).not.toBeChecked();
      await expect(semantic).toBeDisabled();
    }
  } else {
    await expect(consent.getByTestId('search-preparation-semantic-model')).toHaveCount(0);
  }
  expect(setupRequests).toEqual([]);
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);

  await page.getByTestId('import-workspace-close').click();
  await expect(receipt).toBeHidden();
  const deferred = await preparationBatch(page.request, pid, imported.search_preparation_batch_id!);
  expect(deferred.status).toBe('needs_consent');
  expect(deferred.jobs).toEqual([]);
  expect(deferred.models.every((model) => model.pull_id === null)).toBeTruthy();
  expect(setupRequests).toEqual([]);
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);

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

test('turning automatic preparation off leaves an imported PDF untouched until Search prepares it', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('make-searchable-setting-off'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  const settings = await page.request.patch(`/api/projects/${pid}/settings`, {
    data: {
      retrieval: { prepare_new_files_automatically: false },
    },
  });
  expect(settings.ok()).toBeTruthy();
  expect(await settings.json()).toMatchObject({
    retrieval: { prepare_new_files_automatically: false },
  });
  const batchesBefore = await preparationBatches(page.request, pid);
  const originalPdf = textPdf([
    ['Deferred search preparation', 'The amberquay permit remains available in the original PDF.'],
  ], { fontSize: 20, startX: 54, startY: 720, leading: 28 });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  const imported = await importFilesFromWorkspace(page, pid, [{
    name: 'deferred-permit.pdf',
    mimeType: 'application/pdf',
    buffer: originalPdf,
  }]);

  expect(imported.search_preparation_batch_id ?? null).toBeNull();
  await expect(page.getByTestId('import-workspace')).toHaveCount(0);
  await expect(page.getByTestId('search-preparation-receipt')).toHaveCount(0);
  expect(await preparationBatches(page.request, pid)).toEqual(batchesBefore);

  const importedSheet = await sheetData(page.request, pid, imported.sheet_id);
  const mediaColumn = importedSheet.columns.find((column) => column.name === 'media');
  expect(mediaColumn).toBeTruthy();
  const media = importedSheet.rows[0].cells[String(mediaColumn!.id)] as { blob: string };
  const storedPdf = await page.request.get(`/api/projects/${pid}/blobs/${media.blob}`);
  expect(storedPdf.ok()).toBeTruthy();
  expect(await storedPdf.body()).toEqual(originalPdf);

  const palette = await openPalette(page);
  await palette.getByTestId('search-mode-select').selectOption('lexical');
  await palette.getByTestId('command-palette-input').fill('amberquay');
  const gap = palette.getByTestId('search-preparation-gap');
  await expect(gap).toContainText(/1 current media item needs search preparation/i);
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
  const explicitBatch = await explicitResponse.json() as PreparationBatch;
  expect(explicitBatch.sheet_id).toBe(imported.sheet_id);
  expect((await preparationBatches(page.request, pid)).map((batch) => batch.id)).toContain(
    explicitBatch.id,
  );
  await expect(page.getByTestId('search-preparation-receipt')).toBeVisible();
});

test('a resumable file import keeps its preparation receipt in the import flow and clears the recovery bar', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('make-searchable-resumable'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const created = await importFilesThroughSession(page, pid, [
    {
      name: 'resumable-one.pdf',
      mimeType: 'application/pdf',
      buffer: textPdf([['First resumable PDF', 'The north annex note.']]),
    },
    {
      name: 'resumable-two.pdf',
      mimeType: 'application/pdf',
      buffer: textPdf([['Second resumable PDF', 'The south annex note.']]),
    },
  ]);

  let terminal: ImportSessionStatus | null = null;
  await expect.poll(async () => {
    const response = await page.request.get(
      `/api/projects/${pid}/import/files/sessions/${created.import_ref}`,
    );
    expect(response.ok()).toBeTruthy();
    terminal = await response.json() as ImportSessionStatus;
    if (terminal.state === 'completed') {
      expect(terminal.search_preparation_batch_id).toBeTruthy();
    }
    return terminal.state;
  }, {
    message: 'resumable import did not reach a terminal state with its preparation receipt',
    timeout: 30_000,
    intervals: [100, 250, 500, 1_000],
  }).toBe('completed');
  expect(terminal!.sealed).toBeTruthy();
  expect(terminal!.committed_rows).toBe(2);
  expect(terminal!.sheet_id).toBeGreaterThan(0);
  expect(terminal!.search_preparation_batch_id).toBeTruthy();

  const receipt = await reviewImportedFiles(page);
  await expect(receipt.getByTestId('search-preparation-text-preview')).toBeVisible();
  await page.getByTestId('import-workspace-close').click();
  const unadmitted = await preparationBatch(page.request, pid, terminal!.search_preparation_batch_id!);
  expect(unadmitted.status).toBe('needs_consent');
  expect(unadmitted.jobs).toEqual([]);
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('import-progress-completed')).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Import progress' })).toHaveCount(0);
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
  const receipt = await reviewImportedFiles(page);
  await expect(receipt.getByTestId('search-preparation-model-consent')).toBeVisible();
  await expect(receipt.getByTestId('search-preparation-group-native_pdf'))
    .toHaveAttribute('data-status', 'needs_consent');
  const planned = await preparationBatch(page.request, pid, batchId);
  expect(planned.status).toBe('needs_consent');
  expect(planned.jobs).toEqual([]);
  const semantic = receipt.getByTestId('search-preparation-semantic-model');
  if (await semantic.count()) await semantic.uncheck();
  const decision = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${pid}/search-preparation/${batchId}/decision`
  ));
  await receipt.getByTestId('search-preparation-decision-prepare').click();
  const decisionResult = await decision;
  expect(decisionResult.ok()).toBeTruthy();
  expect(decisionResult.request().postDataJSON()).toEqual({
    plan_hash: planned.plan_hash,
    decision: 'prepare',
    selected_setup_refs: [],
  });

  const completed = await waitForPreparation(page.request, pid, batchId, 'complete');
  expect(completed.groups).toContainEqual({
    kind: 'native_pdf',
    count: 1,
    status: 'complete',
  });
  expect(completed.models.filter((model) => model.required)).toEqual([]);
  expect(setupRequests).toEqual([]);
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);

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

  const visibleHit = palette.getByTestId('search-hit').filter({ hasText: 'bluewharf' }).first();
  await expect(visibleHit).toBeVisible();
  await expectSearchHitOpensPage(page, pid, hit!, visibleHit, {
    sheetId: imported.sheet_id,
    page: 2,
    pageCount: 2,
  });
});

test('native text review requests replacement OCR without changing the original PDF', async ({
  page,
}) => {
  if (process.env.FRISKET_E2E_LIVE_SEARCH_PREPARATION === '1') {
    test.setTimeout(180_000);
  }
  const pid = await createProject(page.request, uniqueName('make-searchable-check-text'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  const pullsBefore = await modelPullInventory(page.request);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const imported = await importFilesFromWorkspace(page, pid, [{
    name: 'questionable-text.pdf',
    mimeType: 'application/pdf',
    buffer: readFileSync(path.join(MAKE_SEARCHABLE_FIXTURES, 'bad-text-layer.pdf')),
  }]);
  expect(imported.search_preparation_batch_id).toBeTruthy();
  const receipt = await reviewImportedFiles(page);
  const initialPlan = await preparationBatch(page.request, pid, imported.search_preparation_batch_id!);
  expect(initialPlan.status).toBe('needs_consent');
  expect(initialPlan.jobs).toEqual([]);

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

  const preview = page.getByTestId('search-preparation-text-preview');
  await expect(preview).toBeVisible();
  await expect(preview).toContainText('Does this text look right?');
  await expect(preview).toContainText('garb1ed XXv0 vvv meaningless existing OCR');
  await expect(preview.getByTestId('search-preparation-force-ocr')).toBeVisible();

  const replacementRequest = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === (
      `/api/projects/${pid}/search-preparation/${initialPlan.id}/replan`
    )
  ));
  await preview.getByTestId('search-preparation-force-ocr').click();
  const replacementResponse = await replacementRequest;
  expect(replacementResponse.ok()).toBeTruthy();
  expect(replacementResponse.request().postDataJSON()).toEqual({
    plan_hash: initialPlan.plan_hash,
    force_ocr: true,
  });
  const replacement = await replacementResponse.json() as PreparationBatch;
  expect(replacement.id).not.toBe(initialPlan.id);
  expect((await preparationBatch(page.request, pid, initialPlan.id)).status).toBe('stopped');
  expect(replacement).toMatchObject({
    sheet_id: imported.sheet_id,
    status: 'needs_consent',
    groups: [{ kind: 'ocr', count: 1 }],
    jobs: [],
  });
  const samplesResponse = await page.request.get(
    `/api/projects/${pid}/search-preparation/${replacement.id}/text-samples`,
  );
  expect(samplesResponse.ok()).toBeTruthy();
  const samples = await samplesResponse.json() as {
    batch_id: string;
    samples: Array<{ row_id: number; column_id: number }>;
  };
  expect(samples.batch_id).toBe(replacement.id);
  expect(samples.samples.map(({ row_id, column_id }) => ({ row_id, column_id })))
    .toEqual([{ row_id: before.rows[0].id, column_id: mediaColumn!.id }]);
  await expect(receipt.getByTestId('search-preparation-force-ocr')).toBeChecked();
  expect(await modelPullInventory(page.request)).toEqual(pullsBefore);

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
      if (activeReplacement.models.some((model) => model.required && !model.ready)) {
        await expect(consent.getByTestId('search-preparation-model-read_text')).toBeVisible();
      }
      const requiredRefs = activeReplacement.models
        .filter((model) => model.required && !model.ready && model.can_setup)
        .map((model) => model.setup_ref);
      const hasInstallableOptionalModel = activeReplacement.models.some((model) => (
        !model.required && !model.ready && model.can_setup
      ));
      if (hasInstallableOptionalModel) {
        const semantic = consent.getByTestId('search-preparation-semantic-model');
        await expect(semantic).toBeChecked();
        await semantic.uncheck();
        await expect(semantic).not.toBeChecked();
      }
      const decision = page.waitForResponse((response) => (
        response.request().method() === 'POST'
        && new URL(response.url()).pathname === (
          `/api/projects/${pid}/search-preparation/${replacement.id}/decision`
        )
      ));
      await page.getByTestId('search-preparation-receipt')
        .getByTestId('search-preparation-decision-prepare')
        .click();
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

    await expect(receipt).toBeHidden();
    await page.getByTestId('bottom-dock-tab-jobs').click();
    for (const job of completedOcr.jobs) {
      await expect(page.getByTestId(`bottom-dock-job-${job.job_id}`)).toContainText(
        '(search preparation)',
      );
    }
    await page.reload();
    await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
    await page.getByTestId('bottom-dock-tab-jobs').click();
    await expect(page.getByTestId(`bottom-dock-job-${completedOcr.jobs[0].job_id}`)).toContainText(
      '(search preparation)',
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
    const correctedResult = await correctedSearch;
    expect(correctedResult.ok()).toBeTruthy();
    const correctedHit = palette.getByTestId('search-hit').filter({
      hasText: 'turquoise lighthouse',
    }).first();
    await expect(correctedHit).toBeVisible({ timeout: 20_000 });
    const correctedBody = await correctedResult.json() as SearchResponse;
    const corrected = correctedBody.groups.find((group) => (
      group.primary.snippet.replaceAll('<b>', '').replaceAll('</b>', '')
        .includes('turquoise lighthouse')
    ))?.primary;
    expect(corrected).toBeTruthy();
    await expectSearchHitOpensPage(page, pid, corrected!, correctedHit, {
      sheetId: imported.sheet_id,
      page: 1,
      pageCount: 1,
    });
  }
});
