// While a run streams results into a sheet, the output column's target cells
// pulse (pending shimmer), fill in live
// as rows land, drop their pulse per cell, and a FAILED row shows its error text
// inline instead of an invisible empty dash. Terminal run state clears every
// pulse.
//
// This drives a REAL map.python run (a deterministic, local, no-API-key slow
// worker: `time.sleep` per row over a sandbox subprocess) so the live fill is
// the genuine per-row-flush stream, not a stubbed timeline. The pulse is drawn
// on Glide's canvas, so it is asserted
// through the DEV/test probe SheetGrid publishes at `window.__frisketLiveFill`
// (same idiom as `window.__renderCounts` / `window.__frisketGridTheme`): it
// reports the run's pending output columns plus the grid's own row-cache values
// and per-cell errors — exactly the two inputs buildCell uses to decide whether
// a cell pulses, fills, or shows an error.

import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openAction, uniqueName } from './helpers';

interface LiveFillProbe {
  runId: string | null;
  status: string | null;
  sheetId: string | null;
  pendingColumnIds: string[];
  targetRowIds: string[] | null;
  rowCount: number;
}

interface FillState {
  filled: number;
  empty: number;
  errored: number;
  rowCount: number;
  pending: boolean;
  status: string | null;
}

const ACTIVE = ['queued', 'running', 'stalled', 'orphaned'];

async function readProbe(page: Page): Promise<LiveFillProbe | null> {
  return page.evaluate(() => {
    const p = (window as unknown as { __frisketLiveFill?: LiveFillProbe }).__frisketLiveFill;
    if (!p) return null;
    return {
      runId: p.runId,
      status: p.status,
      sheetId: p.sheetId,
      pendingColumnIds: p.pendingColumnIds,
      targetRowIds: p.targetRowIds,
      rowCount: p.rowCount,
    };
  });
}

// Fold the probe's per-cell reads (cache values + errors) into counts for one
// output column — the shape the three run-phase assertions poll on.
async function fillState(page: Page, columnId: string): Promise<FillState | null> {
  return page.evaluate((cid) => {
    const p = (window as unknown as {
      __frisketLiveFill?: {
        status: string | null;
        pendingColumnIds: string[];
        rowCount: number;
        cellValue: (rowIndex: number, columnId: string) => unknown;
        cellError: (rowIndex: number, columnId: string) => string | null;
      };
    }).__frisketLiveFill;
    if (!p) return null;
    let filled = 0;
    let empty = 0;
    let errored = 0;
    for (let i = 0; i < p.rowCount; i += 1) {
      const err = p.cellError(i, cid);
      if (err) {
        errored += 1;
        continue;
      }
      const v = p.cellValue(i, cid);
      if (v === null || v === undefined || v === '') empty += 1;
      else filled += 1;
    }
    return {
      filled,
      empty,
      errored,
      rowCount: p.rowCount,
      pending: p.pendingColumnIds.includes(cid),
      status: p.status,
    };
  }, columnId);
}

// The run's output column id, discovered from the probe once the pulse turns on
// (a brand-new ai_generated column with no current_run_id yet — the pending set).
async function outputColumnId(page: Page): Promise<string> {
  let columnId = '';
  await expect
    .poll(async () => {
      const probe = await readProbe(page);
      const id = probe?.pendingColumnIds[0];
      if (id) columnId = id;
      return columnId;
    }, { timeout: 20_000, intervals: [100, 150, 200] })
    .not.toBe('');
  return columnId;
}

async function startPythonRun(page: Page, code: string, columnName: string): Promise<void> {
  await openAction(page, 'map.python');
  await expect(page.getByTestId('field-code')).toBeVisible();
  await page.getByTestId('field-code').fill(code);
  await page.getByTestId('field-output-computed').fill(columnName);
  await page.getByTestId('generated-action-run').click();
}

function importNumberedCsv(count: number): string {
  const rows = Array.from({ length: count }, (_, i) => String(i)).join('\n');
  return `n\n${rows}\n`;
}

test('cells pulse during a run, fill in live before completion, and clear all pulses after', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-live-cell-fill'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', importNumberedCsv(20));
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // A slow local worker: each row sleeps, so the run streams over ~1.5s and the
  // per-row flush makes intermediate fills observable. 20 rows over concurrency
  // 8 = three fill waves — a wide window where some cells are filled while the
  // rest still pulse.
  await startPythonRun(
    page,
    "import time\ntime.sleep(0.5)\nresult = {'v': row['n']}",
    'out',
  );

  const columnId = await outputColumnId(page);

  // (1) pulse present DURING + (2) intermediate fill visible BEFORE completion:
  // catch a live tick where the run is still active, at least one cell has
  // landed, and at least one target cell is still empty (= pulsing).
  await expect
    .poll(async () => {
      const s = await fillState(page, columnId);
      if (!s) return false;
      const active = s.status !== null && ACTIVE.includes(s.status);
      return active && s.pending && s.filled > 0 && s.filled < s.rowCount;
    }, { timeout: 20_000, intervals: [100, 150, 200] })
    .toBe(true);

  // (3) run end: every row filled, and NO pulse remains anywhere.
  await expect
    .poll(async () => {
      const s = await fillState(page, columnId);
      if (!s) return false;
      return s.filled === s.rowCount && !s.pending && s.errored === 0;
    }, { timeout: 30_000, intervals: [150, 250, 400] })
    .toBe(true);

  const probe = await readProbe(page);
  expect(probe?.status).not.toBeNull();
  expect(ACTIVE.includes(String(probe?.status))).toBe(false);
  expect(probe?.pendingColumnIds).toEqual([]);
});

test('a failed row shows its error text inline instead of an empty dash', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-live-cell-fail'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', importNumberedCsv(12));
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Odd-indexed rows raise; even rows succeed. Per-row failures persist as result
  // errors and the run continues past them (map_runner: failed rows land in
  // per-row error state).
  await startPythonRun(
    page,
    [
      'import time',
      'time.sleep(0.3)',
      "if int(row['n']) % 2 == 1:",
      "    raise ValueError('stub failure for row ' + str(row['n']))",
      "result = {'value': 'ok' + str(row['n'])}",
    ].join('\n'),
    'out',
  );

  const columnId = await outputColumnId(page);

  // The run finishes; failed rows are counted and every pulse has cleared.
  await expect
    .poll(async () => {
      const s = await fillState(page, columnId);
      if (!s) return false;
      const done = s.status !== null && !ACTIVE.includes(s.status);
      return done && !s.pending && s.errored > 0 && s.filled + s.errored === s.rowCount;
    }, { timeout: 30_000, intervals: [150, 250, 400] })
    .toBe(true);

  // The failed cell carries readable error text in the grid's own row cache —
  // the inline error the renderer draws (⚠ …) instead of an invisible dash.
  const errorText = await page.evaluate((cid) => {
    const p = (window as unknown as {
      __frisketLiveFill?: { cellError: (rowIndex: number, columnId: string) => string | null };
    }).__frisketLiveFill;
    return p ? p.cellError(1, cid) : null; // row index 1 = n:1 = odd = failed
  }, columnId);
  expect(errorText, 'failed cell must carry inline error text').toBeTruthy();
  expect(String(errorText)).toMatch(/stub failure|failed|error/i);
});

test('run-status polling stops once the run is terminal (no forever-polling completed runs)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-live-fill-poll-stop'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', importNumberedCsv(4));

  // Count every /actions/runs/{id}/status request the page fires.
  let statusRequests = 0;
  page.on('request', (req) => {
    if (/\/actions\/runs\/\d+\/status(\?|$)/.test(req.url())) statusRequests += 1;
  });

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await startPythonRun(page, "import time\ntime.sleep(0.2)\nresult = {'v': row['n']}", 'out');
  const columnId = await outputColumnId(page);

  // Wait for the run to reach a terminal status.
  await expect
    .poll(async () => {
      const s = await fillState(page, columnId);
      return s !== null && s.status !== null && !ACTIVE.includes(s.status);
    }, { timeout: 30_000, intervals: [150, 250, 400] })
    .toBe(true);

  // Let any final terminal-tick fetch settle, then assert polling has stopped:
  // over a multi-interval window (poll drivers are 300ms/750ms) the
  // /status request count must not grow — a terminal run is never re-polled.
  await page.waitForTimeout(2_000);
  const settled = statusRequests;
  await page.waitForTimeout(3_000);
  expect(
    statusRequests,
    `status polling must stop after a run completes (was ${settled}, now ${statusRequests})`,
  ).toBe(settled);
});
