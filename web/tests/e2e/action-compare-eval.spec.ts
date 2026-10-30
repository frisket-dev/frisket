import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createEvaluationScenario, EVALUATION_EXPECTED, EVALUATION_ROW_COUNT, EVALUATION_VARIANTS } from './evaluationScenario';
import { listSheets, openProject, sheetData } from './helpers';

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
