// Golden project #2: NTSB lat/lon extraction from
// real public-domain aviation final-report PDFs, end-to-end through the real
// UI: import PDFs → media.to_markdown (local markitdown) → map.extract with a
// list-of-dicts schema {lat: number, lon: number, context: text} → verify
// against the hand-labeled answer key (tests/fixtures/ntsb/answer_key.json,
// labeled from the literal report text).
//
// LIVE MODEL CALLS — no page.route, no mocked model output. Requires
// GEMINI_API_KEY (enforced by playwright.goldens.config.ts) and the `convert`
// extra in the backend env (markitdown for PDF → markdown). Budget: 6 rows ×
// 1 extract call on gemini-2.5-flash ≈ $0.002/run.
//
// Asserts (the strongest deterministic checks in the
// suite): numeric types, plausible coordinate ranges, found-vs-missed against
// the answer key.

import fs from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import {
  createProject,
  listSheets,
  openAction,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';
import { runViaUi, waitForRun } from './goldenRun';

// Playwright's cwd is web/; the fixture set lives in the repo's tests tree.
const FIXTURE_DIR = path.resolve(process.cwd(), '../tests/fixtures/ntsb');

interface AnswerKeyReport {
  file: string;
  ntsb_no: string;
  lat: number | null;
  lon: number | null;
  has_coordinates: boolean;
}

// Degrees of tolerance when matching an extracted pair against the key. The
// key is exact (copied from the report text); the extraction may round.
const COORD_TOLERANCE = 0.02;

test('NTSB reports: PDFs → markdown → structured lat/lon extraction vs answer key', async ({ page }, testInfo) => {
  const answerKey = JSON.parse(
    fs.readFileSync(path.join(FIXTURE_DIR, 'answer_key.json'), 'utf8'),
  ) as { reports: AnswerKeyReport[] };
  expect(answerKey.reports.length).toBeGreaterThanOrEqual(5);

  const pid = await createProject(page.request, uniqueName('golden-ntsb'));
  await page.goto(`/p/${pid}`);

  // Import the report PDFs through the import workspace (Files mode: one
  // media row per report).
  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
  await page.getByTestId('import-workspace-open').click();
  await expect(page.getByTestId('import-workspace')).toBeVisible();
  await page.getByTestId('import-mode-files').click();
  await page.getByTestId('import-file-input').setInputFiles(
    answerKey.reports.map((r) => path.join(FIXTURE_DIR, r.file)),
  );
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 30_000 });
  const sheetId = (await listSheets(page.request, pid))[0].id;
  await expect(page.getByTestId('sheet-stats')).toContainText(`${answerKey.reports.length} rows`);

  // Step 1 — convert each PDF to markdown (local engine, no model call).
  await openAction(page, 'media.to_markdown');
  await expect(page.getByTestId('field-source')).toHaveValue('media');
  const toMarkdownRun = await runViaUi(page, pid);
  await waitForRun(page.request, pid, toMarkdownRun, 'to_markdown');

  const afterConvert = await sheetColumns(page.request, pid, sheetId);
  const markdownCol = afterConvert.find((c) => c.name === 'markdown');
  if (!markdownCol) {
    throw new Error(
      `to_markdown produced no markdown column; columns: ${afterConvert.map((c) => c.name).join(', ')}`,
    );
  }

  // Step 2 — structured extraction (LIVE model call): list of
  // {lat, lon, context} dicts per report. This golden exists to show
  // structured > freeform; the schema is the point.
  await openAction(page, 'map.extract');
  await expect(page.getByTestId('model-picker-button')).toContainText('Gemini 3.5 Flash-Lite');
  await page.getByTestId('text-source-column-select').selectOption('markdown');
  // Extract's output columns ARE its fields: reshape the template's three
  // default fields into one list field named `locations`.
  await page.getByLabel('Remove Field 3').click();
  await page.getByLabel('Remove Field 2').click();
  await page.getByLabel('Field 1 name').fill('locations');
  await page.getByLabel('Field 1 type').selectOption('list');
  await expect(page.getByTestId('list-item-schema')).toBeVisible();
  // Shape of each item: lat (number), lon (number), context (text). Selecting
  // the object item mode seeds one field row; add the other two.
  await page.getByTestId('list-item-kind').selectOption('object');
  await page.getByTestId('list-item-field-name').nth(0).fill('lat');
  await page.getByTestId('list-item-field-type').nth(0).selectOption('number');
  await page.getByTestId('list-item-field-description').nth(0)
    .fill('Latitude in signed decimal degrees (north positive)');
  await page.getByTestId('list-item-field-add').click();
  await page.getByTestId('list-item-field-name').nth(1).fill('lon');
  await page.getByTestId('list-item-field-type').nth(1).selectOption('number');
  await page.getByTestId('list-item-field-description').nth(1)
    .fill('Longitude in signed decimal degrees (west negative)');
  await page.getByTestId('list-item-field-add').click();
  await page.getByTestId('list-item-field-name').nth(2).fill('context');
  await page.getByTestId('list-item-field-description').nth(2)
    .fill('The sentence or field where the coordinates appear');
  await page
    .getByTestId('action-prompt')
    .fill(
      'This is an NTSB aviation accident report. Extract every geographic '
      + 'coordinate pair (latitude/longitude) stated in the report as signed '
      + 'decimal degrees. Do not invent coordinates: if none are stated, '
      + 'return an empty list.',
    );
  const extractRun = await runViaUi(page, pid);
  await waitForRun(page.request, pid, extractRun, 'extract');

  // --- Assertions ---
  const columns = await sheetColumns(page.request, pid, sheetId);
  const locationsCol = columns.find((c) => c.name === 'locations');
  if (!locationsCol) {
    throw new Error(`locations column missing; columns: ${columns.map((c) => c.name).join(', ')}`);
  }
  const data = await sheetData(page.request, pid, sheetId, 0, 20);
  expect(data.rows.length).toBe(answerKey.reports.length);

  // Match each row back to its answer-key report via the imported file name
  // (any cell whose text contains the NTSB number).
  const misses: string[] = [];
  let found = 0;
  for (const report of answerKey.reports) {
    const row = data.rows.find((candidate) =>
      Object.values(candidate.cells).some(
        (v) => typeof v === 'string' && v.includes(report.ntsb_no),
      ),
    );
    if (!row) throw new Error(`no imported row matched report ${report.ntsb_no}`);
    const value = row.cells[String(locationsCol.id)];
    const items = Array.isArray(value) ? (value as Array<Record<string, unknown>>) : [];

    // Numeric types + plausible ranges for EVERY extracted item.
    for (const item of items) {
      expect(typeof item.lat, `${report.ntsb_no} lat type`).toBe('number');
      expect(typeof item.lon, `${report.ntsb_no} lon type`).toBe('number');
      const lat = item.lat as number;
      const lon = item.lon as number;
      expect(lat, `${report.ntsb_no} lat range`).toBeGreaterThanOrEqual(-90);
      expect(lat, `${report.ntsb_no} lat range`).toBeLessThanOrEqual(90);
      expect(lon, `${report.ntsb_no} lon range`).toBeGreaterThanOrEqual(-180);
      expect(lon, `${report.ntsb_no} lon range`).toBeLessThanOrEqual(180);
    }

    if (!report.has_coordinates || report.lat === null || report.lon === null) {
      // Negative example: the model must not hallucinate a location.
      expect(items.length, `${report.ntsb_no} should have no coordinates`).toBe(0);
      continue;
    }
    const hit = items.some(
      (item) =>
        Math.abs((item.lat as number) - report.lat!) <= COORD_TOLERANCE &&
        Math.abs((item.lon as number) - report.lon!) <= COORD_TOLERANCE,
    );
    if (hit) found += 1;
    else misses.push(`${report.ntsb_no} (expected ${report.lat}, ${report.lon}, got ${JSON.stringify(items)})`);
  }

  // Found-vs-missed against the hand-labeled key: at most one miss.
  const withCoords = answerKey.reports.filter((r) => r.has_coordinates).length;
  expect(
    found,
    `found ${found}/${withCoords}; misses:\n${misses.join('\n')}`,
  ).toBeGreaterThanOrEqual(withCoords - 1);

  const screenshotPath = testInfo.outputPath('golden-ntsb-final.png');
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await testInfo.attach('golden-ntsb-final.png', { path: screenshotPath, contentType: 'image/png' });
});
