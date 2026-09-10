// Golden project #1: the tariff country search —
// Dylan's live IRE demo reproduced end-to-end through the real UI. Countries
// CSV → research.web_search with a {{country}} template (keyless ddgs) →
// map.summarize → 0–10 impact score with justification → sort → CSV export.
//
// LIVE MODEL CALLS — no page.route, no mocked model output anywhere (the
// suite's core rule). Requires GEMINI_API_KEY in the backend environment;
// playwright.goldens.config.ts refuses to start without it. Budget: 6 rows ×
// 2 LLM steps on gemini-2.5-flash ≈ $0.003/run; DDG search is free.
//
// Asserts: every row's AI columns populated, scores numeric and
// in 0–10, justification column present and non-empty, export works. The
// search step tolerates one missing row (live DDG), matching the committed
// pytest golden (tests/engine/test_golden_tariff.py).

import fs from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import {
  createProject,
  listSheets,
  openAction,
  openAdvancedSortPanel,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';
import { runViaUi, waitForRun } from './goldenRun';

const COUNTRIES = ['Canada', 'Mexico', 'China', 'Germany', 'Vietnam', 'Brazil'];
const COUNTRIES_CSV = `country\n${COUNTRIES.join('\n')}\n`;

test('tariff country search: CSV → web search → summarize → score → sort → export', async ({ page }, testInfo) => {
  const pid = await createProject(page.request, uniqueName('golden-tariff'));
  await page.goto(`/p/${pid}`);

  // Upload the countries CSV through the empty-state import workspace.
  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
  await page.getByTestId('import-workspace-open').click();
  await expect(page.getByTestId('import-workspace')).toBeVisible();
  await page.getByTestId('import-mode-csv').click();
  await page.getByTestId('import-csv-input').setInputFiles({
    name: 'countries.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(COUNTRIES_CSV),
  });
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('sheet-stats')).toHaveText(`${COUNTRIES.length} rows · 1 column`);
  const sheetId = (await listSheets(page.request, pid))[0].id;

  // Step 1 — per-row web search (keyless ddgs; external-API confirm gate).
  await openAction(page, 'research.web_search');
  await page
    .getByTestId('action-prompt')
    .fill('US tariff impacts on {{country}} economy 2026');
  // The redesigned research form declares its output column (search_results).
  await expect(page.getByTestId('research-output-name')).toContainText('search_results');
  const searchRun = await runViaUi(page, pid);
  // Live DDG: tolerate one missed row, like the pytest golden.
  await waitForRun(page.request, pid, searchRun, 'web_search', 1);

  const afterSearch = await sheetData(page.request, pid, sheetId, 0, 20);
  const colId = (name: string): string => {
    const col = afterSearch.columns.find((c) => c.name === name);
    if (!col) throw new Error(`column "${name}" missing after search`);
    return String(col.id);
  };
  const searchValues = afterSearch.rows.map((r) => r.cells[colId('search_results')]);
  const populatedSearches = searchValues.filter(
    (v) => Array.isArray(v) ? v.length > 0 : typeof v === 'string' && v.length > 0,
  );
  // Live DDG: mirror the pytest golden's tolerance of a single missed row.
  expect(populatedSearches.length).toBeGreaterThanOrEqual(COUNTRIES.length - 1);

  // Step 2 — summarize the search results (LIVE model call, chained input).
  await openAction(page, 'map.summarize');
  await expect(page.getByTestId('model-picker-button')).toContainText('Gemini 3.5 Flash-Lite');
  await page.getByTestId('text-source-columns').click();
  await page.getByTestId('text-source-columns-menu')
    .getByRole('option', { name: /search_results/ })
    .click();
  await page
    .getByTestId('field-instruction')
    .fill('Summarize what these search results say about US tariff impacts on this country in one paragraph.');
  await page.getByTestId('field-output-summary').fill('summary');
  const summarizeRun = await runViaUi(page, pid);
  await waitForRun(page.request, pid, summarizeRun, 'summarize');

  // Step 3 — 0–10 impact score with justification (LIVE model call, chained).
  await openAction(page, 'map.classify');
  await expect(page.getByTestId('model-picker-button')).toContainText('Gemini 3.5 Flash-Lite');
  await page
    .getByTestId('action-prompt')
    .fill('Each row is a country and a summary of search results about US tariff impacts on it. Score the economic impact.');
  await page.getByTestId('new-column-name').fill('tariff_impact');
  await page.getByLabel('Field 1 type').selectOption('integer');
  await page
    .getByLabel('Field 1 description')
    .fill('0 = no economic impact, 10 = severe economic impact from US tariffs');
  const scoreRun = await runViaUi(page, pid);
  await waitForRun(page.request, pid, scoreRun, 'score');

  // --- Assertions (golden #1 contract) ---
  const columns = await sheetColumns(page.request, pid, sheetId);
  const names = columns.map((c) => c.name);
  expect(names).toContain('country');
  expect(names).toContain('search_results');
  expect(names).toContain('summary');
  expect(names).toContain('tariff_impact');
  const justificationCol = columns.find((c) => c.name.includes('justification'));
  if (!justificationCol) {
    throw new Error(`justification column missing; columns: ${names.join(', ')}`);
  }

  const data = await sheetData(page.request, pid, sheetId, 0, 20);
  expect(data.rows.length).toBe(COUNTRIES.length);
  const byName = new Map(columns.map((c) => [c.name, String(c.id)]));
  for (const row of data.rows) {
    const country = row.cells[byName.get('country')!];
    const summary = row.cells[byName.get('summary')!];
    const score = row.cells[byName.get('tariff_impact')!];
    const justification = row.cells[String(justificationCol.id)];
    expect(typeof country, `country in row ${row.id}`).toBe('string');
    // Every row populated: the model steps must not miss any row.
    expect(typeof summary, `summary for ${country}`).toBe('string');
    expect((summary as string).length, `summary for ${country}`).toBeGreaterThan(20);
    expect(typeof score, `score for ${country}`).toBe('number');
    expect(score as number, `score range for ${country}`).toBeGreaterThanOrEqual(0);
    expect(score as number, `score range for ${country}`).toBeLessThanOrEqual(10);
    expect(typeof justification, `justification for ${country}`).toBe('string');
    expect((justification as string).length, `justification for ${country}`).toBeGreaterThan(0);
  }
  // The tip-line property, weak form: major trading partners score meaningfully.
  const scores = data.rows.map((r) => r.cells[byName.get('tariff_impact')!] as number);
  expect(Math.max(...scores)).toBeGreaterThanOrEqual(5);

  // Sort by impact score through the real grid controls.
  await openAdvancedSortPanel(page, columns, 'country');
  await page.getByTestId('grid-sort-column').selectOption('tariff_impact');
  await page.getByTestId('grid-sort-direction').selectOption('desc');
  const sortResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/sheets/${sheetId}/data` &&
      url.searchParams.has('sort')
    );
  });
  await page.getByTestId('apply-grid-sort').click();
  await sortResponse;

  // Export the sorted view as CSV through the project menu; keep it and a
  // final screenshot as run artifacts.
  await page.getByTestId('switch-project').click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  const downloadPromise = page.waitForEvent('download');
  await page.getByTestId('export-menu-open').click();
  await page.getByTestId('export-target-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-scope-current-view').check();
  await page.getByTestId('export-dataset-download').click();
  const download = await downloadPromise;
  const exportPath = testInfo.outputPath('golden-tariff-export.csv');
  await download.saveAs(exportPath);
  const exported = await fs.readFile(exportPath, 'utf8');
  const header = exported.slice(0, exported.indexOf('\n'));
  expect(header).toContain('country');
  expect(header).toContain('tariff_impact');
  for (const country of COUNTRIES) expect(exported).toContain(country);
  // Sorted export: the first data row carries the maximum score.
  const firstDataRow = exported.split('\n')[1] ?? '';
  const topCountry = data.rows
    .map((r) => ({
      country: r.cells[byName.get('country')!] as string,
      score: r.cells[byName.get('tariff_impact')!] as number,
    }))
    .sort((a, b) => b.score - a.score)[0];
  expect(firstDataRow).toContain(topCountry.country);
  await testInfo.attach('golden-tariff-export.csv', { path: exportPath, contentType: 'text/csv' });

  await page.keyboard.press('Escape');
  const screenshotPath = testInfo.outputPath('golden-tariff-final.png');
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await testInfo.attach('golden-tariff-final.png', { path: screenshotPath, contentType: 'image/png' });
});
