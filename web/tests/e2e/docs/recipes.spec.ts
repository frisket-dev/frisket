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
  copyFileSync(process.env.FRISKET_DOCS_COURT_CACHE!, path.join(process.env.FRISKET_E2E_WS!, `${pid}.frisket`, 'project.cache.db'));
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
  console.log('DOCS_RUN', JSON.stringify(await runResponse.json()));
  await expect(page.getByTestId('run-watcher-toggle')).toHaveAttribute('data-run-status', 'complete', { timeout: 120_000 });
  await expect(page.getByTestId('run-watcher-toggle')).toHaveAttribute('data-run-failed-rows', '0');
});
