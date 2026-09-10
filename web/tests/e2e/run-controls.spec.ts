import { expect, test } from '@playwright/test';
import {
  blockLegacyProjectRun,
  routeV1ActionRun,
  routeV1ActionStatus,
} from './actionStatusFixtures';
import {
  addRow,
  clickCell,
  clickHeader,
  createProject,
  finishLocalQueuedRun,
  importCsv,
  openAction,
  selectActionInputColumns,
  sheetData,
  uniqueName,
} from './helpers';

async function startRegexRun(page: import('@playwright/test').Page, pid: string): Promise<void> {
  await openAction(page, 'map.regex_extract');
  await selectActionInputColumns(page, 'note');
  await expect(page.getByTestId('field-pattern')).toBeVisible();
  await page.getByTestId('field-pattern').fill('\\$[0-9,]+');
  const [, runResponse] = await Promise.all([
    page.waitForRequest((request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      request.method() === 'POST',
    ),
    page.waitForResponse((response) =>
      response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      response.request().method() === 'POST',
    ),
    page.getByTestId('generated-action-run').click(),
  ]);
  const body = await runResponse.json() as { run_id: number | null };
  if (body.run_id == null) throw new Error('regex v1 run did not return a run id');
  finishLocalQueuedRun(pid, body.run_id);
}

async function startSlowPythonRun(page: import('@playwright/test').Page, pid: string): Promise<number> {
  const runId = 9321;
  let status: 'running' | 'cancelled' = 'running';
  await blockLegacyProjectRun(page, pid, 'python should use v1 action endpoint');
  await routeV1ActionRun(page, pid, {
    actionId: 'act-cancel',
    actionKind: 'map.python',
    receiptId: 'receipt-cancel',
    runId,
    status: 'completed',
  });
  await routeV1ActionStatus(page, pid, (matchedRunId) => {
    if (matchedRunId !== runId) return null;
    return {
      actionKind: 'map.python',
      actionName: 'Python',
      projectId: pid,
      runId,
      status,
      total: 80,
      completed: status === 'cancelled' ? 0 : 3,
      failed: 0,
      cost: 0,
      live: status !== 'cancelled',
    };
  });
  await page.route(`**/api/projects/${pid}/actions/runs/${runId}/cancel`, async (route) => {
    status = 'cancelled';
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, run_id: runId, status }),
    });
  });
  await openAction(page, 'map.python');
  await expect(page.getByTestId('field-code')).toBeVisible();
  await page
    .getByTestId('field-code')
    .fill("import time\ntime.sleep(0.5)\nresult = row.get('note', '')");
  const [, runResponse] = await Promise.all([
    page.waitForRequest((request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      request.method() === 'POST',
    ),
    page.waitForResponse((response) =>
      response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      response.request().method() === 'POST',
    ),
    page.getByTestId('generated-action-run').click(),
  ]);
  const body = await runResponse.json() as { run_id: number | null };
  if (body.run_id == null) throw new Error('simulated v1 run did not return a run id');
  return body.run_id;
}

test('run watcher can cancel a queued run', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-cancel'));
  const rows = Array.from({ length: 80 }, (_, i) => `"row ${i} needs slow local work"`).join('\n');
  await importCsv(page.request, pid, 'rows.csv', `note\n${rows}\n`);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const runId = await startSlowPythonRun(page, pid);
  await expect(page.getByTestId('run-progress')).toContainText(/Action:\s*Python/i, {
    timeout: 15_000,
  });

  await page.getByTestId('run-watcher-toggle').click();
  await expect(page.getByTestId('run-cancel-button')).toBeVisible();
  const cancelResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/projects/${pid}/actions/runs/${runId}/cancel`) &&
    response.request().method() === 'POST',
  );
  await page.getByTestId('run-cancel-button').click();
  await cancelResponse;

  await expect(page.getByTestId('run-progress')).toContainText('cancelled', {
    timeout: 15_000,
  });
  const status = await page.evaluate(async ({ projectId, id }) => {
    const response = await fetch(`/api/projects/${projectId}/actions/runs/${id}/status`);
    const body = await response.json();
    return body.run.public_status;
  }, { projectId: pid, id: runId });
  expect(status.status).toBe('cancelled');
});

test('AI column drawer backfills newly incomplete cells', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-backfill'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'rows.csv',
    'note\n"contract $4,200 total"\n"fee was $96 flat"\n',
  );
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await startRegexRun(page, pid);
  await expect(page.getByTestId('run-progress')).toContainText('complete', {
    timeout: 30_000,
  });
  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 2 columns', {
    timeout: 30_000,
  });

  const add = await addRow(page.request, pid, sheetId, { note: 'late invoice $77' });
  expect(add.total).toBe(3);
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await page.reload();
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 2 columns', {
    timeout: 15_000,
  });

  const before = await sheetData(page.request, pid, sheetId, 0, 10);
  const extracted = before.columns.find((c) => c.name === 'extracted');
  expect(extracted).toBeTruthy();
  expect(before.rows[2].meta?.[String(extracted!.id)]).toMatchObject({
    state: 'incomplete',
  });

  const pageErrors: string[] = [];
  page.on('pageerror', (err) => pageErrors.push(err.message));
  await clickCell(page, before.columns, 'extracted', 2);
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('cell-state-extracted')).toHaveText('incomplete');
  expect(pageErrors).toEqual([]);
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('row-drawer')).toBeHidden();

  let legacyBackfillHits = 0;
  await page.route(`**/api/projects/${pid}/backfill`, async (route) => {
    legacyBackfillHits += 1;
    await route.abort('failed');
  });
  await clickHeader(page, before.columns, 'extracted');
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  const [backfillRequest] = await Promise.all([
    page.waitForRequest((request) => {
      if (
        !request.url().includes(`/api/projects/${pid}/actions/v1/run`) ||
        request.method() !== 'POST'
      ) {
        return false;
      }
      return (request.postDataJSON() as { action_id?: string }).action_id === 'run.backfill';
    }),
    page.getByTestId('backfill-column-button').click(),
  ]);
  expect(backfillRequest.postDataJSON()).toMatchObject({
    action_id: 'run.backfill',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { column: 'extracted' },
    output_names: {},
  });
  await expect(page.getByTestId('backfill-result')).toContainText('Filled 1 cell', {
    timeout: 30_000,
  });
  expect(legacyBackfillHits).toBe(0);

  const after = await sheetData(page.request, pid, sheetId, 0, 10);
  expect(after.rows[2].cells[String(extracted!.id)]).toBe('$77');
});
