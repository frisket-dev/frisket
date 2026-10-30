import { mkdirSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createEvaluationScenario, EVALUATION_EXPECTED, EVALUATION_ROW_COUNT, EVALUATION_VARIANTS } from './evaluationScenario';
import { clickGroupHeader, clickHeaderMenu, editCells, listSheets, openProject, sheetData } from './helpers';

test.use({ deviceScaleFactor: 2 });

test('evaluation fixture uses ordinary sheets and strict replay for 38 baseline rows', async ({ page }) => {
  test.setTimeout(120_000);
  const scenario = await createEvaluationScenario(page);
  const sheets = await listSheets(page.request, scenario.projectId);
  expect(sheets.find(sheet => sheet.id === scenario.targetSheetId)?.rows).toBe(EVALUATION_ROW_COUNT);
  expect(sheets.find(sheet => sheet.id === scenario.expectedSheetId)?.rows).toBe(EVALUATION_ROW_COUNT + 2);
  expect(EVALUATION_VARIANTS).toHaveLength(5);
  const plaintiff = scenario.targetColumns.find(column => column.name === 'plaintiff');
  expect(plaintiff).toBeDefined();
  const data = await sheetData(page.request, scenario.projectId, scenario.targetSheetId, 0, EVALUATION_ROW_COUNT);
  expect(data.rows.map(row => row.cells[String(plaintiff!.id)]))
    .toEqual(EVALUATION_VARIANTS[0].rows.map(row => row.plaintiff));
  const expectedPlaintiff = scenario.expectedColumns.find(column => column.name === 'plaintiff');
  const expected = await sheetData(page.request, scenario.projectId, scenario.expectedSheetId, 0, 40);
  expect(expected.rows.slice(0, 38).map(row => row.cells[String(expectedPlaintiff!.id)]))
    .toEqual(EVALUATION_EXPECTED.map(row => row.plaintiff));
  const artifacts = process.env.FRISKET_COMPARE_SCREENSHOTS ?? path.resolve('screenshots/action-compare-connected');
  mkdirSync(artifacts, { recursive: true });
  await openProject(page, scenario.projectId, scenario.targetSheetId);
  await page.screenshot({ path: path.join(artifacts, 'fixture-target-baseline.png'), animations: 'disabled', fullPage: true });
  await openProject(page, scenario.projectId, scenario.expectedSheetId);
  await page.screenshot({ path: path.join(artifacts, 'fixture-expected-ordinary-sheet.png'), animations: 'disabled', fullPage: true });
});

function observeEvaluationScores(page: import('@playwright/test').Page, publish: (score: import('../../src/api/evaluation').EvaluationResponse | null) => void) {
  let newestRequest = 0;
  const order = new WeakMap<import('@playwright/test').Request, number>();
  page.on('request', request => {
    if (request.url().endsWith('/evaluation/score')) order.set(request, ++newestRequest);
  });
  page.on('response', async response => {
    const sequence = order.get(response.request());
    if (sequence === undefined) return;
    const score = response.ok() ? await response.json() : null;
    if (sequence === newestRequest) publish(score);
  });
}

async function capture(page: import('@playwright/test').Page, name: string) {
  const artifacts = process.env.FRISKET_COMPARE_SCREENSHOTS ?? path.resolve('screenshots/action-compare-connected');
  mkdirSync(artifacts, { recursive: true });
  await page.screenshot({ path: path.join(artifacts, name), animations: 'disabled', fullPage: true });
}

async function configureChallenger(page: import('@playwright/test').Page, index: number) {
  const variant = EVALUATION_VARIANTS[index];
  await page.getByRole('button', { name: '+ ⌄', exact: true }).click();
  await page.getByRole('menuitem', { name: 'New variant', exact: true }).click();
  const editor = page.getByRole('complementary', { name: `Configure variant ${variant.key}` });
  await expect(editor).toBeVisible();
  await editor.locator('details.action-advanced > summary').click();
  await editor.getByTestId('field-context').fill(variant.context);
  await editor.getByRole('button', { name: 'Close variant editor' }).click();
}

async function approveComparisonConsent(page: import('@playwright/test').Page, rows?: number) {
  const gate = page.getByTestId('cost-gate-modal');
  await expect(gate).toBeVisible({ timeout: 20_000 });
  if (rows !== undefined) await expect(gate.getByTestId('cost-gate-estimate')).toContainText(`${rows} rows`);
  await gate.getByTestId('cost-gate-confirm').click();
}

async function openExpectedComparison(page: import('@playwright/test').Page) {
  const expand = page.getByTestId('ribbon-expand');
  if (await expand.isVisible()) await expand.click();
  await page.getByTestId('ribbon-tab-analyze').click();
  await page.getByTestId('compare-menu-trigger').click();
  await expect(page.getByRole('menuitem', { name: /vs expected/ })).toBeVisible();
  await capture(page, '07-compare-menu.png');
  await page.getByRole('menuitem', { name: /vs expected/ }).click();
  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible();
}

async function focusComparisonWorkspace(page: import('@playwright/test').Page) {
  const dock = page.getByTestId('bottom-dock-collapse');
  if (await dock.getAttribute('aria-expanded') === 'true') await dock.click();
  const discover = page.getByRole('button', { name: 'Collapse Discover panel', exact: true });
  if (await discover.isVisible()) await discover.click();
  const ribbon = page.getByTestId('ribbon-collapse');
  if (await ribbon.isVisible()) await ribbon.click();
}

async function selectAllExpectedRows(page: import('@playwright/test').Page) {
  await page.getByLabel('Comparison rows', { exact: true }).selectOption('expected');
  await expect(page.getByLabel('Comparison rows', { exact: true })).toHaveValue('expected');
}

async function openFirstComparisonRow(page: import('@playwright/test').Page, expectedLens = true) {
  const grid = page.getByTestId('action-compare-grid');
  await expect(grid.locator('canvas').first()).toBeVisible();
  const box = await grid.boundingBox();
  if (!box) throw new Error('Comparison grid has no visible bounds');
  await page.mouse.dblclick(box.x + 40 + 140 + (expectedLens ? 140 : 0) + 85, box.y + 28 + 34 + 17);
  await expect(page.getByTestId('comparison-review')).toBeVisible();
}

async function expectFullScreenReview(page: import('@playwright/test').Page) {
  const dialog = page.getByRole('dialog', { name: 'Review queue', exact: true });
  await expect(dialog).toBeVisible();
  const box = await dialog.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box).not.toBeNull();
  expect(box!.x).toBe(0);
  expect(box!.y).toBe(0);
  expect(box!.width).toBe(viewport.width);
  expect(box!.height).toBe(viewport.height);
  await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Tab');
  await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  return dialog;
}

async function downloadedText(download: import('@playwright/test').Download) {
  const stream = await download.createReadStream();
  if (!stream) throw new Error('Download produced no readable stream');
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString('utf8');
}

function csvRecords(text: string): Array<Record<string, string>> {
  const python = process.env.FRISKET_E2E_PYTHON ?? path.resolve('..', '.venv/bin/python');
  const result = execFileSync(python, ['-c', 'import csv,io,json,sys; print(json.dumps(list(csv.DictReader(io.StringIO(sys.stdin.read())))))'], { input: text, encoding: 'utf8' });
  return JSON.parse(result);
}

test('join expected answers and compare 38 rows across five variants', async ({ page }) => {
  test.setTimeout(240_000);
  let previewStarts = 0;
  page.on('request', request => { if (request.url().endsWith('/actions/v1/compare-preview') && request.method() === 'POST') previewStarts += 1; });
  let latest: import('../../src/api/evaluation').EvaluationResponse | null = null;
  observeEvaluationScores(page, score => { latest = score; });
  const scenario = await createEvaluationScenario(page);
  await openProject(page, scenario.projectId, scenario.targetSheetId);
  await page.getByTestId('ribbon-tab-tables').click();
  await page.getByRole('button', { name: 'Join expected answers', exact: true }).click();
  const join = page.getByTestId('join-expected-dialog');
  await expect(join).toBeVisible();
  await join.getByLabel('Expected sheet', { exact: true }).selectOption(String(scenario.expectedSheetId));
  await expect(join.getByLabel('Action to check', { exact: true }).locator('option')).toHaveCount(2);
  await join.getByLabel('Action to check', { exact: true }).selectOption({ index: 1 });
  await join.getByRole('button', { name: 'Next', exact: true }).click();
  const expectedColumn = (name: string) => String(scenario.expectedColumns.find(column => column.name === name)!.id);
  const targetColumn = (name: string) => String(scenario.targetColumns.find(column => column.name === name)!.id);
  await join.getByLabel('Expected key 1', { exact: true }).selectOption(expectedColumn('case_id'));
  await join.getByLabel('Target key 1', { exact: true }).selectOption(targetColumn('case_id'));
  await expect(join.getByRole('status')).toContainText('match more than one');
  await expect(join.getByRole('button', { name: 'Next', exact: true })).toBeDisabled();
  await capture(page, '01a-ambiguous-key-next-blocked.png');
  await join.getByRole('button', { name: '+ Another column', exact: true }).click();
  await join.getByLabel('Expected key 2', { exact: true }).selectOption(expectedColumn('county'));
  await join.getByLabel('Target key 2', { exact: true }).selectOption(targetColumn('county'));
  await expect(join.getByRole('status')).toContainText('38 matched · 2 not found');
  await expect(join.getByRole('button', { name: 'Next', exact: true })).toBeEnabled();
  await capture(page, '01-join-expected-answers.png');
  await join.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(join.getByLabel('Match rule for amount')).toHaveValue('exact');
  await join.getByLabel('Match rule for parties').selectOption('set');
  await join.getByLabel('Match rule for summary').selectOption('hand');
  await expect(join.getByRole('button', { name: 'Join', exact: true })).toBeEnabled();
  await capture(page, '01b-join-columns.png');
  await join.getByRole('button', { name: 'Join', exact: true }).click();
  await expect(join).toHaveCount(0);
  await expect(page.getByRole('button', { name: '◉ Expected answers · Hide', exact: true })).toBeVisible();
  await capture(page, '02-expected-columns-in-sheet.png');
  const projectedColumns = scenario.targetColumns.flatMap(column => {
    const expected = scenario.expectedColumns.find(candidate => candidate.name === column.name);
    return expected && ['plaintiff', 'amount', 'parties', 'summary'].includes(column.name)
      ? [column, { ...expected, name: `Expected · ${column.name}` }] : [column];
  });
  await clickHeaderMenu(page, projectedColumns, 'Expected · plaintiff');
  const expectedMenu = page.getByRole('dialog', { name: 'Expected column', exact: true });
  await expect(expectedMenu).toBeVisible();
  await capture(page, '02b-expected-column-menu.png');
  await expectedMenu.getByRole('button', { name: 'Close', exact: true }).click();
  await clickGroupHeader(page, scenario.targetColumns, 'plaintiff', 'summary');
  const entry = page.getByTestId('action-run-entry-card');
  await expect(entry.getByRole('button', { name: '✓ Check against expected', exact: true })).toBeVisible();
  await capture(page, '08-entries-from-data.png');
  await entry.getByRole('button', { name: '✓ Check against expected', exact: true }).click();
  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible();
  console.log('evaluation milestone: ordinary composite-key join created');

  await openExpectedComparison(page);
  await expect.poll(() => latest?.row_ids.length).toBe(20);
  await expect(page.getByLabel('Comparison rows', { exact: true })).toHaveValue('sample');
  await selectAllExpectedRows(page);
  await expect.poll(() => latest?.row_ids.length).toBe(38);
  await focusComparisonWorkspace(page);
  await expect.poll(() => latest?.variants.length).toBe(1);
  await expect(page.getByRole('button', { name: 'Each other', exact: true })).toBeDisabled();
  await expect(page.getByText('B · try to beat A', { exact: true })).toBeVisible();
  await capture(page, '09-comparison-of-one.png');
  for (let index = 1; index < EVALUATION_VARIANTS.length; index += 1) await configureChallenger(page, index);
  await page.getByRole('button', { name: 'Run comparison', exact: true }).click();
  await approveComparisonConsent(page, 152);
  for (const key of ['B', 'C', 'D', 'E']) await expect(page.getByTestId(`action-compare-variant-${key}`)).toHaveAttribute('data-status', 'ready', { timeout: 90_000 });
  await expect.poll(() => latest?.variants.length, { timeout: 20_000 }).toBe(5);
  await expect.poll(() => latest?.best.plaintiff).toEqual(['D', 'E']);
  expect(latest!.variants.every(variant => variant.scores.plaintiff.attempted === 38)).toBe(true);
  expect(previewStarts).toBe(8);
  await capture(page, '12-five-variants.png');
  await page.setViewportSize({ width: 1024, height: 900 });
  await capture(page, '12b-five-variants-1024.png');
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.getByRole('button', { name: 'Use D…', exact: true }).click();
  const replacement = page.getByTestId('generated-action-form');
  await expect(replacement.getByRole('button', { name: 'Replace columns', exact: true })).toBeVisible();
  await capture(page, '12c-use-best-replace-form.png');
  await replacement.getByRole('button', { name: 'Choose run scope', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Return to Compare', exact: true }).click();
  await expect(replacement).toHaveCount(0);
  console.log('evaluation milestone: five variants scored on all 38 rows, exact D/E tie');

  await page.getByRole('group', { name: 'Comparison field' }).getByRole('button', { name: 'amount', exact: true }).click();
  expect(latest!.variants[0].scores.amount.checked).toBe(37);
  await page.getByRole('button', { name: 'Exact ▾', exact: true }).click();
  const ruleDialog = page.getByTestId('comparison-match-rule');
  await expect(ruleDialog).toBeVisible();
  await capture(page, '04-change-match-rule.png');
  await ruleDialog.getByRole('radio', { name: /Same number/ }).click();
  await expect(ruleDialog.getByRole('radio', { name: /Same number/ })).toBeChecked();
  await expect.poll(() => latest?.link?.fields.find(field => field.logical_key === 'amount')?.rule).toBe('number');
  await expect.poll(() => latest?.variants[0].scores.amount.accuracy).toBe(1);
  expect(previewStarts).toBe(8);
  await page.getByRole('button', { name: 'Close match rule', exact: true }).click();
  await capture(page, '03-compare-against-expected.png');
  await page.getByRole('button', { name: 'Results', exact: true }).click();
  await expect(page.getByTestId('comparison-results')).toBeVisible();
  await capture(page, '05-results.png');
  await page.getByRole('button', { name: 'Close results', exact: true }).click();

  await page.getByRole('group', { name: 'Comparison field' }).getByRole('button', { name: 'plaintiff', exact: true }).click();
  const comparisonGrid = await page.getByTestId('action-compare-grid').elementHandle();
  await openFirstComparisonRow(page);
  await expectFullScreenReview(page);
  expect(await comparisonGrid!.evaluate(element => element.isConnected)).toBe(true);
  await expect(page.getByTestId('action-compare-toolbar')).toHaveCount(1);
  await expect(page.getByTestId('comparison-review').getByRole('button', { name: /Answer 1, variants A, B, C/ })).toBeVisible();
  await expect(page.getByTestId('comparison-review').getByRole('button', { name: 'Accept answer 1', exact: true })).toBeDisabled();
  await expect(page.getByTestId('comparison-review')).toContainText('Public notice 1: Reporter 1 v Agency 1.');
  await expect(page.getByText('Loading cited sources…', { exact: true })).toHaveCount(0);
  await capture(page, '13a-inspect-grouped-misses.png');
  await page.setViewportSize({ width: 1024, height: 900 });
  await expectFullScreenReview(page);
  await capture(page, '13c-comparison-review-1024.png');
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.getByRole('button', { name: 'Close comparison review', exact: true }).focus();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Review queue', exact: true })).toHaveCount(0);
  expect(await comparisonGrid!.evaluate(element => element.isConnected)).toBe(true);

  await page.getByRole('group', { name: 'Comparison field' }).getByRole('button', { name: 'summary', exact: true }).click();
  await page.getByRole('button', { name: /to score by hand/ }).click();
  const review = page.getByTestId('comparison-review');
  await expect(review).toBeVisible();
  await expectFullScreenReview(page);
  await expect(review.getByRole('button', { name: /Answer 1, variants A, B, E/ })).toBeVisible();
  await expect(review.getByRole('button', { name: /Answer 2, variants C, D/ })).toBeInViewport();
  await expect(review.getByRole('button', { name: /Answer 1, variants A, B, E/ })).toBeInViewport();
  await expect(review.getByRole('button', { name: 'Accept answer 2', exact: true })).toBeEnabled();
  await expect(review.getByText('Loading full expected answer…', { exact: true })).toHaveCount(0);
  await capture(page, '13b-long-answer-reader.png');
  await review.getByRole('button', { name: 'Reject answer 1', exact: true }).click();
  await expect.poll(() => latest?.variants.find(variant => variant.key === 'A')?.scores.summary.scored).toBe(1);
  expect(latest!.variants.find(variant => variant.key === 'B')!.scores.summary.misses).toBe(1);
  expect(latest!.variants.find(variant => variant.key === 'E')!.scores.summary.misses).toBe(1);
  await review.getByRole('button', { name: 'Accept answer 2', exact: true }).click();
  await expect.poll(() => latest?.variants.find(variant => variant.key === 'D')?.scores.summary.scored).toBe(1);
  await expect(review.getByRole('button', { name: /Answer 1, variants A, B, E, To score/ })).toBeVisible();
  await capture(page, '13-grouped-answers.png');
  await review.getByRole('button', { name: 'Edit expected answer', exact: true }).click();
  await review.getByLabel('Expected answer', { exact: true }).fill('A corrected public expected narrative.');
  await expect.poll(async () => {
    const data = await sheetData(page.request, scenario.projectId, scenario.expectedSheetId, 0, 2);
    return data.rows[1].cells[expectedColumn('summary')];
  }).toBe('A corrected public expected narrative.');
  await capture(page, '05b-hand-scoring-and-live-edit.png');
  const pendingEdit = 'Pending edit saved when comparison Review closes.';
  await review.getByLabel('Expected answer', { exact: true }).fill(pendingEdit);
  await page.getByRole('button', { name: 'Close comparison review', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Review queue', exact: true })).toHaveCount(0);
  expect(await comparisonGrid!.evaluate(element => element.isConnected)).toBe(true);
  await expect(page.getByLabel('Comparison rows', { exact: true })).toHaveValue('expected');
  await expect(page.getByRole('group', { name: 'Comparison field' }).getByRole('button', { name: 'summary', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(async () => (await sheetData(page.request, scenario.projectId, scenario.expectedSheetId, 0, 2)).rows[1].cells[expectedColumn('summary')]).toBe(pendingEdit);
  expect(previewStarts).toBe(8);
  console.log('evaluation milestone: grouped hand scores reuse only the same row; expected edits write ordinary sheet');

  const [csvDownload] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '⤓ CSV', exact: true }).click()]);
  const csvText = await downloadedText(csvDownload);
  const downloadArtifacts = process.env.FRISKET_COMPARE_SCREENSHOTS ?? path.resolve('screenshots/action-compare-connected');
  mkdirSync(downloadArtifacts, { recursive: true });
  writeFileSync(path.join(downloadArtifacts, 'comparison.csv'), csvText);
  const records = csvRecords(csvText);
  expect(records).toHaveLength(38);
  expect(records[0].summary_C).toBe(EVALUATION_EXPECTED[0].summary);
  expect(records[0].summary_expected).toBe(EVALUATION_EXPECTED[0].summary);
  expect(records[0].summary_A_match).toBe('no');
  expect(records[36].amount_A_match).toBe('');
  expect(records[37].amount_A).toBe('');
  expect(records[37].amount_A_match).toBe('yes');
  const [settingsDownload] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: 'Settings', exact: true }).click()]);
  const settingsText = await downloadedText(settingsDownload);
  writeFileSync(path.join(downloadArtifacts, 'comparison-settings.json'), settingsText);
  const settings = JSON.parse(settingsText);
  expect(settings.schema_version).toBe('frisket.comparison_settings.v1');
  expect(settings.variants).toHaveLength(5);
  expect(settings.row_ids).toHaveLength(38);
  await capture(page, '06-download.png');

  await page.getByRole('group', { name: 'Comparison field' }).getByRole('button', { name: 'plaintiff', exact: true }).click();
  const subset = await page.request.post(`/api/projects/${scenario.projectId}/evaluation/shelf`, { data: {
    comparison: { basis: { kind: 'run', run_id: scenario.baselineRunId, column_id: Number(targetColumn('plaintiff')) },
      row_ids: scenario.rowIds.slice(1), link_id: latest!.link!.id,
      variants: [{ key: 'A', label: 'Earlier subset', source: { kind: 'current' } }] }, variant_keys: ['A'],
  } });
  expect(subset.ok()).toBeTruthy();
  await page.getByRole('button', { name: '🗄 Shelve', exact: true }).click();
  await expect(page.getByTestId('action-compare-toolbar')).toHaveCount(0);
  const firstExpectedRow = (await sheetData(page.request, scenario.projectId, scenario.expectedSheetId, 0, 1)).rows[0];
  await editCells(page.request, scenario.projectId, [{ rowId: firstExpectedRow.id, columnId: Number(expectedColumn('plaintiff')), value: EVALUATION_VARIANTS[0].rows[0].plaintiff }]);
  await openExpectedComparison(page);
  await selectAllExpectedRows(page);
  await expect.poll(() => latest?.row_ids.length).toBe(38);
  await page.getByRole('button', { name: '+ ⌄', exact: true }).click();
  await capture(page, '15a-from-the-shelf-menu.png');
  await page.getByRole('menuitem', { name: 'Manage shelf…', exact: true }).click();
  await expect(page.getByTestId('comparison-manage-shelf')).toBeVisible();
  await capture(page, '15b-manage-shelf.png');
  await page.getByRole('button', { name: 'Close shelf', exact: true }).click();
  await page.getByRole('button', { name: '+ ⌄', exact: true }).click();
  const shelfChoice = page.getByRole('menuitem').filter({ hasText: 'Current values' }).filter({ hasText: '38 rows' });
  await expect(shelfChoice).toBeVisible();
  await shelfChoice.click();
  await expect(page.getByLabel(/shelved, read-only/)).toBeVisible();
  await expect.poll(() => latest?.variants.length).toBe(2);
  await expect.poll(() => latest?.variants.find(variant => variant.key === 'S')?.scores.plaintiff.matches).toBe(29);
  expect(previewStarts).toBe(8);
  await capture(page, '15-shelf.png');
  await page.getByRole('button', { name: '+ ⌄', exact: true }).click();
  await page.getByRole('menuitem').filter({ hasText: 'Earlier subset' }).click();
  await expect.poll(() => latest?.variants.find(variant => variant.key === 'S2')?.verdicts[String(scenario.rowIds[0])]?.plaintiff).toBe('not_run');
  await capture(page, '15c-restored-missing-row.png');
  console.log('evaluation milestone: full CSV and separate metadata downloaded; shelf restored read-only');
});

test('disagreement rejection stays unchecked and acceptance creates an ordinary expected sheet', async ({ page }) => {
  test.setTimeout(180_000);
  let latest: import('../../src/api/evaluation').EvaluationResponse | null = null;
  let expectedWrites = 0;
  page.on('request', request => { if (request.url().endsWith('/evaluation/expected-answer') && request.method() === 'POST') expectedWrites += 1; });
  observeEvaluationScores(page, score => { latest = score; });
  const scenario = await createEvaluationScenario(page);
  await openProject(page, scenario.projectId, scenario.targetSheetId);
  await page.getByTestId('ribbon-tab-analyze').click();
  await page.getByTestId('compare-menu-trigger').click();
  await page.getByRole('menuitem').filter({ hasText: 'Try other models or instructions' }).click();
  await expect(page.getByTestId('action-compare-toolbar')).toBeVisible();
  await focusComparisonWorkspace(page);
  await page.getByRole('button', { name: '✓ Expected answers', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Join expected answers…', exact: true })).toBeVisible();
  await capture(page, '10-no-answers-yet.png');
  await page.getByRole('button', { name: 'Join expected answers…', exact: true }).click();
  const setup = page.getByTestId('join-expected-dialog');
  await expect(setup.getByLabel('Action to check', { exact: true })).not.toHaveValue('');
  await capture(page, '10b-action-prefilled-join.png');
  await setup.getByRole('button', { name: 'Close', exact: true }).click();
  await configureChallenger(page, 1);
  await page.getByRole('button', { name: 'Run comparison', exact: true }).click();
  await approveComparisonConsent(page);
  await expect(page.getByTestId('action-compare-variant-B')).toHaveAttribute('data-status', 'ready', { timeout: 90_000 });
  await expect.poll(() => latest?.variants.length).toBe(2);
  expect(latest!.link).toBeNull();
  await page.getByRole('button', { name: 'Each other', exact: true }).click();
  await page.getByRole('button', { name: /^Variants disagree ·/ }).click();
  const review = page.getByTestId('comparison-review');
  await expect(review).toBeVisible();
  await expect(review.getByLabel('Expected answer', { exact: true })).toHaveValue('');
  await review.getByRole('button', { name: 'Reject answer 1', exact: true }).click();
  expect(expectedWrites).toBe(0);
  await expect(review.getByLabel('Expected answer', { exact: true })).toHaveValue('');
  await capture(page, '14a-disagreement-rejected-unchecked.png');
  await review.getByRole('button', { name: 'Accept answer 2', exact: true }).click();
  await expect.poll(() => latest?.link?.expected_sheet_name).toBe('public-notices · expected');
  await expect.poll(() => expectedWrites).toBe(1);
  await expect.poll(async () => (await listSheets(page.request, scenario.projectId)).length).toBe(3);
  await expect(page.getByRole('tab', { name: 'public-notices · expected', exact: true })).toBeVisible();
  await expect(review.getByLabel('Expected answer', { exact: true })).not.toHaveValue('');
  await capture(page, '14-disagreement-queue.png');
  expect(latest!.link!.fields[0].rule).toBe('exact');
  expect(latest!.link!.fields[0].source).toBe('disagreements');
  await page.getByRole('button', { name: 'Close comparison review', exact: true }).click();
  await page.getByRole('button', { name: '✓ Expected answers', exact: true }).click();
  await expect(page.getByTestId('action-compare-grid')).toBeVisible();
  await expect(page.getByText('Scoring…', { exact: true })).toHaveCount(0);
  await capture(page, '14b-expected-header-from-disagreements.png');
  console.log('evaluation milestone: rejection wrote no expected answer; acceptance created visible ordinary expected sheet');
});

test('Try several preserves engine and model selection and scores the chosen field', async ({ page }) => {
  test.setTimeout(180_000);
  let latest: import('../../src/api/evaluation').EvaluationResponse | null = null;
  observeEvaluationScores(page, score => { latest = score; });
  const scenario = await createEvaluationScenario(page, { alternateModel: true });
  const targetColumn = (name: string) => scenario.targetColumns.find(column => column.name === name)!.id;
  const expectedColumn = (name: string) => scenario.expectedColumns.find(column => column.name === name)!.id;
  const linked = await page.request.post(`/api/projects/${scenario.projectId}/evaluation/links`, { data: {
    target_sheet_id: scenario.targetSheetId, expected_sheet_id: scenario.expectedSheetId, action_id: 'map.extract',
    keys: ['case_id', 'county'].map(name => ({ expected_column_id: expectedColumn(name), target_column_id: targetColumn(name) })),
    fields: ['plaintiff', 'amount', 'parties', 'summary'].map(name => ({ expected_column_id: expectedColumn(name), output_column_id: targetColumn(name),
      logical_key: name, rule: name === 'summary' ? 'hand' : name === 'parties' ? 'set' : 'exact', hidden: false, source: 'import' })), hidden: false,
  } });
  expect(linked.ok()).toBeTruthy();
  await openProject(page, scenario.projectId, scenario.targetSheetId);
  await openExpectedComparison(page);
  await expect.poll(() => latest?.row_ids.length).toBe(20);
  await selectAllExpectedRows(page);
  await expect.poll(() => latest?.row_ids.length).toBe(38);
  await focusComparisonWorkspace(page);
  await page.getByRole('button', { name: 'Try several…', exact: true }).click();
  const matrix = page.getByRole('region', { name: 'Try several values', exact: true });
  await expect(matrix).toBeInViewport();
  await matrix.getByRole('button', { name: 'Add model', exact: true }).click();
  const modelChoices = matrix.getByRole('group', { name: 'Models', exact: true }).getByRole('button');
  await expect(modelChoices).toHaveCount(2);
  await modelChoices.nth(1).click();
  await page.getByTestId('engine-selector-dialog').getByRole('button', { name: /Claude Haiku 4.5/ }).click();
  await expect(modelChoices.nth(1)).toContainText('Claude Haiku 4.5');
  await expect(matrix.getByLabel('Score on', { exact: true }).locator('option[value="summary"]')).toBeDisabled();
  await capture(page, '11a-try-several-setup.png');
  await matrix.getByRole('button', { name: 'Run 2 combinations', exact: true }).click();
  await approveComparisonConsent(page, 76);
  for (const key of ['B', 'C']) await expect(page.getByTestId(`action-compare-variant-${key}`)).toHaveAttribute('data-status', 'ready', { timeout: 90_000 });
  await expect.poll(() => latest?.variants.length).toBe(3);
  await expect.poll(() => latest?.best.plaintiff).toEqual(['C']);
  const selected = latest!.variants.find(variant => variant.key === 'C')!;
  expect((selected.settings as { params: { model: string } }).params.model).toBe('anthropic/claude-haiku-4-5');
  expect(selected.scores.plaintiff.matches).toBe(38);
  expect(latest!.variants.find(variant => variant.key === 'B')!.scores.plaintiff.matches).toBe(28);
  await expect(matrix.getByText('Show setup', { exact: true })).toBeVisible();
  await expect(matrix.getByRole('button', { name: 'Add model', exact: true })).toBeHidden();
  await expect(matrix.getByTestId('try-several-grid')).toBeInViewport();
  await expect(matrix.getByRole('button', { name: 'Use best', exact: true })).toBeInViewport();
  await capture(page, '11-try-several-scored.png');
  await page.setViewportSize({ width: 1024, height: 900 });
  await expect(matrix.getByRole('button', { name: 'Use best', exact: true })).toBeInViewport();
  await capture(page, '11c-try-several-results-1024.png');
  await page.setViewportSize({ width: 1600, height: 900 });
  await matrix.getByRole('button', { name: 'Use best', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Try several values', exact: true })).toHaveCount(0);
  const replacement = page.getByTestId('generated-action-form');
  await expect(replacement.getByRole('button', { name: 'Replace columns', exact: true })).toBeVisible();
  await expect(replacement.getByTestId('engine-selector')).toContainText('Claude Haiku 4.5');
  await capture(page, '11b-try-several-use-best.png');
  console.log('evaluation milestone: model matrix preserved full engine/model selection, scored one field and opened Replace');
});


test('ordinary Review keeps its full-screen decisions and saves a pending row note on close', async ({ page }) => {
  test.setTimeout(120_000);
  const scenario = await createEvaluationScenario(page);
  await openProject(page, scenario.projectId, scenario.targetSheetId);
  await page.getByTestId('review-queue-button').click();
  const dialog = await expectFullScreenReview(page);
  const plaintiff = dialog.getByTestId('review-field-plaintiff');
  await expect(plaintiff).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Edit plaintiff', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Accept plaintiff', exact: true }).click();
  await expect(plaintiff).toHaveAttribute('data-review-state', 'verified');
  await capture(page, '16-ordinary-review.png');
  await page.setViewportSize({ width: 1024, height: 900 });
  await expectFullScreenReview(page);
  await capture(page, '16b-ordinary-review-1024.png');
  const note = 'Ordinary Review retains its own row note and yes/no decisions.';
  await dialog.getByTestId('review-note-input').fill(note);
  await dialog.getByRole('button', { name: 'Close review queue', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect.poll(async () => {
    const response = await page.request.get(`/api/projects/${scenario.projectId}/review/bundles?run_id=${scenario.baselineRunId}&include_reviewed=true&limit=100`);
    return (await response.json()).bundles.some((bundle: { review_note: string | null }) => bundle.review_note === note);
  }).toBe(true);
  const links = await page.request.get(`/api/projects/${scenario.projectId}/evaluation/links?sheet_id=${scenario.targetSheetId}`);
  expect(links.ok()).toBeTruthy();
  expect((await links.json()).links).toHaveLength(0);
});
