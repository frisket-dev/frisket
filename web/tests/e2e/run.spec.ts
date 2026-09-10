// Full run lifecycle in its own project: CSV import → classify (LIVE model
// call: gemini-2.5-flash over 5 rows, well under a cent) → pending state →
// results land → undo/redo.

import { expect, test, type Page } from '@playwright/test';
import {
  TINY_CSV,
  clickCell,
  createProject,
  openAction,
  sheetColumns,
  uniqueName,
} from './helpers';

const LABELS = ['transit', 'money', 'other'];

async function clickRunAndCaptureResponse(page: Page, pid: string) {
  const runButton = page.getByTestId('run-button');
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      await expect(runButton).toBeEnabled({ timeout: 5_000 });
    } catch (error) {
      if (attempt === 3) throw error;
      await page.waitForTimeout(250);
      continue;
    }

    const runResponse = page.waitForResponse((response) => (
      response.request().method() === 'POST' &&
      response.url().includes(`/api/projects/${pid}/actions/v1/run`)
    ), { timeout: 15_000 });
    let clicked = false;
    try {
      await runButton.click({ timeout: 5_000 });
      clicked = true;
      // The classify estimate ($0.0027 over 5 rows) lands above the local
      // confirmation threshold, so the drawer run flow shows the cost gate;
      // confirm it so the run POSTs.
      const gate = page.getByTestId('cost-gate-modal');
      if (await gate.isVisible({ timeout: 2_000 }).catch(() => false)) {
        await page.getByTestId('cost-gate-input').fill('confirm');
        const confirm = page.getByTestId('cost-gate-confirm');
        await expect(confirm).toBeEnabled({ timeout: 5_000 });
        await confirm.click();
      }
      return await runResponse;
    } catch (error) {
      void runResponse.catch(() => undefined);
      if (clicked || attempt === 3) throw error;
      await page.waitForTimeout(250);
    }
  }
  throw new Error('run button never accepted click');
}

test('import → classify run → results → undo/redo', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-run'));
  await page.goto(`/p/${pid}`);

  // Import 5 rows through the empty-state import workspace.
  await expect(page.getByTestId('import-dropzone')).toBeVisible({ timeout: 15_000 });
  await page.getByTestId('import-workspace-open').click();
  await expect(page.getByTestId('import-workspace')).toBeVisible();
  await page.getByTestId('import-csv-input').setInputFiles({
    name: 'snippets.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(TINY_CSV),
  });
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('sheet-stats')).toHaveText('5 rows · 1 columns');

  // Fill the classify action form. The prompt carries a per-run nonce so the
  // server's response cache never short-circuits the run — we want to observe
  // the live pending → complete transition, not a cache replay.
  await openAction(page, 'map.classify');
  await expect(page.getByTestId('action-form')).toBeVisible();
  await expect(page.getByTestId('model-picker-button')).toContainText('Gemini 3.5 Flash-Lite');
  await page
    .getByTestId('action-prompt')
    .fill(`Each row is a one-line local news item. Pick the single best label. (run ${Date.now()})`);
  await page.getByTestId('new-column-name').fill('topic');
  await page.getByLabel('Field 1 labels').fill(LABELS.join(', '));
  const runBody = await (await clickRunAndCaptureResponse(page, pid)).json();
  expect(runBody.schema_version).toBe('frisket.action_result.v1');
  expect(Number.isInteger(Number(runBody.run_id))).toBe(true);

  // Run starts: the watcher opens for the v1 action run, and the output columns
  // appear immediately. Fast cached runs can complete before the spinner paints,
  // so the durable assertion is the watcher + completed status below.
  await expect(page.getByTestId('run-watcher-toggle')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('sheet-stats')).toHaveText(/5 rows · [2-9] columns/);

  // …then real values land.
  await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 60_000 });
  await expect(page.getByTestId('sheet-stats')).toHaveText('5 rows · 4 columns'); // +topic, _justification, _confidence
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const columns = await sheetColumns(page.request, pid, sheets[0].id);
  await clickCell(page, columns, 'topic', 0);
  await page.keyboard.press('Enter');
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText(new RegExp(LABELS.join('|')));
  await expect(drawer.getByTestId('cell-provenance').first()).toContainText('gemini');
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();

  // Undo removes the AI columns; redo restores them.
  await page.getByTestId('undo-button').click();
  await expect(page.getByTestId('sheet-stats')).toHaveText('5 rows · 1 columns');
  await page.getByTestId('redo-button').click();
  await expect(page.getByTestId('sheet-stats')).toHaveText('5 rows · 4 columns');
});
