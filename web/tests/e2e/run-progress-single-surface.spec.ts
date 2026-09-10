// Active run progress should have one prominent watcher surface. The footer may
// show compact status, but it must not auto-open a competing popover.

import { expect, test, type Page } from '@playwright/test';
import { routeV1ActionRun, routeV1ActionStatus } from './actionStatusFixtures';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

type RunStatus = 'queued' | 'running' | 'stalled' | 'orphaned' | 'complete' | 'failed' | 'cancelled';

interface RunStatusOverride {
  status: RunStatus;
  total: number;
  completed: number;
  failed: number;
  cost: number | null;
  live: boolean;
}

async function startSimulatedPythonRun(page: Page, pid: string, sleepSeconds = 0.2): Promise<number> {
  const runId = 9311 + Math.round(sleepSeconds * 10);
  await routeV1ActionRun(page, pid, {
    actionId: `act-progress-${runId}`,
    actionKind: 'map.python',
    receiptId: `receipt-progress-${runId}`,
    runId,
    status: 'completed',
  });
  await openAction(page, 'map.python');
  await expect(page.getByTestId('field-code')).toBeVisible();
  await page
    .getByTestId('field-code')
    .fill(`import time\ntime.sleep(${sleepSeconds})\nresult = row.get('note', '')`);
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
  const body = (await runResponse.json()) as { run_id: number | null };
  if (body.run_id == null) throw new Error('simulated v1 run did not return a run id');
  return body.run_id;
}

async function installRunStatusOverride(page: Page, pid: string) {
  let runId: number | null = null;
  let override: RunStatusOverride | null = null;
  await routeV1ActionStatus(page, pid, (matchedRunId) => {
    if (override === null || runId === null || matchedRunId !== runId) {
      return null;
    }
    return {
      actionKind: 'map.python',
      actionName: 'Python',
      projectId: pid,
      runId,
      ...override,
    };
  });
  return {
    bind(nextRunId: number) {
      runId = nextRunId;
    },
    async set(next: RunStatusOverride) {
      if (runId === null) throw new Error('run status override installed before bind()');
      const response = page.waitForResponse((resp) =>
        resp.url().includes(`/api/projects/${pid}/actions/runs/${runId}/status`) &&
        resp.request().method() === 'GET',
      );
      override = next;
      await response;
    },
    clear() {
      override = null;
    },
  };
}

test('active run uses one watcher surface plus a compact footer affordance', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-run-progress-single-surface'));
  const rows = Array.from({ length: 24 }, (_, i) => `"row ${i} needs slow local work"`).join('\n');
  await importCsv(page.request, pid, 'rows.csv', `note\n${rows}\n`);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const status = await installRunStatusOverride(page, pid);
  const runId = await startSimulatedPythonRun(page, pid);
  status.bind(runId);
  await status.set({
    status: 'running',
    total: 24,
    completed: 3,
    failed: 0,
    cost: 0.01,
    live: true,
  });

  const banner = page.getByTestId('active-run-banner');
  await expect(banner).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('run-watcher')).toHaveCount(1);
  await expect(page.locator('.run-watcher-popover')).toHaveCount(0);
  await expect(page.getByTestId('run-progress')).toContainText(/queued|running/i);

  await page.getByTestId('run-watcher-toggle').click();
  await expect(page.getByTestId('run-watcher')).toHaveCount(1);
  await expect(page.locator('.run-watcher-popover')).toHaveCount(0);

  await status.set({
    status: 'stalled',
    total: 24,
    completed: 3,
    failed: 0,
    cost: 0.01,
    live: true,
  });
  await expect(banner).toContainText('stalled: waiting for worker progress', { timeout: 15_000 });

  await status.set({
    status: 'orphaned',
    total: 24,
    completed: 3,
    failed: 0,
    cost: 0.01,
    live: true,
  });
  await expect(banner).toContainText('orphaned: waiting on run recovery', { timeout: 15_000 });

  await status.set({
    status: 'complete',
    total: 24,
    completed: 24,
    failed: 0,
    cost: 0.02,
    live: false,
  });
  await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 60_000 });
  await expect(banner).toBeHidden();
  await expect(page.getByTestId('run-watcher')).toHaveCount(0);

  await page.getByTestId('run-watcher-toggle').click();
  await expect(page.locator('.run-watcher-popover')).toHaveCount(1);
  await expect(page.getByTestId('run-watcher')).toHaveCount(1);
  await expect(page.getByTestId('run-progress')).toContainText('24 rows');

  await page.getByTestId('run-watcher-close').click();
  await expect(page.locator('.run-watcher-popover')).toHaveCount(0);
  await expect(page.getByTestId('run-watcher')).toHaveCount(0);
});

test('failed run copy stays clear without reopening a duplicate active surface', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-run-progress-failed-surface'));
  await importCsv(page.request, pid, 'rows.csv', 'note\n"alpha"\n"beta"\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const status = await installRunStatusOverride(page, pid);
  const runId = await startSimulatedPythonRun(page, pid, 0.5);
  status.bind(runId);
  await status.set({
    status: 'failed',
    total: 2,
    completed: 1,
    failed: 1,
    cost: 0,
    live: false,
  });

  await expect(page.getByTestId('run-progress')).toContainText('failed after 1/2 rows', {
    timeout: 15_000,
  });
  await expect(page.getByTestId('active-run-banner')).toBeHidden();
  await expect(page.getByTestId('run-watcher')).toHaveCount(0);

  await page.getByTestId('run-watcher-toggle').click();
  await expect(page.locator('.run-watcher-popover')).toHaveCount(1);
  await expect(page.getByTestId('run-watcher')).toHaveCount(1);
  await expect(page.getByTestId('run-watcher-close')).toBeVisible();
});
