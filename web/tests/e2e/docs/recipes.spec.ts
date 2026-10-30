import { expect, test } from '@playwright/test';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { copyFileSync, readFileSync, writeFileSync } from 'node:fs';
import { openAction, openCellDrawer, selectActionSourceColumns, selectRow, sheetColumns, sheetData } from '../helpers';
import { captureCheckpoint, publishCaptureBundle } from './capture';

test.use({ deviceScaleFactor: 2, viewport: { width: 1600, height: 1000 } });
test.skip(!process.env.FRISKET_DOCS_PROOF_DIR, 'Run through the isolated documentation capture harness');
test.beforeAll(() => {
  const root = path.resolve('..');
  const git = (...args: string[]) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
  expect(process.env.FRISKET_DOCS_REVISION).toBe(git('rev-parse', 'HEAD'));
  const changes = git('status', '--porcelain', '--untracked-files=no').split('\n').filter(Boolean);
  const permittedOverlay = process.env.FRISKET_DOCS_PACKAGE_OVERLAY === '1' ? ['M web/package.json'] : [];
  expect(changes.filter(change => !permittedOverlay.includes(change))).toEqual([]);
});

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
  const output = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'court');
  await publishCaptureBundle(output, { appRevision: process.env.FRISKET_DOCS_REVISION!, sample: { id: 'court-filings', revision: 1 }, replay: 'recorded-gemini-3.5-flash-lite' }, async stage => {
    const captures = [await captureCheckpoint(page, { id: 'court-check-text', root: flow, annotations: [
      { id: 'ocr-policy', target: { kind: 'radio', group: 'pdf-batch-ocr-policy', value: 'none' }, label: 'None', expected: { kind: 'checked', value: true } },
      { id: 'organization', target: { kind: 'radio', group: 'pdf-batch-organization', value: 'keep_documents' }, label: 'Keep documents', expected: { kind: 'checked', value: true } },
    ] }, stage)];
  await flow.getByRole('button', { name: 'Continue →' }).click();
  await flow.getByRole('button', { name: 'Search setup →' }).click();
  await flow.getByRole('checkbox', { name: /Search by meaning/ }).uncheck();
  await flow.getByRole('button', { name: /^Import/ }).click();
  await expect(flow).toHaveCount(0, { timeout: 90_000 });
  const pid = new URL(page.url()).pathname.split('/')[2];
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const imported = sheets.find((sheet: { name: string }) => sheet.name === 'Court filings');
  expect(imported?.rows).toBe(6);
  const initialData = await sheetData(page.request, pid, imported.id, 0, 6);
  const filenameColumn = initialData.columns.find(column => column.name === 'filename');
  expect(filenameColumn).toBeDefined();
  expect(initialData.rows.slice(0, 3).map(row => row.cells[String(filenameColumn!.id)])).toEqual([
    'RV-CV-2026-00418_alvarez-v-northstar_complaint.pdf',
    'RV-CV-2026-00502_water-alliance_records-petition.pdf',
    'RV-CV-2026-00611_meridian-v-riverton_injunction-motion.pdf',
  ]);
  const selectedRowIds = initialData.rows.slice(0, 3).map(row => row.id).sort((a, b) => a - b);
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
  const settings = Object.fromEntries(plan.steps.find((step: { id: string }) => step.id === 'extract').settings.map((setting: { id: string, value: unknown }) => [setting.id, setting.value])) as { fields: Array<{ name: string; type: string; description: string }>; instruction: string };
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
  const source = page.getByTestId('text-source-columns');
  const engineTarget = page.locator('[data-tour="action-engine-selector"]');
  await page.getByText('Source', { exact: true }).evaluate(element => element.scrollIntoView({ block: 'center' }));
  await expect(source.getByRole('button', { name: 'Remove text' })).toBeVisible();
  await expect(engineTarget).toContainText('Gemini 3.5 Flash-Lite');
  captures.push(await captureCheckpoint(page, { id: 'court-extract-settings', root: page.getByTestId('action-panel'), cropBetween: { top: page.getByText('Source', { exact: true }), bottom: engineTarget }, annotations: [
    { id: 'source', number: 1, target: { kind: 'field', id: 'text-source-columns' }, label: 'Source', labelTarget: page.getByText('Source', { exact: true }), expected: { kind: 'text', value: (await source.innerText()).trim() } },
    { id: 'engine', number: 2, target: { kind: 'tour', id: 'action-engine-selector' }, label: 'Gemini 3.5 Flash-Lite', labelTarget: engineTarget.locator('strong'), expected: { kind: 'text', value: (await engineTarget.innerText()).trim() } },
  ] }, stage));
  const fieldsLabel = page.getByTestId('output-fields-label');
  await fieldsLabel.scrollIntoViewIfNeeded();
  await page.getByTestId('output-fields-editor').scrollIntoViewIfNeeded();
  captures.push(await captureCheckpoint(page, { id: 'court-extract-fields', root: page.getByTestId('action-panel'), cropBetween: { top: fieldsLabel.locator('..'), bottom: page.getByTestId('output-fields-editor') }, annotations: [
    { id: 'fields', number: 4, target: { kind: 'field', id: 'output-fields-label' }, label: 'Columns it creates', labelTarget: fieldsLabel, expected: { kind: 'text', value: 'Columns it creates' } },
  ] }, stage));
  const instruction = page.getByTestId('field-instruction');
  const citations = page.getByTestId('field-citation_mode');
  await citations.scrollIntoViewIfNeeded();
  captures.push(await captureCheckpoint(page, { id: 'court-extract-instruction', root: page.getByTestId('action-panel'), cropBetween: { top: page.getByText('Instruction', { exact: true }), bottom: citations.locator('xpath=ancestor::label[1]') }, annotations: [
    { id: 'instruction', number: 5, target: { kind: 'field', id: 'field-instruction' }, label: 'Instruction', expected: { kind: 'value', value: settings.instruction } },
    { id: 'citations', number: 6, target: { kind: 'field', id: 'field-citation_mode' }, label: 'Citations', labelTarget: page.getByText('Citations', { exact: true }), expected: { kind: 'value', value: 'cite' } },
  ] }, stage));
  await expect(page.locator('.run-scope-summary')).toContainText('3 selected rows');
  await page.getByTestId('generated-action-preview').click();
  const previewGate = page.getByTestId('cost-gate-modal');
  if (await previewGate.isVisible({ timeout: 2_000 }).catch(() => false)) {
    const input = page.getByTestId('cost-gate-input');
    if (await input.isVisible()) await input.fill('confirm');
    await page.getByTestId('cost-gate-confirm').click();
  }
  await expect(page.getByTestId('preview-tab-banner')).toHaveAttribute('data-status', 'done', { timeout: 120_000 });
  await expect(page.getByTestId('preview-run-for-real-button')).toBeVisible();
  await page.getByTestId('action-drawer-close').click();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  captures.push(await captureCheckpoint(page, { id: 'court-preview', root: page.getByTestId('workbench-region-mainView'), annotations: [] }, stage));
  const start = page.waitForResponse(response => response.url().endsWith('/actions/v1/run') && response.request().method() === 'POST', { timeout: 60_000 });
  await page.getByTestId('preview-run-for-real-button').click();
  const gate = page.getByTestId('cost-gate-modal');
  if (await gate.isVisible({ timeout: 2_000 }).catch(() => false)) {
    const input = page.getByTestId('cost-gate-input');
    if (await input.isVisible()) await input.fill('confirm');
    await page.getByTestId('cost-gate-confirm').click();
  }
  const runResponse = await start;
  expect(runResponse.ok()).toBeTruthy();
  const posted = runResponse.request().postDataJSON();
  expect([...posted.scope.row_ids].sort((a: number, b: number) => a - b)).toEqual(selectedRowIds);
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
  captures.push(await captureCheckpoint(page, { id: 'court-sample-run', root: page.getByTestId('grid'), annotations: [] }, stage));
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
  captures.push(await captureCheckpoint(page, { id: 'court-expected-join', root: join, annotations: [] }, stage));
  await join.getByRole('button', { name: 'Join', exact: true }).click();
  await expect(join).toHaveCount(0);
  await page.getByTestId('ribbon-tab-analyze').click();
  await page.getByTestId('compare-menu-trigger').click();
  let score: import('../../../src/api/evaluation').EvaluationResponse | null = null;
  page.on('response', async response => {
    if (response.url().endsWith('/evaluation/score') && response.ok()) score = await response.json();
  });
  await page.getByRole('menuitem', { name: /vs expected/ }).click();
  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible();
  for (const name of ['case_number', 'filed_at', 'plaintiff', 'defendant_as_filed']) {
    await page.getByRole('group', { name: 'Comparison field' }).getByRole('button', { name, exact: true }).click();
  }
  await expect.poll(() => score?.link?.id ?? null, { timeout: 30_000 }).not.toBeNull();
  const scored = score as import('../../../src/api/evaluation').EvaluationResponse;
  expect([...scored.row_ids].sort((a, b) => a - b)).toEqual(selectedRowIds);
  writeFileSync(path.join(stage, 'court-comparison-score.json'), JSON.stringify(scored, null, 2));
  await expect(page.getByText('Join a sheet of expected answers to check this action.')).toHaveCount(0, { timeout: 30_000 });
  await expect(page.getByText('Scoring…')).toHaveCount(0, { timeout: 30_000 });
  captures.push(await captureCheckpoint(page, { id: 'court-comparison', root: page.getByRole('region', { name: 'Action comparison' }), annotations: [] }, stage));
  await page.getByTestId('workbench-mainView-tab-' + imported.id).click();
  await openAction(page, 'map.extract');
  await selectActionSourceColumns(page, 'text');
  await page.getByTestId('ribbon-tab-analyze').click();
  await expect(page.getByTestId('text-source-columns-menu')).toBeHidden();
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await page.getByTestId('engine-selector-dialog').getByText('Gemini 3.5 Flash-Lite', { exact: true }).click();
  await expect(page.getByTestId('engine-selector-dialog')).toBeHidden();
  while (await page.getByLabel(/Field \d+ name/).count() < settings.fields.length) await page.getByTestId('generated-action-form').getByRole('button', { name: 'Add column' }).click();
  for (const [index, field] of settings.fields.entries()) {
    await page.getByLabel(`Field ${index + 1} name`).fill(field.name);
    await page.getByLabel(`Field ${index + 1} type`).selectOption(field.type);
    await page.getByLabel(`Field ${index + 1} description`).fill(field.description);
  }
  await page.getByTestId('field-instruction').fill(settings.instruction);
  await page.getByTestId('field-citation_mode').selectOption('cite');
  await expect(page.getByTestId('field-engine')).toContainText('Gemini 3.5 Flash-Lite');
  await page.getByTestId('generated-action-run-scope-menu-button').click();
  await expect(page.getByTestId('generated-action-row-scope-all')).toBeVisible();
  captures.push(await captureCheckpoint(page, { id: 'court-run-settings', root: page.getByTestId('action-panel'), annotations: [] }, stage));
  const fullStart = page.waitForResponse(response => response.url().endsWith('/actions/v1/run') && response.request().method() === 'POST', { timeout: 60_000 });
  await page.getByTestId('generated-action-row-scope-all').click();
  if (await page.getByTestId('generated-action-run').isVisible({ timeout: 2_000 }).catch(() => false)) await page.getByTestId('generated-action-run').click();
  const fullGate = page.getByTestId('cost-gate-modal');
  if (await fullGate.isVisible({ timeout: 2_000 }).catch(() => false)) {
    await page.getByTestId('cost-gate-input').fill('confirm');
    await page.getByTestId('cost-gate-confirm').click();
  }
  const fullResponse = await fullStart;
  expect(fullResponse.ok()).toBeTruthy();
  const { run_id: fullRunId } = await fullResponse.json();
  await expect.poll(async () => {
    const status = await page.request.get(`/api/projects/${pid}/actions/runs/${fullRunId}/status`);
    const { run } = await status.json();
    if (['failed', 'cancelled', 'stalled', 'orphaned'].includes(run.status)) throw new Error(`Extract run ${fullRunId} ${run.status}`);
    return [run.status, run.completed_rows, run.failed_rows];
  }, { timeout: 120_000 }).toEqual(['completed', 6, 0]);
  const actualRows = await sheetData(page.request, pid, imported.id, 0, 6);
  const expectedRows = await sheetData(page.request, pid, expected.id, 0, 6);
  const actualColumns = Object.fromEntries(actualRows.columns.map(column => [column.name, String(column.id)]));
  const expectedColumns = Object.fromEntries(expectedRows.columns.map(column => [column.name, String(column.id)]));
  const fullSheet = (await (await page.request.get(`/api/projects/${pid}/sheets`)).json()).find((sheet: { id: number }) => sheet.id === imported.id);
  for (const name of ['case_number', 'filed_at', 'plaintiff', 'defendant_as_filed']) {
    expect(fullSheet.columns.find((column: { name: string }) => column.name === `${name}_2`)?.latest_run_id).toBe(fullRunId);
  }
  const byFilename = new Map(expectedRows.rows.map(row => [row.cells[expectedColumns.pdf_filename], row]));
  const review = actualRows.rows.map(row => {
    const filename = row.cells[actualColumns.filename];
    const answer = byFilename.get(filename);
    if (!answer) throw new Error(`Expected filing not found: ${String(filename)}`);
    return { filename, fields: Object.fromEntries(['case_number', 'filed_at', 'plaintiff', 'defendant_as_filed'].map(name => [name, { actual: row.cells[actualColumns[`${name}_2`]], expected: answer.cells[expectedColumns[name]] }])) };
  });
  expect(review).toHaveLength(6);
  writeFileSync(path.join(stage, 'court-review.json'), JSON.stringify({ runId: fullRunId, rows: review }, null, 2));
  captures.push(await captureCheckpoint(page, { id: 'court-results', root: page.getByTestId('grid'), annotations: [] }, stage));
  await openAction(page, 'media.ocr');
  await expect(page.getByText('Loading models…')).toHaveCount(0, { timeout: 60_000 });
  await expect(page.getByText('Loading choices…')).toHaveCount(0, { timeout: 60_000 });
  await expect(page.getByTestId('field-engine').locator('.engine-selector__trigger')).toBeVisible({ timeout: 60_000 });
  captures.push(await captureCheckpoint(page, { id: 'court-ocr-settings', root: page.getByTestId('action-panel'), cropBetween: { top: page.getByTestId('action-panel'), bottom: page.getByText('Text result', { exact: true }).locator('..') }, annotations: [] }, stage));
  await page.getByTestId('field-engine').locator('.engine-selector__trigger').click();
  await expect(page.getByTestId('engine-selector-dialog')).toBeVisible();
  captures.push(await captureCheckpoint(page, { id: 'court-ocr-engine', root: page.getByTestId('engine-selector-dialog'), annotations: [] }, stage));
  const required = ['court-check-text', 'court-extract-settings', 'court-extract-fields', 'court-extract-instruction', 'court-preview', 'court-sample-run', 'court-expected-join', 'court-comparison', 'court-run-settings', 'court-results', 'court-ocr-settings', 'court-ocr-engine'];
  expect(captures.map(item => item.id)).toEqual(required);
  return captures;
  });
});

test('council sample opens the real transcription comparison', async ({ page }) => {
  test.setTimeout(240_000);
  const output = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'council');
  await publishCaptureBundle(output, { appRevision: process.env.FRISKET_DOCS_REVISION!, sample: { id: 'council-recordings', revision: 1 }, replay: 'whisper-base-local;recorded-mai-transcribe-2' }, async stage => {
  const captures = [];
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
  await compare.getByText(/out there today/i).scrollIntoViewIfNeeded();
  captures.push(await captureCheckpoint(page, { id: 'council-engine-comparison', root: compare, annotations: [
    { id: 'first-seconds', number: 3, target: { kind: 'element', id: 'transcribe-compare-duration' }, label: 'First seconds', expected: { kind: 'value', value: '30' } },
  ] }, stage));
  await page.getByTestId('transcribe-compare-maintab-close').click();
  await page.getByTestId('transcribe-compare-discard-confirm').click();
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
  const form = page.getByTestId('generated-action-form');
  const engineTarget = form.locator('[data-tour="action-engine-selector"]');
  await page.getByTestId('field-source').scrollIntoViewIfNeeded();
  captures.push(await captureCheckpoint(page, { id: 'council-transcribe-settings', root: page.getByTestId('action-panel'), annotations: [
    { id: 'source', number: 1, target: { kind: 'field', id: 'field-source' }, label: 'Media', expected: { kind: 'value', value: 'media' } },
    { id: 'engine', number: 2, target: { kind: 'tour', id: 'action-engine-selector' }, label: 'Whisper (multilingual transcription)', labelTarget: engineTarget.locator('strong'), expected: { kind: 'text', value: (await engineTarget.innerText()).trim() } },
    { id: 'model-size', number: 3, target: { kind: 'field', id: 'field-model_size' }, label: 'Model size', expected: { kind: 'value', value: 'base' } },
  ] }, stage));
  await page.getByTestId('field-output-segments').scrollIntoViewIfNeeded();
  captures.push(await captureCheckpoint(page, { id: 'council-transcribe-outputs', root: page.getByTestId('action-panel'), annotations: [
    { id: 'transcript', number: 4, target: { kind: 'field', id: 'field-output-text' }, label: 'Transcript', expected: { kind: 'value', value: 'transcript' } },
    { id: 'segments', number: 5, target: { kind: 'field', id: 'field-output-segments' }, label: 'Timestamped segments', expected: { kind: 'value', value: 'transcript_segments' } },
  ] }, stage));
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
  expect((await segment.innerText()).trim().length).toBeGreaterThan(marker.length);
  await segment.click();
  await expect(segment).toHaveAttribute('data-active', 'true');
  await expect.poll(async () => Math.abs(await transcript.locator('audio').evaluate((el: HTMLAudioElement) => el.currentTime) - seconds)).toBeLessThanOrEqual(1);
  captures.push(await captureCheckpoint(page, { id: 'council-timed-transcript', root: transcript, annotations: [] }, stage));
  await page.getByTestId('view-switch-grid').click();
  captures.push(await captureCheckpoint(page, { id: 'council-results', root: page.getByTestId('grid'), annotations: [] }, stage));
  expect(captures.map(item => item.id)).toEqual(['council-engine-comparison', 'council-transcribe-settings', 'council-transcribe-outputs', 'council-timed-transcript', 'council-results']);
  return captures;
  });
});


test('docs entry: council source details and Transcribe Compare', async ({ page }) => {
  test.setTimeout(120_000);
  const output = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'entry-council');
  await publishCaptureBundle(output, { appRevision: process.env.FRISKET_DOCS_REVISION!, sample: { id: 'council-recordings', revision: 1 } }, async stage => {
    const captures = [];
    await page.goto('/learn/council-recordings?revision=1');
    await page.getByRole('button', { name: 'Open in Frisket' }).click();
    await expect(page.getByTestId('grid')).toBeVisible({ timeout: 90_000 });
    const pid = new URL(page.url()).pathname.split('/')[2];
    const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
    const audio = sheets.find((sheet: { name: string }) => sheet.name === 'Council audio');
    const meetings = sheets.find((sheet: { name: string }) => sheet.name === 'Council meetings');
    expect(audio?.rows).toBe(2);
    expect(meetings?.rows).toBe(2);
    await expect(page.getByTestId(`workbench-mainView-tab-${audio.id}`)).toBeVisible();
    await expect(page.getByTestId(`workbench-mainView-tab-${meetings.id}`)).toBeVisible();
    captures.push(await captureCheckpoint(page, { id: 'council-audio-source', root: page.locator('.workbench-content'), annotations: [] }, stage));
    await page.getByTestId(`workbench-mainView-tab-${meetings.id}`).click();
    await expect(page.getByTestId('grid')).toBeVisible();
    const columns = await sheetColumns(page.request, pid, meetings.id);
    await openCellDrawer(page, columns, 'audio_filename', 0);
    const drawer = page.getByTestId('row-drawer');
    await expect(drawer).toBeVisible();
    await expect(drawer.getByTestId('row-field-source_url')).toBeVisible();
    await drawer.getByTestId('row-field-source_url').scrollIntoViewIfNeeded();
    await expect(drawer).toContainText('PolicyCommittee-2022-04-19.ogg');
    await expect(drawer).toContainText('CC0-1.0');
    captures.push(await captureCheckpoint(page, { id: 'council-source-details', root: drawer, annotations: [] }, stage));
    await page.getByTestId(`workbench-mainView-tab-${audio.id}`).click();
    await page.getByTestId('ribbon-tab-media').click();
    const ribbon = page.getByTestId('act-ribbon');
    await expect(ribbon.getByTestId('ribbon-command-transcribe-compare')).toBeVisible();
    const compareGroup = ribbon.getByTestId('ribbon-command-transcribe-compare').locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " act-ribbon-group ")][1]');
    await expect(compareGroup).toHaveCount(1);
    captures.push(await captureCheckpoint(page, { id: 'council-compare-entry', root: compareGroup, annotations: [] }, stage));
    await expect(ribbon.getByTestId('ribbon-action-media.transcribe')).toBeVisible();
    const transcriptsGroup = ribbon.getByTestId('ribbon-action-media.transcribe').locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " act-ribbon-group ")][1]');
    await expect(transcriptsGroup).toHaveCount(1);
    captures.push(await captureCheckpoint(page, { id: 'council-transcribe-entry', root: transcriptsGroup, annotations: [] }, stage));
    expect(captures.map(capture => capture.id)).toEqual(['council-audio-source', 'council-source-details', 'council-compare-entry', 'council-transcribe-entry']);
    return captures;
  });
});

test('docs entry: court sample and Extract fields', async ({ page }) => {
  test.setTimeout(180_000);
  const output = path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'entry-court');
  await publishCaptureBundle(output, { appRevision: process.env.FRISKET_DOCS_REVISION!, sample: { id: 'court-filings', revision: 1 } }, async stage => {
    const captures = [];
    await page.goto('/learn/court-filings?revision=1');
    const card = page.locator('.picker-card');
    await expect(card.getByRole('heading', { name: 'Open Court filings?' })).toBeVisible();
    await expect(card.getByRole('button', { name: 'Open in Frisket' })).toBeVisible();
    captures.push(await captureCheckpoint(page, { id: 'court-open-sample', root: card, annotations: [] }, stage));
    await card.getByRole('button', { name: 'Open in Frisket' }).click();
    const flow = page.getByTestId('pdf-batch-flow');
    await expect(flow).toHaveAttribute('data-stage', 'check', { timeout: 90_000 });
    await flow.getByRole('radio', { name: /^None/ }).click();
    await expect(flow.getByRole('radio', { name: /^None/ })).toBeChecked();
    await expect(flow.getByRole('radio', { name: /^Keep documents/ })).toBeChecked();
    await flow.getByRole('button', { name: 'Continue →' }).click();
    await flow.getByRole('button', { name: 'Search setup →' }).click();
    const semantic = flow.getByRole('checkbox', { name: /Search by meaning/ });
    if (await semantic.isChecked()) await semantic.uncheck();
    await flow.getByRole('button', { name: /^Import/ }).click();
    await expect(flow).toHaveCount(0, { timeout: 90_000 });
    await expect(page.getByTestId('grid')).toBeVisible();
    await page.getByTestId('ribbon-tab-analyze').click();
    const ribbon = page.getByTestId('act-ribbon');
    await expect(ribbon.getByTestId('ribbon-action-map.extract')).toBeVisible();
    const extractGroup = ribbon.getByTestId('ribbon-action-map.extract').locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " act-ribbon-group ")][1]');
    await expect(extractGroup).toHaveCount(1);
    captures.push(await captureCheckpoint(page, { id: 'court-extract-entry', root: extractGroup, annotations: [] }, stage));
    await expect(ribbon.getByTestId('compare-menu-trigger')).toBeVisible();
    const compareGroup = ribbon.getByTestId('compare-menu-trigger').locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " act-ribbon-group ")][1]');
    await expect(compareGroup).toHaveCount(1);
    captures.push(await captureCheckpoint(page, { id: 'court-compare-entry', root: compareGroup, annotations: [] }, stage));
    await page.getByTestId('ribbon-tab-tables').click();
    await expect(ribbon.getByRole('button', { name: 'Join expected answers', exact: true })).toBeVisible();
    const joinGroup = ribbon.getByRole('button', { name: 'Join expected answers', exact: true }).locator('xpath=ancestor::div[contains(concat(" ", normalize-space(@class), " "), " act-ribbon-group ")][1]');
    await expect(joinGroup).toHaveCount(1);
    captures.push(await captureCheckpoint(page, { id: 'court-expected-entry', root: joinGroup, annotations: [] }, stage));
    expect(captures.map(capture => capture.id)).toEqual(['court-open-sample', 'court-extract-entry', 'court-compare-entry', 'court-expected-entry']);
    return captures;
  });
});
