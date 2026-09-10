import { readFileSync } from 'node:fs';
import path from 'node:path';

import { expect, test, type Page, type Route } from '@playwright/test';
import {
  createProject,
  importCsv,
  listSheets,
  openAction,
  openProject,
  sheetData,
  uniqueName,
} from './helpers';

// Transcription Compare bake-off — a drop-in SCRATCH center tab built on the
// genericized MediaCompareShell (extracted from the OCR Compare tab). Accepts
// audio AND video.
//
// Uploaded clips are now QUOTED preview jobs rather than immediate free calls:
// dropping or configuring a clip spends nothing, the operator clicks run, every
// candidate is estimated first, the paid ones are approved in ONE cost gate,
// and each then runs as an ordinary preview polled to completion. The whole
// catalog is offered, so a billable engine is reachable here and consent — not
// omission — is what guards it.
//
// Every leg is intercepted, so nothing reaches a provider. The seeded
// per-engine disagreement drives the symmetric amber word/char diff; the
// stubbed timestamped segments drive the diff-token → player SEEK.

const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const AUDIO_FIXTURE = readFileSync(path.join(MEDIA_DIR, 'tiny-audio.wav'));
const VIDEO_FIXTURE = readFileSync(path.join(MEDIA_DIR, 'tiny-video.mp4'));

// Three LOCAL engines so the Survey (n≥3) flip has a third non-remote engine,
// plus a remote engine that exercises the cost gate ONLY (every scratch call is
// intercepted, so nothing ever reaches a provider).
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
        { id: 'faster_whisper', label: 'Whisper (local)', tier: 'local', available: true },
        { id: 'parakeet-tdt', label: 'Parakeet (local)', tier: 'local', available: true },
        { id: 'vosk', label: 'Vosk (local)', tier: 'local', available: true },
        {
          id: 'stubprovider/whisper-1',
          label: 'Stub remote transcription',
          tier: 'hosted',
          billable: true,
          available: true,
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

// The seeded disagreement lives in segment index 1 (start 1.0s): whisper reads
// MERIDIAN, parakeet-tdt reads MERLDIAN — exactly one token differs, at the
// character level. vosk agrees with whisper. Segment 0 is identical.
const ENGINE_SEGMENTS: Record<string, Array<{ start: number; end: number; text: string }>> = {
  faster_whisper: [
    { start: 0.0, end: 0.9, text: 'PANEL DISCUSSION OPENING' },
    { start: 1.0, end: 1.9, text: 'THE MERIDIAN REPORT' },
  ],
  'parakeet-tdt': [
    { start: 0.0, end: 0.9, text: 'PANEL DISCUSSION OPENING' },
    { start: 1.0, end: 1.9, text: 'THE MERLDIAN REPORT' },
  ],
  vosk: [
    { start: 0.0, end: 0.9, text: 'PANEL DISCUSSION OPENING' },
    { start: 1.0, end: 1.9, text: 'THE MERIDIAN REPORT' },
  ],
};

// The uploaded clip is now a QUOTED preview job, not an immediate call: the UI
// estimates every candidate, sums the paid ones into one confirmation, then
// runs each as an ordinary preview it polls to completion. These three routes
// stand in for that whole path, keyed so a poll answers with the transcript of
// the engine that started that particular job.
const REMOTE_ENGINE = 'stubprovider/whisper-1';

function comparePayload(route: Route): Record<string, unknown> {
  const postData = route.request().postData() ?? '';
  const match = postData.match(/name="payload"\r?\n\r?\n([\s\S]*?)\r?\n--/);
  return match ? JSON.parse(match[1]) : {};
}

/** Only the remote engine is billable; a local one quotes free and must never
 * raise the gate. */
function estimateFor(engine: string) {
  const billable = engine === REMOTE_ENGINE;
  return {
    rows: 1,
    cost: billable ? 0.42 : 0,
    cost_source: billable ? 'estimated' : 'free',
    billed_cost: billable ? 420_000 : 0,
    policy_id: 'frisket.pricing.identity.v1',
    audio_seconds: 12,
    engine,
    requires_confirmation: billable,
    // The hook refuses a quote that demands confirmation without carrying its
    // token, so the paid arm must supply one exactly as the server would.
    ...(billable ? { promise_set_hash: `promise-${engine.replace(/\W/g, '-')}` } : {}),
    claims: billable
      ? [{ field: 'cost', display: '$0.42 for 12s of audio' }]
      : [],
  };
}

async function routeCompareEstimate(page: Page, calls: Array<Record<string, unknown>>) {
  await page.route(/\/transcribe\/compare-scratch\/estimate$/, async (route) => {
    const payload = comparePayload(route);
    calls.push(payload);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.transcribe_compare_estimate.v1',
        source: {
          scratch: true,
          filename: payload.filename ?? 'clip',
          mime: payload.mime ?? 'audio/wav',
          size: 512,
        },
        estimate: estimateFor(String(payload.engine ?? '')),
      }),
    });
  });
}

/** preview id -> the engine whose transcript that job must return. */
async function routeCompareStart(
  page: Page,
  calls: Array<Record<string, unknown>>,
  previewEngines: Map<string, string>,
) {
  await page.route(/\/transcribe\/compare-scratch$/, async (route) => {
    const payload = comparePayload(route);
    calls.push(payload);
    const previewId = `preview-${previewEngines.size + 1}`;
    previewEngines.set(previewId, String(payload.engine ?? ''));
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
}

async function routePreviewPolling(page: Page, previewEngines: Map<string, string>) {
  await page.route('**/actions/v1/preview/*', async (route) => {
    const previewId = route.request().url().split('/').pop() ?? '';
    const engine = previewEngines.get(previewId) ?? '';
    const segments = (
      ENGINE_SEGMENTS[engine] ?? [{ start: 0, end: 1, text: `${engine} text` }]
    ).map((seg, index) => ({ ...seg, segment_index: index }));
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_preview_status.v1',
        preview_id: previewId,
        status: 'done',
        progress: { done: 1, total: 1 },
        accounting: { elapsed_ms: 9 },
        // The sample lives under `result`; the client reads kind/rows/columns
        // from there, never from the envelope (api/actionPreviewRuns.ts).
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
              text: { value: segments.map((s) => s.text).join(' ') },
              segments: { value: segments },
              detected_language: { value: 'en' },
            },
          ],
          warnings: [],
        },
      }),
    });
  });
}

/** Wire all three legs of the quoted flow at once. */
async function routeComparison(page: Page) {
  const estimates: Array<Record<string, unknown>> = [];
  const starts: Array<Record<string, unknown>> = [];
  const previewEngines = new Map<string, string>();
  await routeCompareEstimate(page, estimates);
  await routeCompareStart(page, starts, previewEngines);
  await routePreviewPolling(page, previewEngines);
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

/** Nothing runs on drop any more; the operator asks for it. The two default
 * variants are seeded from the catalog asynchronously, so a Run that lands
 * before the second one exists would quote only the first. */
async function runComparison(page: Page, expectedColumns = 2) {
  await expect(page.getByTestId('transcribe-compare-engine-column')).toHaveCount(
    expectedColumns,
  );
  await page.getByTestId('transcribe-compare-run').click();
}

async function openTranscribeCompare(page: Page) {
  await page.getByTestId('ribbon-tab-transcripts').click();
  await page.getByTestId('ribbon-command-transcribe-compare').click();
  await expect(page.getByTestId('transcribe-compare-tab')).toBeVisible();
}

async function dropMedia(page: Page, testid: string, kind: 'audio' | 'video') {
  await page.getByTestId(testid).setInputFiles({
    name: kind === 'audio' ? 'panel-audio.wav' : 'panel-video.mp4',
    mimeType: kind === 'audio' ? 'audio/wav' : 'video/mp4',
    buffer: kind === 'audio' ? AUDIO_FIXTURE : VIDEO_FIXTURE,
  });
}

test('ribbon opens the scratch Transcribe Compare tab; audio AND video docs render with a player peek', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tc-open'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  const sheetsBefore = await listSheets(page.request, pid);
  const rowsBefore = (await sheetData(page.request, pid, sheetId, 0, 50)).rows.length;

  await patchTranscribeCatalog(page);
  const { estimates, starts } = await routeComparison(page);

  // Guard: nothing durable is written during a scratch session.
  const durableWrites: string[] = [];
  for (const pattern of [
    '**/api/projects/*/import/**',
    '**/api/projects/*/actions/v1/run',
    '**/api/projects/*/rows',
  ]) {
    await page.route(pattern, async (route) => {
      durableWrites.push(route.request().url());
      await route.fulfill({ status: 500, body: 'no durable writes in scratch' });
    });
  }

  await openProject(page, pid, sheetId);
  await openTranscribeCompare(page);

  await expect(page.getByTestId('transcribe-compare-scratch-tag')).toBeVisible();
  await expect(page.getByTestId('transcribe-compare-dropzone')).toBeVisible();

  // Drop an AUDIO clip: two default engine variants are configured, and the
  // duration control offers the first ten minutes by default.
  await dropMedia(page, 'transcribe-compare-file-input', 'audio');
  await expect(page.getByTestId('transcribe-compare-duration')).toHaveValue('600');

  // Nothing has been quoted or run yet. Adding a clip must not spend.
  expect(estimates).toEqual([]);
  expect(starts).toEqual([]);

  // The operator asks for it: each candidate is quoted, then run.
  await runComparison(page);
  const columns = page.getByTestId('transcribe-compare-engine-column');
  await expect(columns).toHaveCount(2);
  await expect.poll(() => starts.length).toBe(2);
  expect(estimates).toHaveLength(2);
  // The quote is per candidate and carries the duration the control shows.
  for (const call of [...estimates, ...starts]) {
    expect(call.time_limit_seconds).toBe(600);
    expect(typeof call.engine).toBe('string');
  }
  // Free local candidates never ask for confirmation.
  for (const call of starts) expect(call.confirmation).toBeUndefined();

  // The source peek is a native player (audio for the audio doc).
  await page.getByTestId('transcribe-compare-source-toggle').click();
  const peek = page.getByTestId('transcribe-compare-player');
  await expect(peek).toBeVisible();
  await expect(peek).toHaveAttribute('data-media-kind', 'audio');

  // Drop a VIDEO clip too: it becomes a second doc with a <video> peek.
  await dropMedia(page, 'transcribe-compare-file-input-more', 'video');
  await expect(page.getByTestId('transcribe-compare-doc-item')).toHaveCount(2);
  await page.getByTestId('transcribe-compare-doc-item').nth(1).click();
  await expect(peek).toHaveAttribute('data-media-kind', 'video');

  // No durable project writes; the sheet is unchanged.
  expect(durableWrites).toEqual([]);
  const sheetsAfter = await listSheets(page.request, pid);
  expect(sheetsAfter.map((s) => s.id)).toEqual(sheetsBefore.map((s) => s.id));
  const rowsAfter = (await sheetData(page.request, pid, sheetId, 0, 50)).rows.length;
  expect(rowsAfter).toBe(rowsBefore);
});

test('symmetric amber diff marks BOTH sides on a seeded segment disagreement', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('tc-diff'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompare(page);
  await dropMedia(page, 'transcribe-compare-file-input', 'audio');
  await runComparison(page);

  const columns = page.getByTestId('transcribe-compare-engine-column');
  await expect(columns).toHaveCount(2);

  // The one disagreeing token is marked on BOTH sides — never a right/wrong color.
  const tokens = page.getByTestId('transcribe-compare-diff-token');
  await expect(tokens).toHaveCount(2);
  await expect(columns.nth(0).getByTestId('transcribe-compare-diff-token')).toHaveText(/MER.?DIAN/);
  await expect(columns.nth(1).getByTestId('transcribe-compare-diff-token')).toHaveText(/MER.?DIAN/);

  // Footer token nav: one token differs, character-level.
  await expect(page.getByTestId('transcribe-compare-token-nav')).toContainText('1 token');
  await expect(page.getByTestId('transcribe-compare-token-nav')).toContainText('character');
});

test('clicking a diff token SEEKS the player to that segment start (timestamp alignment)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tc-seek'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompare(page);
  await dropMedia(page, 'transcribe-compare-file-input', 'audio');
  await runComparison(page);
  await expect(page.getByTestId('transcribe-compare-engine-column')).toHaveCount(2);

  // The disagreement is in segment index 1, whose start is 1.0s. Clicking the
  // diff token opens the peek and seeks the player to that segment's start.
  await page.getByTestId('transcribe-compare-diff-token').first().click();
  const player = page.getByTestId('transcribe-compare-player');
  await expect(player).toBeVisible();
  await expect(player).toHaveAttribute('data-seek-seconds', '1');
  // The real seek ran: the media element's currentTime advanced off zero.
  await expect
    .poll(() => player.evaluate((el) => (el as HTMLMediaElement).currentTime))
    .toBeGreaterThan(0);
});

test('third engine flips diff → survey (no color); narrowing back to two re-arms the diff', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tc-survey'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompare(page);
  await dropMedia(page, 'transcribe-compare-file-input', 'audio');
  await runComparison(page);

  await expect(page.getByTestId('transcribe-compare-mode-diff')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('transcribe-compare-diff-token').first()).toBeVisible();

  await page.getByTestId('transcribe-compare-add-engine').click();
  await page.getByTestId('transcribe-compare-add-engine-option-vosk').click();
  await expect(page.getByTestId('transcribe-compare-engine-column')).toHaveCount(3);
  // The third candidate is configured but unrun until asked for.
  await runComparison(page, 3);
  await expect(page.getByTestId('transcribe-compare-mode-survey')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('transcribe-compare-diff-token')).toHaveCount(0);

  await page.getByTestId('transcribe-compare-engine-chip-remove-vosk').click();
  await expect(page.getByTestId('transcribe-compare-engine-column')).toHaveCount(2);
  await expect(page.getByTestId('transcribe-compare-mode-diff')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('transcribe-compare-diff-token').first()).toBeVisible();
});

test('votes cycle per document; doc list marks + footer tally update', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('tc-votes'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompare(page);
  await dropMedia(page, 'transcribe-compare-file-input', 'audio');
  await runComparison(page);

  const whisperVote = page
    .getByTestId('transcribe-compare-vote-chip')
    .filter({ hasText: 'Whisper (local)' });
  await expect(whisperVote).toHaveAttribute('data-vote', 'neutral');
  await whisperVote.click();
  await expect(whisperVote).toHaveAttribute('data-vote', 'keep');
  await whisperVote.click();
  await expect(whisperVote).toHaveAttribute('data-vote', 'reject');
  await whisperVote.click();
  await expect(whisperVote).toHaveAttribute('data-vote', 'neutral');

  await whisperVote.click();
  await expect(page.getByTestId('transcribe-compare-verdict')).toContainText('Whisper (local)');
  await expect(page.getByTestId('transcribe-compare-doc-item').first()).toHaveAttribute(
    'data-doc-vote',
    /keep|mixed/,
  );
});

test('closing the tab warns that scratch votes/text are discarded (verdict copyable); palette reopens fresh', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tc-discard'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompare(page);
  await dropMedia(page, 'transcribe-compare-file-input', 'audio');
  await runComparison(page);
  await page.getByTestId('transcribe-compare-vote-chip').filter({ hasText: 'Whisper (local)' }).click();

  await page.getByTestId('transcribe-compare-maintab-close').click();
  await expect(page.getByTestId('transcribe-compare-discard-warning')).toBeVisible();
  await expect(page.getByTestId('transcribe-compare-copy-verdict')).toBeVisible();
  await page.getByTestId('transcribe-compare-discard-confirm').click();
  await expect(page.getByTestId('transcribe-compare-tab')).toHaveCount(0);

  // The ⌘K palette command reopens the tab — the session really was discarded.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  await palette.getByTestId('workbench-command-frisket-media-command-transcribe-compare').click();
  await expect(page.getByTestId('transcribe-compare-tab')).toBeVisible();
  await expect(page.getByTestId('transcribe-compare-dropzone')).toBeVisible();
  await expect(page.getByTestId('transcribe-compare-verdict')).toContainText('No votes cast yet');
});

test('no-drift: the transcribe action form and the compare tab render the identical engine option set', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tc-nodrift'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  await routeComparison(page);
  await openProject(page, pid, sheetId);

  // Surface 1: the transcribe action's visible engine picker.
  await openAction(page, 'media.transcribe');
  await expect(page.getByTestId('action-panel')).toBeVisible();
  await page.getByTestId('engine-picker-button').click();
  const tierButtons = page.locator('button[data-testid^="engine-picker-tier-"]');
  let formEngineCount = 0;
  for (let index = 0; index < await tierButtons.count(); index += 1) {
    await tierButtons.nth(index).click();
    formEngineCount += await page.locator('button[data-testid^="engine-option-"]').count();
  }

  // Surface 2: the compare tab's chips + add-engine menu (multi-pick).
  await openTranscribeCompare(page);
  const chipIds = await page
    .getByTestId('transcribe-compare-engine-chip')
    .evaluateAll((chips) => chips.map((chip) => chip.getAttribute('data-engine-id') ?? ''));
  await page.getByTestId('transcribe-compare-add-engine').click();
  await expect(page.getByTestId('transcribe-compare-add-menu')).toBeVisible();
  const menuIds = await page
    .locator('[data-testid^="transcribe-compare-add-engine-option-"]')
    .evaluateAll((options) => options.map((option) => option.getAttribute('data-engine-id') ?? ''));

  const tabEngineIds = [...chipIds, ...menuIds];
  expect(tabEngineIds.length).toBeGreaterThan(0);
  expect(formEngineCount).toBe(tabEngineIds.length);

  await openAction(page, 'media.transcribe');
  await page.getByTestId('engine-picker-button').click();
  const search = page.getByTestId('engine-picker-search');
  for (const engineId of tabEngineIds) {
    await search.fill(engineId);
    await expect(page.locator('button[data-testid^="engine-option-"]')).toHaveCount(1);
  }
});

// Billable -> run; not billable -> preview. This tab AUTO-RUNS on drop, which
// is exactly why a billable engine must not be selectable here: the old flow
// let one session-wide "allow" turn every subsequent drop into unrecorded
// spend. The engine is now absent from the add menu entirely.
test('a billable engine is offered, and spends only through the cost gate', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('tc-billable'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await patchTranscribeCatalog(page);
  const { estimates, starts } = await routeComparison(page);
  await openProject(page, pid, sheetId);
  await openTranscribeCompare(page);
  await dropMedia(page, 'transcribe-compare-file-input', 'audio');

  // The whole catalog is offered now; the gate, not omission, is what stands
  // between the operator and a paid engine.
  await page.getByTestId('transcribe-compare-add-engine').click();
  await page
    .getByTestId(`transcribe-compare-add-engine-option-${REMOTE_ENGINE}`)
    .click();
  await expect(page.getByTestId('transcribe-compare-engine-column')).toHaveCount(3);

  // Still nothing spent: adding the engine only configures a candidate.
  expect(starts).toEqual([]);

  await runComparison(page, 3);

  // Every candidate is quoted BEFORE anything runs, the paid one included.
  await expect.poll(() => estimates.length).toBe(3);
  expect(estimates.map((call) => call.engine)).toContain(REMOTE_ENGINE);
  // The quote carries no confirmation: consent is asked for after the price.
  for (const call of estimates) expect(call.confirmation).toBeUndefined();
  // Nothing has started while the gate is up.
  expect(starts).toEqual([]);

  const gate = page.getByTestId('cost-gate-modal');
  await expect(gate).toBeVisible();
  await expect(page.getByTestId('cost-gate-claims')).toContainText('$0.42');

  // Refusing spends nothing at all, not even on the free candidates.
  await page.getByTestId('cost-gate-cancel').click();
  await expect(gate).toBeHidden();
  expect(starts).toEqual([]);

  // Approving runs every candidate, and only the paid one carries the token
  // the quote issued.
  await runComparison(page, 3);
  await expect(page.getByTestId('cost-gate-modal')).toBeVisible();
  await approveCostGate(page);
  await expect.poll(() => starts.length).toBe(3);
  const paid = starts.filter((call) => call.engine === REMOTE_ENGINE);
  expect(paid).toHaveLength(1);
  expect(paid[0].confirmation).toBe(`promise-${REMOTE_ENGINE.replace(/\W/g, '-')}`);
  for (const call of starts.filter((item) => item.engine !== REMOTE_ENGINE)) {
    expect(call.confirmation).toBeUndefined();
  }
  // The retired blanket flag never reappears on the wire.
  for (const call of starts) expect(call).not.toHaveProperty('allow_remote');
});
