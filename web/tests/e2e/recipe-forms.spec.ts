// Action panel forms for the non-classify ops. RED-FIRST
// acceptance check authored by the check-author, not the implementer.
//
// The endpoints + actions already exist and the seeder uses them. Forms open
// in the overlay Action drawer via the Act ribbon (openAction helper,
// workbench-ia-action-drawer-v1).
//
// The "each op's form exposes its distinctive field" coverage moved to
// tests/component/ActionFormRecipeFields.test.tsx — that assertion is pure
// ActionForm rendering, mounted directly with a typed ActionTemplate rather
// than a live project. What remains here needs a real backend/dev-server:
// catalog reachability across ribbon/menu surfaces, backend-computed engine
// availability copy, and an actual op execution updating sheet stats.

import { expect, test } from '@playwright/test';
import {
  createProject,
  finishLocalQueuedRun,
  importCsv,
  openAction,
  selectActionInputColumns,
  uniqueName,
} from './helpers';

const CSV = 'name,note\n"Ada","met the mayor"\n"Joe","filed a complaint"\n';

test('catalog actions without static cards are reachable and runnable', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-backend-actions'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'dates.csv',
    'raw,address\n"12/06/2026","1600 Pennsylvania Ave NW, Washington, DC"\n',
  );
  await page.goto(`/p/${pid}`);

  // Each backend-advertised kind must be reachable from the Act ribbon (its
  // tile is generated from /api/actions/v1/catalog) and open a drawer form.
  for (const kind of ['map.clean_dates', 'enrich.geocode', 'agent', 'media.video_frames', 'media.extract_faces']) {
    await openAction(page, kind);
  }

  await openAction(page, 'map.clean_dates');
  await expect(page.getByTestId('clean-dates-formats')).toBeVisible();
  await page.getByTestId('clean-dates-format-select').selectOption('%d/%m/%Y');
  const cleanDatesRun = page.waitForRequest((request) =>
    request.url().includes('/actions/v1/run') && request.method() === 'POST',
  );
  await page.getByTestId('generated-action-run').click();
  await expect((await cleanDatesRun).postDataJSON()).toMatchObject({
    action_id: 'map.clean_dates',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { source: 'raw', format: '%d/%m/%Y' },
    output_names: { cleaned: 'raw_iso' },
    idempotency_key: expect.any(String),
  });
  await expect(page.getByTestId('sheet-stats')).toHaveText('1 rows · 3 columns', {
    timeout: 30_000,
  });
});

test('media action engine picker uses backend availability metadata', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-action-engines'));
  await importCsv(page.request, pid, 'rows.csv', CSV);
  await page.goto(`/p/${pid}`);

  await openAction(page, 'media.transcribe');
  await page.getByTestId('engine-picker-button').click();
  await expect(page.getByTestId('engine-option-faster_whisper')).toBeEnabled();
  await page.getByTestId('engine-picker-tier-sidecar').click();
  await expect(page.getByTestId('engine-option-faster-whisper')).toBeDisabled();
  await page.getByTestId('engine-picker-button').click();
  // S7: unavailable-engine remediation now lives inside the collapsed
  // "Engine availability" disclosure — expand it before asserting.
  await page.getByTestId('engine-availability-disclosure').locator('summary').click();
  await expect(page.getByText('FRISKET_MODELS_URL')).toBeVisible();

  await openAction(page, 'media.ocr');
  await page.getByTestId('engine-picker-button').click();
  await expect(page.getByTestId('engine-option-rapidocr')).toBeEnabled();
  await page.getByTestId('engine-picker-tier-sidecar').click();
  await expect(page.getByTestId('engine-option-dots-mocr')).toBeDisabled();
  await page.getByTestId('engine-picker-button').click();
  // Two sidecar OCR engines now (dots.mocr + paddleocr-vl), so the unconfigured-
  // sidecar hint renders once per engine — assert the first is present.
  await page.getByTestId('engine-availability-disclosure').locator('summary').click();
  await expect(page.getByText('FRISKET_MODELS_URL').first()).toBeVisible();

  await openAction(page, 'media.to_markdown');
  await page.getByTestId('engine-picker-button').click();
  await expect(page.getByTestId('engine-option-markitdown')).toBeEnabled();
  await page.getByTestId('engine-picker-tier-sidecar').click();
  await expect(page.getByTestId('engine-option-docling')).toBeDisabled();
});

test('an action form actually runs (regex, offline) — not just visible controls', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-forms-run'));
  // a column with an extractable pattern (a dollar amount)
  await importCsv(
    page.request,
    pid,
    'rows.csv',
    'note\n"contract worth $4,200 total"\n"fee was $96 flat"\n',
  );
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 1 columns');

  await openAction(page, 'map.regex_extract');
  await selectActionInputColumns(page, 'note');
  await expect(page.getByTestId('field-pattern')).toBeVisible();
  await page.getByTestId('field-pattern').fill('\\$([0-9,]+)');
  const regexRun = page.waitForResponse((response) =>
    response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    response.request().method() === 'POST',
  );
  await page.getByTestId('generated-action-run').click();
  const { run_id: runId } = (await (await regexRun).json()) as { run_id: number | null };
  if (runId == null) throw new Error('regex v1 run did not return a run id');
  finishLocalQueuedRun(pid, runId);

  // a new column lands with the captured values — the form is wired to a
  // real run (regex is computed/offline, so this is deterministic).
  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 2 columns', {
    timeout: 30_000,
  });
});
