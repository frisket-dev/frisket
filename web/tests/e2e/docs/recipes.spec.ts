import { expect, test } from '@playwright/test';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { copyFileSync, readFileSync, writeFileSync } from 'node:fs';
import { openAction, selectActionSourceColumns, selectRow } from '../helpers';
import { captureCheckpoint, publishCaptureBundle } from './capture';

test.use({ deviceScaleFactor: 2, viewport: { width: 1600, height: 1000 } });
test.skip(!process.env.FRISKET_DOCS_PROOF_DIR, 'Run through the isolated documentation capture harness');

test('court sample imports and runs the selected filings through strict replay', async ({ page }) => {
  test.setTimeout(180_000);
  await page.goto('/learn/court-filings?revision=1');
  await expect(page.getByText('court filings', { exact: false }).first()).toBeVisible();
  await page.getByRole('button', { name: /open/i }).click();
  const flow = page.getByTestId('pdf-batch-flow');
  await expect(flow).toHaveAttribute('data-stage', 'check', { timeout: 90_000 });
  await flow.getByRole('radio', { name: /^None/ }).click();
  await expect(flow.getByRole('radio', { name: /^None/ })).toBeChecked();
  await expect(flow.getByRole('radio', { name: /^Keep documents/ })).toBeChecked();
  await expect(flow.getByText('Loading choices…')).toHaveCount(0, { timeout: 60_000 });
  await expect(flow.getByText('6 files → 6 rows')).toBeVisible();
  const output = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, `court-check-text-proof-${Date.now()}`);
  await publishCaptureBundle(output, { appRevision: process.env.FRISKET_DOCS_REVISION!, sample: { id: 'court-filings', revision: 1 } }, async stage => [
    await captureCheckpoint(page, { id: 'court-check-text', root: flow, annotations: [
      { id: 'ocr-policy', target: { kind: 'radio', group: 'pdf-batch-ocr-policy', value: 'none' }, label: 'None', expected: { kind: 'checked', value: true } },
      { id: 'organization', target: { kind: 'radio', group: 'pdf-batch-organization', value: 'keep_documents' }, label: 'Keep documents', expected: { kind: 'checked', value: true } },
    ] }, stage),
  ]);
  await flow.getByRole('button', { name: 'Continue →' }).click();
  await flow.getByRole('button', { name: 'Search setup →' }).click();
  await flow.getByRole('checkbox', { name: /Search by meaning/ }).uncheck();
  await flow.getByRole('button', { name: /^Import/ }).click();
  await expect(flow).toHaveCount(0, { timeout: 90_000 });
  const pid = new URL(page.url()).pathname.split('/')[2];
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const imported = sheets.find((sheet: { name: string }) => sheet.name === 'Court filings');
  expect(imported?.rows).toBe(6);
  execFileSync(process.env.FRISKET_E2E_PYTHON!, [path.resolve('tests/e2e/docs/render_court_requests.py'), process.env.FRISKET_E2E_WS!, pid, String(imported.id), path.resolve('../guides/recipes/court-filings-to-table.json'), path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'llm-requests.json')], { env: process.env, stdio: 'inherit', timeout: 60_000 });
  expect(JSON.parse(readFileSync(path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'llm-requests.json'), 'utf8'))).toEqual(JSON.parse(readFileSync(path.resolve('tests/e2e/docs/fixtures/court-live-requests.json'), 'utf8')));
  copyFileSync(path.resolve('tests/e2e/docs/fixtures/court-live.cache.db'), path.join(process.env.FRISKET_E2E_WS!, `${pid}.frisket`, 'project.cache.db'));
  await selectRow(page, 0);
  await selectRow(page, 1);
  await selectRow(page, 2);
  await openAction(page, 'map.extract');
  await selectActionSourceColumns(page, 'text');
  await page.getByTestId('output-fields-label').click();
  await expect(page.getByTestId('text-source-columns-menu')).toBeHidden();
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  const selector = page.getByTestId('engine-selector-dialog');
  await expect(selector).toBeVisible();
  await selector.getByText('Gemini 3.5 Flash-Lite', { exact: true }).click();
  await expect(selector).toBeHidden();
  const plan = JSON.parse(readFileSync(path.resolve('../guides/recipes/court-filings-to-table.json'), 'utf8'));
  const settings = Object.fromEntries(plan.steps.find((step: { id: string }) => step.id === 'extract').settings.map((setting: { id: string, value: unknown }) => [setting.id, setting.value])) as Record<string, any>;
  while (await page.getByLabel(/Field \d+ name/).count() < settings.fields.length) await page.getByTestId('generated-action-form').getByRole('button', { name: 'Add column' }).click();
  for (const [index, field] of settings.fields.entries()) {
    await page.getByLabel(`Field ${index + 1} name`).fill(field.name);
    await page.getByLabel(`Field ${index + 1} type`).selectOption(field.type);
    await page.getByLabel(`Field ${index + 1} description`).fill(field.description);
  }
  const paramsRequest = page.waitForRequest(request => request.url().endsWith('/actions/v1/validate-params') && JSON.stringify(request.postDataJSON()).includes(settings.instruction));
  await page.getByTestId('field-instruction').fill(settings.instruction);
  await page.getByTestId('field-citation_mode').selectOption('cite');
  await page.getByTestId('field-instruction').blur();
  const actual = (await paramsRequest).postDataJSON();
  const paramsPath = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'ui-params.json');
  writeFileSync(paramsPath, JSON.stringify(actual, null, 2));
  const authoredPath = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'authored-requests.json');
  execFileSync(process.env.FRISKET_E2E_PYTHON!, [path.resolve('tests/e2e/docs/render_court_requests.py'), process.env.FRISKET_E2E_WS!, pid, String(imported.id), path.resolve('../guides/recipes/court-filings-to-table.json'), authoredPath, paramsPath], { env: process.env, stdio: 'inherit', timeout: 60_000 });
  expect(JSON.parse(readFileSync(authoredPath, 'utf8'))).toEqual(JSON.parse(readFileSync(path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'llm-requests.json'), 'utf8')));
  await page.screenshot({ path: path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'extract-settings.png') });
  await page.getByTestId('generated-action-run-scope-menu-button').click();
  await expect(page.getByTestId('generated-action-row-scope-selected')).toContainText('3 selected');
  const start = page.waitForResponse(response => response.url().endsWith('/actions/v1/run') && response.request().method() === 'POST', { timeout: 60_000 });
  await page.getByTestId('generated-action-row-scope-selected').click();
  if (await page.getByTestId('generated-action-run').isVisible({ timeout: 2_000 }).catch(() => false)) await page.getByTestId('generated-action-run').click();
  const gate = page.getByTestId('cost-gate-modal');
  if (await gate.isVisible({ timeout: 2_000 }).catch(() => false)) {
    await page.getByTestId('cost-gate-input').fill('confirm');
    await page.getByTestId('cost-gate-confirm').click();
  }
  const runResponse = await start;
  expect(runResponse.ok()).toBeTruthy();
  const { run_id: runId } = await runResponse.json();
  expect(typeof runId).toBe('number');
  await expect.poll(async () => {
    const response = await page.request.get(`/api/projects/${pid}/actions/runs/${runId}/status`);
    expect(response.ok()).toBeTruthy();
    const { run } = await response.json();
    if (['failed', 'cancelled', 'stalled', 'orphaned'].includes(run.status)) throw new Error(`Extract run ${runId} ${run.status}`);
    return [run.status, run.completed_rows, run.failed_rows];
  }, { timeout: 120_000 }).toEqual(['completed', 3, 0]);
  await expect.poll(async () => {
    const current = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
    const sheet = current.find((item: { id: number }) => item.id === imported.id);
    return ['case_number', 'filed_at', 'plaintiff', 'defendant_as_filed'].every(name => sheet.columns.some((column: { name: string, latest_run_id: number }) => column.name === name && column.latest_run_id === runId));
  }, { timeout: 30_000 }).toBe(true);
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible();
  await page.getByTestId('ribbon-tab-tables').click();
  await page.getByRole('button', { name: 'Join expected answers', exact: true }).click();
  const join = page.getByTestId('join-expected-dialog');
  await expect(join).toBeVisible();
  const latestSheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const expected = latestSheets.find((sheet: { name: string }) => sheet.name === 'Expected');
  const target = latestSheets.find((sheet: { id: number }) => sheet.id === imported.id);
  await join.getByLabel('Expected sheet', { exact: true }).selectOption(String(expected.id));
  await expect(join.getByLabel('Action to check').locator('option')).toHaveCount(2);
  await join.getByLabel('Action to check').selectOption({ index: 1 });
  await join.getByRole('button', { name: 'Next', exact: true }).click();
  const expectedId = (name: string) => String(expected.columns.find((column: { name: string }) => column.name === name).id);
  const targetId = (name: string) => String(target.columns.find((column: { name: string }) => column.name === name).id);
  await join.getByLabel('Expected key 1').selectOption(expectedId('pdf_filename'));
  await join.getByLabel('Target key 1').selectOption(targetId('filename'));
  await expect(join.getByRole('status')).toContainText('6 matched · 0 not found');
  await join.getByRole('button', { name: 'Next', exact: true }).click();
  for (const name of ['case_number', 'filed_at', 'plaintiff', 'defendant_as_filed']) {
    await join.getByLabel(`Role for ${name}`, { exact: true }).selectOption({ label: `Expected · ${name}` });
  }
  await join.getByLabel('Match rule for filed_at').selectOption('date');
  await page.screenshot({ path: path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'join-expected.png') });
  await join.getByRole('button', { name: 'Join', exact: true }).click();
  await expect(join).toHaveCount(0);
  await page.getByTestId('ribbon-tab-analyze').click();
  await page.getByTestId('compare-menu-trigger').click();
  await page.getByRole('menuitem', { name: /vs expected/ }).click();
  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible();
  for (const name of ['case_number', 'filed_at', 'plaintiff', 'defendant_as_filed']) {
    await page.getByRole('group', { name: 'Comparison field' }).getByRole('button', { name, exact: true }).click();
  }
  await page.screenshot({ path: path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'comparison.png') });
});

test('council sample opens the real transcription comparison', async ({ page }) => {
  test.setTimeout(240_000);
  await page.goto('/learn/council-recordings?revision=1');
  await page.getByRole('button', { name: 'Open in Frisket' }).click();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 90_000 });
  await page.getByTestId('ribbon-tab-media').click();
  await page.getByTestId('ribbon-command-transcribe-compare').click();
  const compare = page.getByTestId('transcribe-compare-tab');
  await expect(compare).toBeVisible();
  const filename = 'ann-arbor-policy-committee-2022-04-19-excerpt.mp3';
  const audio = execFileSync(process.env.FRISKET_E2E_PYTHON!, ['-c', 'import sys; from frisket.sample_content.audio import council_audio_bytes; sys.stdout.buffer.write(council_audio_bytes(sys.argv[1]))', filename], { env: process.env, maxBuffer: 20 * 1024 * 1024 });
  await page.getByTestId('transcribe-compare-file-input').setInputFiles({ name: filename, mimeType: 'audio/mpeg', buffer: audio });
  await page.getByTestId('transcribe-compare-duration').fill('30');
  await expect(page.getByTestId('transcribe-compare-duration')).toHaveValue('30');
  const columns = page.getByTestId('transcribe-compare-engine-column');
  await expect(columns).toHaveCount(2);
  await expect(page.getByTestId('transcribe-compare-engine-chips')).toContainText('Whisper');
  await expect(page.getByTestId('transcribe-compare-engine-chips')).toContainText('MAI-Transcribe');
  page.on('response', response => { if (response.url().includes('/transcribe/compare-scratch') && !response.ok()) void response.text().then(body => console.log('DOCS_TRANSCRIBE_ERROR', response.status(), body)); });
  await page.getByTestId('transcribe-compare-run').click();
  const gate = page.getByTestId('cost-gate-modal');
  if (await gate.isVisible({ timeout: 10_000 }).catch(() => false)) await page.getByTestId('cost-gate-confirm').click();
  await expect(page.locator('[data-testid="transcribe-compare-engine-column"][data-run-status="done"]')).toHaveCount(2, { timeout: 120_000 });
  await page.screenshot({ path: path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'council-comparison.png') });
});

test('council full-sheet transcription uses the installed Whisper base model', async ({ page }) => {
  test.setTimeout(300_000);
  await page.goto('/learn/council-recordings?revision=1');
  await page.getByRole('button', { name: 'Open in Frisket' }).click();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 90_000 });
  await openAction(page, 'media.transcribe');
  await page.getByTestId('field-source').selectOption('media');
  const engine = page.getByTestId('field-engine');
  await expect(page.getByText('Loading models…')).toHaveCount(0, { timeout: 60_000 });
  await engine.locator('.engine-selector__trigger').click();
  const selector = page.getByTestId('engine-selector-dialog');
  await selector.getByText('Whisper (multilingual transcription)', { exact: true }).click();
  await expect(selector).toBeHidden();
  await expect(engine).toContainText('Whisper (multilingual transcription)');
  await page.getByTestId('field-model_size').selectOption('base');
  await expect(page.getByTestId('field-model_size')).toHaveValue('base');
  await page.getByTestId('field-output-text').fill('transcript');
  await page.getByTestId('field-output-segments').fill('transcript_segments');
  await page.getByTestId('field-output-segments').blur();
  await expect(engine).toContainText('Whisper (multilingual transcription)');
  await expect(page.getByText('Loading models…')).toHaveCount(0);
  await page.screenshot({ path: path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'council-settings.png') });
  const pid = new URL(page.url()).pathname.split('/')[2];
  const start = page.waitForResponse(response => response.url().endsWith('/actions/v1/run') && response.request().method() === 'POST', { timeout: 60_000 });
  await page.getByTestId('generated-action-run').click();
  const response = await start;
  expect(response.ok()).toBeTruthy();
  const { run_id: runId } = await response.json();
  expect(typeof runId).toBe('number');
  await expect.poll(async () => {
    const status = await page.request.get(`/api/projects/${pid}/actions/runs/${runId}/status`);
    expect(status.ok()).toBeTruthy();
    const { run } = await status.json();
    if (['failed', 'cancelled', 'stalled', 'orphaned'].includes(run.status)) throw new Error(`Transcribe run ${runId} ${run.status}: ${run.public_status?.error ?? ''}`);
    return [run.status, run.completed_rows, run.failed_rows];
  }, { timeout: 240_000 }).toEqual(['completed', 2, 0]);
  await page.getByTestId('view-switch-document').click();
  await page.getByTestId('document-list-item').filter({ hasText: 'ann-arbor-policy-committee-2022-04-19' }).click();
  const transcript = page.getByTestId('document-temporal-transcript');
  await expect(transcript).toBeVisible({ timeout: 30_000 });
  const segment = transcript.getByTestId('document-transcript-segment').nth(1);
  const marker = (await segment.locator('.evidence-temporal-segment-marker').innerText()).trim();
  const parts = marker.split(':').map(Number);
  const seconds = parts.reduce((total, part) => total * 60 + part, 0);
  expect(Number.isFinite(seconds)).toBeTruthy();
  await segment.click();
  await expect(segment).toHaveAttribute('data-active', 'true');
  await expect.poll(() => transcript.locator('audio').evaluate((el: HTMLAudioElement) => el.currentTime)).toBeGreaterThanOrEqual(Math.max(0, seconds - 1));
  await page.screenshot({ path: path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'council-timed.png') });

});
