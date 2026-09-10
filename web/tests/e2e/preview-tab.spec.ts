import { expect, test } from '@playwright/test';
import {
  TINY_CSV,
  createProject,
  finishLocalQueuedRun,
  importCsv,
  openAction,
  openProject,
  selectActionInputColumns,
  sheetColumns,
  uniqueName,
} from './helpers';

// Preview is now an IN-MEMORY sample: the app starts a server job that computes a
// small sample (nothing persisted — no op/run/column/cell), polls it while a
// banner shows progress, then overlays the sampled columns/values on the grid.
// Closing drops the in-memory overlay (and cancels the job); the ONLY way a
// preview's output persists is "Run for real", which fires the normal run. We
// drive a local Regex action so the sample is free, deterministic, and produces
// a brand-new `computed` output column.

async function openRegexPreview(page: import('@playwright/test').Page, pid: string, sheetId: number) {
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await openAction(page, 'map.regex_extract');
  await selectActionInputColumns(page, 'snippet');
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await page.getByTestId('field-pattern').fill('^(.+)$');
  await page.getByTestId('field-output-extracted').fill('computed');
  await page.getByTestId('generated-action-preview').click();
  await expect(page.getByTestId('workbench-mainView-previewTab')).toBeVisible();
  await expect(page.getByTestId('preview-tab-banner')).toBeVisible();
}

async function columnNames(request: import('@playwright/test').APIRequestContext, pid: string, sheetId: number) {
  const cols = await sheetColumns(request, pid, sheetId);
  return cols.map((c) => c.name);
}

async function historyTotal(request: import('@playwright/test').APIRequestContext, pid: string) {
  const res = await request.get(`/api/projects/${pid}/history?limit=50`);
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { total: number }).total;
}

test('preview computes an in-memory sample, overlays it, and persists nothing', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('preview-inmemory'));
  const sheetId = await importCsv(request, pid, 'snippets.csv', TINY_CSV);
  const historyBefore = await historyTotal(request, pid);

  await openRegexPreview(page, pid, sheetId);

  // Progress → done: the banner settles on the sample summary once the job
  // finishes (the overlay's sampled columns/values are then on the grid).
  const stats = page.getByTestId('preview-tab-stats');
  await expect(stats).toContainText('Preview · 5 of 5 rows', { timeout: 20_000 });
  // "Run for real" only appears once the sample is done.
  await expect(page.getByTestId('preview-run-for-real-button')).toBeVisible();

  // The whole point: NOTHING was persisted — no `computed` column, no new op.
  expect(await columnNames(request, pid, sheetId)).not.toContain('computed');
  expect(await historyTotal(request, pid)).toBe(historyBefore);

  // Close drops the in-memory overlay + cancels the job; history is untouched.
  // The action drawer stays open beside the grid, but no longer covers the
  // preview banner or its temporary columns.
  await expect(page.getByTestId('action-drawer')).toBeVisible();
  await page.getByTestId('preview-close-button').click();
  await expect(page.getByTestId('workbench-mainView-previewTab')).toHaveCount(0);
  await expect(page.getByTestId('preview-tab-banner')).toHaveCount(0);

  expect(await columnNames(request, pid, sheetId)).not.toContain('computed');
  expect(await historyTotal(request, pid)).toBe(historyBefore);
});

test('preview tab close cancels + drops the sample (nothing persisted)', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('preview-close'));
  const sheetId = await importCsv(request, pid, 'snippets.csv', TINY_CSV);
  const historyBefore = await historyTotal(request, pid);

  await openRegexPreview(page, pid, sheetId);
  await expect(page.getByTestId('preview-tab-stats')).toContainText('Preview · 5 of 5 rows', {
    timeout: 20_000,
  });

  // The tab's own close button (X) drops the preview, same as the banner Close.
  await page.getByTestId('preview-tab-close').click();
  await expect(page.getByTestId('workbench-mainView-previewTab')).toHaveCount(0);
  await expect(page.getByTestId('preview-tab-banner')).toHaveCount(0);

  expect(await columnNames(request, pid, sheetId)).not.toContain('computed');
  expect(await historyTotal(request, pid)).toBe(historyBefore);
});

test('run for real persists the action via the normal run and closes the preview', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('preview-run-for-real'));
  const sheetId = await importCsv(request, pid, 'snippets.csv', TINY_CSV);

  await openRegexPreview(page, pid, sheetId);
  await expect(page.getByTestId('preview-tab-stats')).toContainText('Preview · 5 of 5 rows', {
    timeout: 20_000,
  });
  // Preview alone persisted nothing.
  expect(await columnNames(request, pid, sheetId)).not.toContain('computed');

  // "Run for real" replays the request through the normal (persisted) run.
  const runResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    response.request().method() === 'POST',
  );
  await page.getByTestId('preview-run-for-real-button').click();
  const { run_id: runId } = (await (await runResponse).json()) as { run_id: number | null };
  if (runId == null) throw new Error('regex v1 run did not return a run id');
  finishLocalQueuedRun(pid, runId);
  await expect(page.getByTestId('workbench-mainView-previewTab')).toHaveCount(0);
  await expect(page.getByTestId('preview-tab-banner')).toHaveCount(0);

  await expect
    .poll(() => columnNames(request, pid, sheetId), { timeout: 20_000 })
    .toContain('computed');
});

test('a normal Run also discards the active preview before launching', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('preview-normal-run'));
  const sheetId = await importCsv(request, pid, 'snippets.csv', TINY_CSV);

  await openRegexPreview(page, pid, sheetId);
  await expect(page.getByTestId('preview-tab-stats')).toContainText('Preview · 5 of 5 rows', {
    timeout: 20_000,
  });

  await openAction(page, 'map.regex_extract');
  await selectActionInputColumns(page, 'snippet');
  await page.getByTestId('field-pattern').fill('^(.+)$');
  await page.getByTestId('field-output-extracted').fill('computed');
  const runResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    response.request().method() === 'POST',
  );
  await page.getByTestId('generated-action-run').click();
  const { run_id: runId } = (await (await runResponse).json()) as { run_id: number | null };
  if (runId == null) throw new Error('regex v1 run did not return a run id');
  finishLocalQueuedRun(pid, runId);

  await expect(page.getByTestId('workbench-mainView-previewTab')).toHaveCount(0);
  await expect(page.getByTestId('preview-tab-banner')).toHaveCount(0);
  await expect(page.getByTestId('preview-run-for-real-button')).toHaveCount(0);
  await expect
    .poll(() => columnNames(request, pid, sheetId), { timeout: 20_000 })
    .toContain('computed');
});
