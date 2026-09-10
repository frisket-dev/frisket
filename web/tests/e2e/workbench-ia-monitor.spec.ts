import { expect, test, type Page } from '@playwright/test';
import { routeV1ActionRun, routeV1ActionStatus } from './actionStatusFixtures';
import {
  createProject,
  editCells,
  importCsv,
  openAction,
  openHistory,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

// workbench-ia-monitor-v1 (Workbench IA increment 5): the Monitor region — the
// bottom dock restyled with the amber --monitor accent, a re-hosted History
// tab, a live run summary driven by the run controller's job store, and a
// drag-resizable height seam (clamp 56–340px, persisted). The assertions below
// preserve that layout and interaction contract.

type RunStatus = 'queued' | 'running' | 'complete' | 'failed';

interface RunStatusOverride {
  status: RunStatus;
  total: number;
  completed: number;
  failed: number;
  cost: number | null;
  live: boolean;
}

// Deterministic, model-free running job: a simulated map.python run whose
// getRunProgress responses we drive through the run-status route override, so
// the dock summary is exercised against the REAL job store (never faked) with
// no live worker or model.
async function installRunStatusOverride(page: Page, pid: string) {
  let runId: number | null = null;
  let override: RunStatusOverride | null = null;
  await routeV1ActionStatus(page, pid, (matchedRunId) => {
    if (override === null || runId === null || matchedRunId !== runId) return null;
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
  };
}

async function startSimulatedPythonRun(page: Page, pid: string): Promise<number> {
  const runId = 8801;
  await routeV1ActionRun(page, pid, {
    actionKind: 'map.python',
    receiptId: `receipt-monitor-${runId}`,
    runId,
    status: 'completed',
  });
  await openAction(page, 'map.python');
  await expect(page.getByTestId('field-code')).toBeVisible();
  await page.getByTestId('field-code').fill("result = row.get('note', '')");
  const [, runResponse] = await Promise.all([
    page.waitForRequest(
      (request) =>
        request.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
        request.method() === 'POST',
    ),
    page.waitForResponse(
      (response) =>
        response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
        response.request().method() === 'POST',
    ),
    page.getByTestId('generated-action-run').click(),
  ]);
  const body = (await runResponse.json()) as { run_id: number | null };
  if (body.run_id == null) throw new Error('simulated v1 run did not return a run id');
  return body.run_id;
}

test('the active dock tab carries the Monitor amber accent', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('monitor-accent'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  const jobsTab = page.getByTestId('bottom-dock-tab-jobs');
  await expect(jobsTab).toHaveAttribute('aria-selected', 'true');

  const probe = await page.evaluate(() => {
    const readColor = (el: Element | null) => (el ? getComputedStyle(el).color : '');
    const span = document.createElement('span');
    span.style.color = getComputedStyle(document.documentElement)
      .getPropertyValue('--monitor')
      .trim();
    document.body.appendChild(span);
    const monitor = getComputedStyle(span).color;
    span.remove();
    const active = document.querySelector('[data-testid="bottom-dock-tab-jobs"]');
    return {
      monitor,
      activeColor: readColor(active),
      activeShadow: active ? getComputedStyle(active).boxShadow : '',
      inactiveColor: readColor(document.querySelector('[data-testid="bottom-dock-tab-history"]')),
    };
  });

  // Active tab text + underline are painted in var(--monitor); inactive is not.
  expect(probe.activeColor).toBe(probe.monitor);
  expect(probe.activeShadow).toContain(probe.monitor);
  expect(probe.inactiveColor).not.toBe(probe.monitor);
});

test('the History tab re-hosts the reverse-chron op log', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('monitor-history'));
  const sheetId = await importCsv(request, pid, 'notes.csv', 'note\nfirst\nsecond\n');
  const columns = await sheetColumns(request, pid, sheetId);
  const noteCol = columns.find((c) => c.name === 'note')!;
  // A completed edit action guarantees at least one op beyond the import.
  const firstRow = (await request
    .get(`/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`)
    .then((r) => r.json())).rows[0].id as number;
  await editCells(request, pid, [{ rowId: firstRow, columnId: noteCol.id, value: 'edited' }]);

  await openProject(page, pid, sheetId);
  await openHistory(page);

  // The dock panel is now the History contribution and shows op rows.
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-contribution-id',
    'frisket.core.panel.history',
  );
  const opRows = page.getByTestId('history-list').locator('[data-testid^="history-step-op-"]');
  await expect(opRows.first()).toBeVisible();
  expect(await opRows.count()).toBeGreaterThanOrEqual(1);
});

test('the live run summary and progress bar track the job store', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('monitor-summary'));
  const rows = Array.from({ length: 12 }, (_, i) => `"row ${i}"`).join('\n');
  await importCsv(request, pid, 'rows.csv', `note\n${rows}\n`);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const summary = page.getByTestId('bottom-dock-run-summary');
  // Idle before any run.
  await expect(summary).toHaveAttribute('data-run-state', 'idle');
  await expect(page.getByTestId('bottom-dock-run-progressbar')).toHaveCount(0);

  const status = await installRunStatusOverride(page, pid);
  const runId = await startSimulatedPythonRun(page, pid);
  status.bind(runId);
  await status.set({ status: 'running', total: 12, completed: 4, failed: 0, cost: 0.01, live: true });

  // Running: the summary flips and the amber progress bar exists during the run.
  await expect(summary).toHaveAttribute('data-run-state', 'running', { timeout: 15_000 });
  await expect(page.getByTestId('bottom-dock-run-progressbar')).toBeVisible();
  await expect(summary).toContainText('running');
  // The status bar's run indicator stays in sync (same job store).
  await expect(page.getByTestId('run-progress')).toContainText(/running/i);

  await status.set({ status: 'complete', total: 12, completed: 12, failed: 0, cost: 0.02, live: false });

  // Complete: no longer running, and the progress bar is gone.
  await expect(summary).toHaveAttribute('data-run-state', 'complete', { timeout: 15_000 });
  await expect(page.getByTestId('bottom-dock-run-progressbar')).toHaveCount(0);
});

test('the top-edge seam resizes the dock and the height persists across reload', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('monitor-resize'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  const dock = page.getByTestId('workbench-region-bottomDock');
  const seam = page.getByTestId('bottom-dock-resize');
  const before = (await dock.boundingBox())!.height;

  // Drag the top-edge seam upward → the dock grows.
  const box = (await seam.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y - 90, { steps: 10 });
  await page.mouse.up();

  const after = (await dock.boundingBox())!.height;
  expect(after).toBeGreaterThan(before);
  const stored = await page.evaluate(() => localStorage.getItem('frisket:bottom-dock-height'));
  expect(Number(stored)).toBeGreaterThan(before);

  // Reload: the height is restored from localStorage.
  await openProject(page, pid, sheetId);
  const restored = (await page.getByTestId('workbench-region-bottomDock').boundingBox())!.height;
  expect(Math.abs(restored - after)).toBeLessThan(3);
});

test('the dock tabs join the panel below and still switch content', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('monitor-tabs'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  const panel = page.getByTestId('bottom-dock-panel');

  const errorsTab = page.getByTestId('bottom-dock-tab-errors');
  await errorsTab.click();
  await expect(panel).toHaveAttribute('data-active-contribution-id', 'frisket.core.panel.errors');

  const activeTabStyle = await errorsTab.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      borderTopWidth: style.borderTopWidth,
      borderBottomWidth: style.borderBottomWidth,
      borderBottomLeftRadius: style.borderBottomLeftRadius,
      borderBottomRightRadius: style.borderBottomRightRadius,
      marginBottom: style.marginBottom,
      boxShadow: style.boxShadow,
      zIndex: style.zIndex,
    };
  });
  expect(activeTabStyle).toEqual({
    borderTopWidth: '2px',
    borderBottomWidth: '0px',
    borderBottomLeftRadius: '0px',
    borderBottomRightRadius: '0px',
    marginBottom: '-1px',
    boxShadow: 'none',
    zIndex: '1',
  });

  await page.getByTestId('bottom-dock-tab-jobs').click();
  await expect(panel).toHaveAttribute('data-active-contribution-id', 'frisket.core.panel.jobs');
});

// --- deriveDockRunSummary contract (page-less: the module is pure) ---------
// Percent must aggregate EVERY
// active source of progress (per-job RunProgress + the active run when it has
// not surfaced in the job list, deduped by runId), never just the active run.

import { deriveDockErrorJobs, deriveDockRunSummary } from '../../src/workbench/dockJobSummary';
import type { DockActionJob } from '../../src/workbench/WorkbenchBottomDock';
import type { RunProgress } from '../../src/api/open';

function fakeProgress(runId: string, completedRows: number, totalRows: number): RunProgress {
  return {
    runId,
    actionName: 'x',
    actionKind: 'map.python',
    sheetId: '1',
    targetColumnId: 'c',
    status: 'running',
    completedRows,
    totalRows,
    failedRows: 0,
    costSoFar: 0,
  } as RunProgress;
}

function fakeJob(jobId: number, runId: string | null, progress?: RunProgress | null): DockActionJob {
  return {
    schemaVersion: '1',
    projectId: 'p',
    jobId,
    kind: 'action',
    runId,
    status: 'running',
    actionKind: 'map.python',
    actionName: 'Python',
    attempts: 1,
    maxAttempts: 3,
    lease: { lockedBy: null, lockedAt: null, leaseExpiresAt: null, leaseExpired: false },
    progress,
  } as DockActionJob;
}

test('summary aggregates percent across multiple active jobs', () => {
  const summary = deriveDockRunSummary(
    [fakeJob(1, 'r1', fakeProgress('r1', 5, 10)), fakeJob(2, 'r2', fakeProgress('r2', 15, 40))],
    null,
  );
  expect(summary.runningCount).toBe(2);
  expect(summary.percent).toBeCloseTo(40); // (5+15)/(10+40)
});

test('summary counts + aggregates an active run absent from the job list, and dedupes one that is present', () => {
  const absent = deriveDockRunSummary(
    [fakeJob(1, 'r1', fakeProgress('r1', 5, 10))],
    fakeProgress('r2', 10, 10),
  );
  expect(absent.runningCount).toBe(2);
  expect(absent.percent).toBeCloseTo(75); // (5+10)/(10+10)

  const present = deriveDockRunSummary(
    [fakeJob(1, 'r1', fakeProgress('r1', 5, 10))],
    fakeProgress('r1', 5, 10),
  );
  expect(present.runningCount).toBe(1); // deduped by runId
  expect(present.percent).toBeCloseTo(50);
});

test('summary is indeterminate without known totals and idle/complete without active work', () => {
  const indeterminate = deriveDockRunSummary([fakeJob(1, 'r1', fakeProgress('r1', 0, 0))], null);
  expect(indeterminate.runningCount).toBe(1);
  expect(indeterminate.percent).toBeNull();

  const done = { ...fakeProgress('r1', 10, 10), status: 'complete' } as RunProgress;
  const complete = deriveDockRunSummary([], done);
  expect(complete.runningCount).toBe(0);
  expect(complete.anyComplete).toBe(true);
  expect(complete.percent).toBeNull();
});

// --- deriveDockErrorJobs contract ---------------------------------------
// A failed DIRECT run (e.g. census_demographics) never becomes a job-queue row,
// so the Errors dock — fed only by the queue — must also fold in the active
// run when it failed / has failed rows, deduped by runId.

test('errors fold in a failed direct run absent from the job queue', () => {
  const failedRun = { ...fakeProgress('r-direct', 3, 3), status: 'failed', failedRows: 3 } as RunProgress;
  const jobs = deriveDockErrorJobs([], failedRun);
  expect(jobs).toHaveLength(1);
  expect(jobs[0].runId).toBe('r-direct');
  expect(jobs[0].status).toBe('failed');
});

test('errors fold in a completed-with-failures direct run (census total failure)', () => {
  // census keeps runs.status='completed' even on total failure; failedRows>0.
  const run = { ...fakeProgress('r-census', 2, 2), status: 'complete', failedRows: 2 } as RunProgress;
  const jobs = deriveDockErrorJobs([], run);
  expect(jobs).toHaveLength(1);
  expect(jobs[0].progress?.failedRows).toBe(2);
});

test('errors carry the run-level message when present', () => {
  const run = {
    ...fakeProgress('r-msg', 1, 1),
    status: 'failed',
    failedRows: 1,
    error: 'Census ACS request failed 403: Missing Census API key',
  } as RunProgress;
  const [entry] = deriveDockErrorJobs([], run);
  expect(entry.error).toContain('Missing Census API key');
});

test('a healthy direct run and a healthy queue produce no error rows', () => {
  const ok = { ...fakeProgress('r-ok', 5, 5), status: 'complete', failedRows: 0 } as RunProgress;
  expect(deriveDockErrorJobs([], ok)).toHaveLength(0);
  expect(deriveDockErrorJobs([fakeJob(1, 'r1', fakeProgress('r1', 5, 10))], null)).toHaveLength(0);
});

test('a failed run already present as a queue job is not double-counted', () => {
  const run = { ...fakeProgress('rDup', 1, 1), status: 'failed', failedRows: 1 } as RunProgress;
  const queued = fakeJob(7, 'rDup', run);
  const jobs = deriveDockErrorJobs([queued], run);
  // The queue row satisfies isErrorJob; no synthetic run row is added.
  expect(jobs).toHaveLength(1);
  expect(jobs[0].jobId).toBe(7);
});

test('the dock minimizes to its tab strip like Discover, re-expands via chevron or tab, persists', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('monitor-collapse'));
  const sheetId = await importCsv(request, pid, 'a.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  const dock = page.getByTestId('workbench-region-bottomDock');
  const expandedHeight = (await dock.boundingBox())!.height;
  await expect(page.getByTestId('bottom-dock-panel')).toBeVisible();

  // Minimize: body gone, strip remains, much shorter.
  await page.getByTestId('bottom-dock-collapse').click();
  await expect(dock).toHaveAttribute('data-collapsed', 'true');
  await expect(page.getByTestId('bottom-dock-panel')).toHaveCount(0);
  await expect(page.getByTestId('bottom-dock-resize')).toHaveCount(0);
  expect((await dock.boundingBox())!.height).toBeLessThan(Math.min(expandedHeight, 60));

  // Persists across reload.
  await page.reload();
  await expect(dock).toHaveAttribute('data-collapsed', 'true');

  // A tab click re-expands to that tab.
  await page.getByTestId('bottom-dock-tab-errors').click();
  await expect(dock).not.toHaveAttribute('data-collapsed', 'true');
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-contribution-id',
    'frisket.core.panel.errors',
  );

  // Chevron toggles too.
  await page.getByTestId('bottom-dock-collapse').click();
  await expect(page.getByTestId('bottom-dock-panel')).toHaveCount(0);
  await page.getByTestId('bottom-dock-collapse').click();
  await expect(page.getByTestId('bottom-dock-panel')).toBeVisible();
});
