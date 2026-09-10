import { readFileSync } from 'node:fs';
import path from 'node:path';

import { expect, test, type Page, type Route } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from './helpers';

// Transcribe Compare exposes the same per-engine options treatment as OCR
// Compare. Each engine chip carries a ⚙ that opens the SAME
// `ConfigureVariantPopover` component OcrCompareTab's v2 uses
// (web/src/workbench/MediaCompareShell.tsx — see its doc comment), NOT a new
// one-off popover. This spec pins: (1) the popover really is the shared
// component (same class as OCR's, same generic testids), (2) per-engine
// option fields (faster_whisper: language + model size + VAD; parakeet-tdt: VAD
// only — grounded in src/frisket/ops/transcribe.py's per-engine spec reads),
// (3) editing an option carries the new param onto the backend seam
// (model_size/vad) on the NEXT requested run — configuring no longer re-runs by
// itself, (4) Duplicate variant compares two configs of the same engine,
// (5) the gear CAN swap a chip onto a billable engine, with the cost gate
// standing in front of it — compare is no longer the free-only surface. The
// add-engine MENU flow (workbench-transcribe-compare.spec.ts) is UNCHANGED by
// this feature — only the per-chip gear/popover is new.

const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const AUDIO_FIXTURE = readFileSync(path.join(MEDIA_DIR, 'tiny-audio.wav'));

async function patchTranscribeCatalog(page: Page) {
  await page.route('**/actions/v1/catalog', async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    const transcribe = (body.actions ?? []).find(
      (action: { kind?: string }) => action.kind === 'media.transcribe',
    );
    if (transcribe) {
      transcribe.ui_hints = transcribe.ui_hints ?? {};
      transcribe.ui_hints.engines = [
        {
          id: 'faster_whisper',
          label: 'Whisper (local)',
          tier: 'local',
          available: true,
          transcription_options: {
            language: true, vad: true, model_size: true,
          },
          language: {
            mode: 'single',
            default: 'auto',
            detects: true,
            choices: [
              { value: 'en', label: 'English' },
              { value: 'fr', label: 'French' },
              { value: 'de', label: 'German' },
              { value: 'es', label: 'Spanish' },
            ],
          },
          diarization: { supported: false, mode: 'none' },
        },
        {
          id: 'parakeet-tdt',
          label: 'Parakeet (local)',
          tier: 'local',
          available: true,
          transcription_options: {
            language: false, vad: true, model_size: false,
          },
          diarization: { supported: false, mode: 'none' },
        },
        {
          id: 'stubprovider/whisper-1',
          label: 'Stub remote transcription',
          tier: 'hosted',
          billable: true,
          available: true,
          transcription_options: {
            language: true, vad: false, model_size: false,
          },
          language: {
            mode: 'single',
            default: 'auto',
            detects: true,
            choices: [
              { value: 'en', label: 'English' },
              { value: 'es', label: 'Spanish' },
            ],
          },
          diarization: { supported: false, mode: 'none' },
        },
        {
          id: 'joint_diarizer_fixture',
          label: 'Synthetic joint diarizer',
          tier: 'sidecar',
          available: true,
          transcription_options: {
            language: false, vad: false, model_size: false,
          },
          diarization: { supported: true, mode: 'intrinsic', speaker_hint: 'none' },
        },
      ];
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
}

// Each candidate is quoted and then run as its own preview job, so the option
// under test appears on BOTH legs and the transcript comes back keyed to the
// engine that started that job.
const REMOTE_ENGINE = 'stubprovider/whisper-1';

function comparePayload(route: Route): Record<string, unknown> {
  const postData = route.request().postData() ?? '';
  const match = postData.match(/name="payload"\r?\n\r?\n([\s\S]*?)\r?\n--/);
  return match ? JSON.parse(match[1]) : {};
}

/** Wire the quote, the start and the poll. `starts` is what the option tests
 * assert against: it holds one payload per candidate actually run. */
async function routeComparison(page: Page) {
  const estimates: Array<Record<string, unknown>> = [];
  const starts: Array<Record<string, unknown>> = [];
  const previews = new Map<string, Record<string, unknown>>();

  await page.route(/\/transcribe\/compare-scratch\/estimate$/, async (route) => {
    const payload = comparePayload(route);
    estimates.push(payload);
    const billable = payload.engine === REMOTE_ENGINE;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.transcribe_compare_estimate.v1',
        source: { scratch: true, filename: 'clip', mime: 'audio/wav', size: 512 },
        estimate: {
          rows: 1,
          cost: billable ? 0.42 : 0,
          cost_source: billable ? 'estimated' : 'free',
          billed_cost: billable ? 420_000 : 0,
          policy_id: 'frisket.pricing.identity.v1',
          requires_confirmation: billable,
          ...(billable ? { promise_set_hash: 'promise-remote' } : {}),
          claims: billable ? [{ field: 'cost', display: '$0.42' }] : [],
        },
      }),
    });
  });

  await page.route(/\/transcribe\/compare-scratch$/, async (route) => {
    const payload = comparePayload(route);
    starts.push(payload);
    const previewId = `preview-${starts.length}`;
    previews.set(previewId, payload);
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_preview.v1',
        preview_id: previewId,
        total: 1,
      }),
    });
  });

  await page.route('**/actions/v1/preview/*', async (route) => {
    const previewId = route.request().url().split('/').pop() ?? '';
    const payload = previews.get(previewId) ?? {};
    const engine = String(payload.engine ?? '');
    // The transcript carries the model size so a re-run with a changed option
    // is visible in the rendered column, not just on the wire.
    const text = `${engine} text ${payload.model_size ?? ''}`.trim();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_preview_status.v1',
        preview_id: previewId,
        status: 'done',
        progress: { done: 1, total: 1 },
        accounting: { elapsed_ms: 9 },
        result: {
          kind: 'table',
          columns: [
            { name: 'text', column_type: 'text' },
            { name: 'segments', column_type: 'json' },
            { name: 'detected_language', column_type: 'text' },
          ],
          sampled: 1,
          total: 1,
          rows: [
            {
              text: { value: text },
              segments: {
                value: [{ segment_index: 0, start: 0, end: 1, text: `${engine} text` }],
              },
              detected_language: { value: 'en' },
            },
          ],
          warnings: [],
        },
      }),
    });
  });

  return { estimates, starts };
}

/** The gate is a typed confirmation, not a single click: Run it stays disabled
 * until the operator writes the word, which is the consent step itself. */
async function approveCostGate(page: Page) {
  const confirm = page.getByTestId('cost-gate-confirm');
  await expect(confirm).toBeDisabled();
  await page.getByTestId('cost-gate-input').fill('confirm');
  await expect(confirm).toBeEnabled();
  await confirm.click();
}

async function runComparison(page: Page) {
  await page.getByTestId('transcribe-compare-run').click();
}

async function openTranscribeCompareWithAudio(page: Page) {
  await page.getByTestId('ribbon-tab-media').click();
  await page.getByTestId('ribbon-command-transcribe-compare').click();
  await expect(page.getByTestId('transcribe-compare-tab')).toBeVisible();
  await page.getByTestId('transcribe-compare-file-input').setInputFiles({
    name: 'panel-audio.wav',
    mimeType: 'audio/wav',
    buffer: AUDIO_FIXTURE,
  });
  // Both default variants are seeded from the catalog asynchronously. Run
  // before the second one lands and only one candidate is ever quoted, which
  // is what made the first two tests in this file see a single column.
  await expect(page.getByTestId('transcribe-compare-engine-column')).toHaveCount(2);
  await runComparison(page);
  // Wait for the transcript itself, not the chip: a later starts-count
  // assertion must not race the run this helper just asked for. The mocked
  // transcript is `<engine> text [model size]`.
  await expect(page.getByTestId('transcribe-compare-engine-column').first()).toContainText(
    'text',
  );
}

test('the gear opens the SAME Configure-variant popover component OCR Compare uses (reuse pin, not a one-off)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tco-reuse'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompareWithAudio(page);

  const chip = page
    .locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="faster_whisper"]')
    .first();
  await chip.getByTestId('transcribe-compare-variant-gear').click();

  const popover = page.getByTestId('transcribe-compare-configure');
  await expect(popover).toBeVisible();
  // Structural component-reuse proof: the shared popover renders the SAME
  // `.ocr-compare-configure` class OCR's popover uses (MediaCompareShell.tsx
  // ConfigureVariantPopover), not a second visual language.
  await expect(popover).toHaveClass(/ocr-compare-configure/);
  await expect(popover.getByTestId('transcribe-compare-configure-engine')).toBeVisible();
  await expect(popover.getByTestId('transcribe-compare-duplicate')).toBeVisible();
});

test('faster_whisper exposes language + model size + VAD; parakeet-tdt exposes only VAD (per-engine declared fields)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tco-fields'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompareWithAudio(page);

  const whisperChip = page
    .locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="faster_whisper"]')
    .first();
  await whisperChip.getByTestId('transcribe-compare-variant-gear').click();
  let popover = page.getByTestId('transcribe-compare-configure');
  await expect(popover.getByTestId('transcribe-compare-configure-language')).toBeVisible();
  await expect(popover.getByTestId('transcribe-compare-model-pill-large-v3')).toBeVisible();
  await expect(popover.getByTestId('transcribe-compare-configure-vad')).toBeVisible();
  await whisperChip.getByTestId('transcribe-compare-variant-gear').click(); // close

  const parakeetChip = page
    .locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="parakeet-tdt"]')
    .first();
  await parakeetChip.getByTestId('transcribe-compare-variant-gear').click();
  popover = page.getByTestId('transcribe-compare-configure');
  await expect(popover).toBeVisible();
  await expect(popover.getByTestId('transcribe-compare-configure-language')).toHaveCount(0);
  await expect(popover.getByTestId('transcribe-compare-model-pills')).toHaveCount(0);
  await expect(popover.getByTestId('transcribe-compare-configure-vad')).toBeVisible();
});

test('language choices and wire codes come from the selected engine declaration', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('tco-language'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  const { starts } = await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompareWithAudio(page);

  const whisperChip = page
    .locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="faster_whisper"]')
    .first();
  await whisperChip.getByTestId('transcribe-compare-variant-gear').click();
  const language = page.getByTestId('transcribe-compare-configure-language');
  await expect(language.locator('option')).toHaveCount(5);
  expect(await language.locator('option').evaluateAll((options) =>
    options.map((option) => (option as HTMLOptionElement).value),
  )).toEqual(['', 'en', 'fr', 'de', 'es']);

  const before = starts.length;
  await language.selectOption('es');
  // Configuring does not spend: the changed variant waits to be asked for.
  expect(starts.length).toBe(before);

  await runComparison(page);
  await expect.poll(() => starts.length).toBeGreaterThan(before);
  const whisperCalls = starts.filter((call) => call.engine === 'faster_whisper');
  expect(whisperCalls[whisperCalls.length - 1].language).toBe('es');
});

test('choosing a model size re-runs the variant with model_size on the scratch payload; the chip summary reflects it', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tco-modelsize'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  const { starts } = await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompareWithAudio(page);

  const whisperChip = page
    .locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="faster_whisper"]')
    .first();
  await whisperChip.getByTestId('transcribe-compare-variant-gear').click();
  const popover = page.getByTestId('transcribe-compare-configure');
  const before = starts.length;
  await popover.getByTestId('transcribe-compare-model-pill-large-v3').click();
  expect(starts.length).toBe(before);

  await runComparison(page);
  await expect.poll(() => starts.length).toBeGreaterThan(before);
  const whisperCalls = starts.filter((call) => call.engine === 'faster_whisper');
  expect(whisperCalls[whisperCalls.length - 1].model_size).toBe('large-v3');

  await expect(whisperChip.locator('.ocr-compare-variant-summary')).toContainText('large-v3');
});

test('an intrinsic engine shows always-on diarization and sends no unsupported Whisper defaults', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tco-intrinsic'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  const { starts } = await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompareWithAudio(page);

  const parakeetChip = page
    .locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="parakeet-tdt"]')
    .first();
  await parakeetChip.getByTestId('transcribe-compare-variant-gear').click();
  await page
    .getByTestId('transcribe-compare-configure-engine')
    .selectOption('joint_diarizer_fixture');

  const popover = page.getByTestId('transcribe-compare-configure');
  await expect(popover.getByTestId('transcribe-compare-diarization-intrinsic')).toContainText(
    'Speaker identification is always on',
  );
  await expect(popover.getByTestId('transcribe-compare-configure-language')).toHaveCount(0);
  await expect(popover.getByTestId('transcribe-compare-model-pills')).toHaveCount(0);
  await expect(popover.getByTestId('transcribe-compare-configure-vad')).toHaveCount(0);

  await runComparison(page);
  await expect
    .poll(() => starts.filter((call) => call.engine === 'joint_diarizer_fixture').length)
    .toBeGreaterThan(0);
  const intrinsicCalls = starts.filter((call) => call.engine === 'joint_diarizer_fixture');
  const payload = intrinsicCalls[intrinsicCalls.length - 1];
  expect(payload).not.toHaveProperty('language');
  expect(payload).not.toHaveProperty('model_size');
  expect(payload).not.toHaveProperty('vad');
  expect(payload).not.toHaveProperty('diarize');
});

test('Duplicate variant compares two model sizes of the same engine side by side', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('tco-duplicate'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompareWithAudio(page);

  const whisperChip = page
    .locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="faster_whisper"]')
    .first();
  await whisperChip.getByTestId('transcribe-compare-variant-gear').click();
  await page.getByTestId('transcribe-compare-configure').getByTestId('transcribe-compare-duplicate').click();

  // The duplicate's popover reopens automatically; both faster_whisper chips
  // now coexist (three chips total: whisper original, whisper copy, parakeet-tdt).
  await expect(
    page.locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="faster_whisper"]'),
  ).toHaveCount(2);
  const copyPopover = page.getByTestId('transcribe-compare-configure');
  await copyPopover.getByTestId('transcribe-compare-model-pill-large-v3').click();

  const chips = page.locator(
    '[data-testid="transcribe-compare-engine-chip"][data-engine-id="faster_whisper"]',
  );
  await expect(chips.nth(1).locator('.ocr-compare-variant-summary')).toContainText('large-v3');
});

// The whole "stranded un-allowed remote engine" class of race is gone with the
// gate that created it: the gear's engine <select> lists only free engines, so
// a swap always lands on something immediately runnable and there is nothing to
// restore on Cancel. What is still worth pinning is that the billable engine
// cannot be reached from the gear at all.
test('the gear can swap a chip onto a billable engine, and the gate stands in front of it', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tco-billable'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  const { estimates, starts } = await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompareWithAudio(page);
  const runsBefore = starts.length;

  const parakeetChip = page
    .locator('[data-testid="transcribe-compare-engine-chip"][data-engine-id="parakeet-tdt"]')
    .first();
  await parakeetChip.getByTestId('transcribe-compare-variant-gear').click();
  const engineSelect = page
    .getByTestId('transcribe-compare-configure')
    .getByTestId('transcribe-compare-configure-engine');
  await expect(engineSelect).toBeVisible();
  // Compare is no longer the free-only surface: the paid engine is spellable
  // here, and consent is what guards it.
  await expect(
    engineSelect.locator(`option[value="${REMOTE_ENGINE}"]`),
  ).toHaveCount(1);
  await engineSelect.selectOption(REMOTE_ENGINE);
  await page.keyboard.press('Escape');

  await expect(
    page.locator(`[data-testid="transcribe-compare-engine-chip"][data-engine-id="${REMOTE_ENGINE}"]`),
  ).toHaveCount(1);
  // Swapping alone spends nothing.
  expect(starts.length).toBe(runsBefore);

  await runComparison(page);
  await expect
    .poll(() => estimates.filter((call) => call.engine === REMOTE_ENGINE).length)
    .toBe(1);
  const gate = page.getByTestId('cost-gate-modal');
  await expect(gate).toBeVisible();
  expect(starts.filter((call) => call.engine === REMOTE_ENGINE)).toEqual([]);

  await approveCostGate(page);
  await expect
    .poll(() => starts.filter((call) => call.engine === REMOTE_ENGINE).length)
    .toBe(1);
  const paid = starts.filter((call) => call.engine === REMOTE_ENGINE);
  expect(paid[0].confirmation).toBe('promise-remote');
});
