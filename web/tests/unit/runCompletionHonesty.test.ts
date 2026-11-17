// The status pill and the status bar used to collapse "complete with errors"
// to a flat "complete". Only the dock's job table consulted failedRows, so a
// job that finished with 3 failed rows read as "all complete" in the pill and
// "complete · 4 rows · 3 failed" in the status bar — a success claim
// contradicted by the next chip in the same line, over a job the detail pane
// called "complete with errors".
//
// runStatusModel now owns the one rule; these pin it and the dock summary that
// carries it up to the pill.

import { describe, expect, it } from 'vitest';
import {
  runCompletedWithErrors,
  runStatusLabel,
  toRunProgressStatus,
} from '../../src/runStatusModel';
import { deriveDockErrorJobs, deriveDockRunSummary } from '../../src/workbench/dockJobSummary';
import type { DockActionJob } from '../../src/workbench/WorkbenchBottomDock';
import type { RunProgress } from '../../src/api/types';

function progress(over: Partial<RunProgress> = {}): RunProgress {
  return {
    runId: 'run-1',
    actionName: 'OCR media',
    actionKind: 'media.ocr',
    sheetId: 'sheet-1',
    targetColumnId: 'ocr_text',
    status: 'complete',
    completedRows: 4,
    totalRows: 4,
    failedRows: 0,
    costSoFar: 0,
    ...over,
  } as RunProgress;
}

function job(over: Partial<DockActionJob> = {}): DockActionJob {
  return {
    jobId: 1,
    runId: 'run-1',
    status: 'done',
    kind: 'action',
    actionName: 'OCR media',
    actionKind: 'media.ocr',
    lease: { leaseExpired: false },
    timing: { createdAt: '2026-07-26T11:50:00Z', startedAt: '2026-07-26T11:50:00Z' },
    progress: progress(),
    ...over,
  } as DockActionJob;
}

describe('the one completed-with-errors rule', () => {
  it('fires only for a complete run that left failed rows behind', () => {
    expect(runCompletedWithErrors('complete', 3)).toBe(true);
    expect(runCompletedWithErrors('complete', 0)).toBe(false);
    expect(runCompletedWithErrors('running', 3)).toBe(false);
    expect(runCompletedWithErrors('failed', 3)).toBe(false);
    expect(runCompletedWithErrors('complete', null)).toBe(false);
  });

  it('labels the terminal status the way the job detail pane already did', () => {
    expect(runStatusLabel('complete', 3)).toBe('complete with errors');
    expect(runStatusLabel('complete', 0)).toBe('complete');
    expect(runStatusLabel('cancelled', 3)).toBe('cancelled');
  });

  it('preserves the server public partial outcome', () => {
    expect(toRunProgressStatus('partial')).toBe('partial');
    expect(runStatusLabel('partial', 0)).toBe('partial');
    expect(runStatusLabel('partial', 3)).toBe('partial');
  });
});

describe('dock run summary carries the errored state', () => {
  it('flags a finished job that failed rows', () => {
    const summary = deriveDockRunSummary(
      [job({ progress: progress({ failedRows: 3 }) })],
      null,
    );
    expect(summary.anyComplete).toBe(true);
    expect(summary.anyErrored).toBe(true);
  });

  it('stays clean when everything finished cleanly', () => {
    const summary = deriveDockRunSummary([job()], null);
    expect(summary.anyComplete).toBe(true);
    expect(summary.anyErrored).toBe(false);
    expect(summary.anyPartial).toBe(false);
  });

  it('keeps an all-skipped public partial outcome out of all-complete', () => {
    const summary = deriveDockRunSummary([], progress({
      status: 'partial',
      completedRows: 0,
      failedRows: 0,
      attemptedRows: 0,
      succeededRows: 0,
      skippedRows: 4,
      remainingRows: 0,
    }));
    expect(summary.anyComplete).toBe(true);
    expect(summary.anyPartial).toBe(true);
    expect(summary.anyErrored).toBe(false);
  });

  it('flags a direct run that failed without ever becoming a job row', () => {
    const summary = deriveDockRunSummary([], progress({ status: 'failed', failedRows: 4 }));
    expect(summary.anyErrored).toBe(true);
  });

  it('follows partial → success → success by accepted job identity, keeping older errors inspectable', () => {
    const partial = job({ jobId: 13, runId: '5', progress: progress({ runId: '5', status: 'partial',
      completedRows: 1, totalRows: 6, failedRows: 5 }),
      timing: { createdAt: '2099-01-01', startedAt: '2099-01-02', finishedAt: '2099-01-03' } });
    const firstSuccess = job({ jobId: 15, runId: '6', progress: progress({ runId: '6' }) });
    const latestSuccess = job({ jobId: 16, runId: '7', progress: progress({ runId: '7', completedRows: 2, totalRows: 2 }) });
    expect(deriveDockRunSummary([partial], partial.progress!).anyPartial).toBe(true);
    expect(deriveDockRunSummary([partial, firstSuccess], partial.progress!)).toMatchObject({
      anyComplete: true, anyPartial: false, anyErrored: false,
    });
    expect(deriveDockRunSummary([firstSuccess, partial, latestSuccess], partial.progress!)).toMatchObject({
      anyComplete: true, anyPartial: false, anyErrored: false,
    });
    expect(deriveDockErrorJobs([latestSuccess, firstSuccess, partial], partial.progress!)).toEqual([partial]);
  });

  it('does not override latest job success with an unrelated direct run', () => {
    const latest = job({ jobId: 16, runId: '6', progress: progress({ runId: '6' }) });
    expect(deriveDockRunSummary([latest], progress({ runId: '99', status: 'partial', failedRows: 1 }))).toMatchObject({
      anyComplete: true, anyPartial: false, anyErrored: false,
    });
  });

  it('keeps current work aggregated even with older partial history', () => {
    const old = job({ jobId: 13, runId: '5', progress: progress({ runId: '5', status: 'partial', failedRows: 1 }) });
    const active = job({ jobId: 16, runId: '6', status: 'running',
      progress: progress({ runId: '6', status: 'running', completedRows: 1, totalRows: 2 }) });
    expect(deriveDockRunSummary([old, active], old.progress!)).toMatchObject({
      runningCount: 1, percent: 50, anyComplete: false, anyPartial: false, anyErrored: false,
    });
  });

  it('preserves the latest job error even when its correlated run has no failed rows', () => {
    expect(deriveDockRunSummary([job({ error: 'Worker failed after row settlement.' })], progress())).toMatchObject({
      anyComplete: true, anyErrored: true,
    });
  });

  it('never claims errors while work is still running', () => {
    const running = job({
      status: 'running',
      progress: progress({ status: 'running', completedRows: 1, failedRows: 3 }),
    });
    const summary = deriveDockRunSummary([running], null);
    expect(summary.runningCount).toBe(1);
    expect(summary.anyComplete).toBe(false);
    expect(summary.anyErrored).toBe(false);
  });

  it('reads idle with no history at all', () => {
    const summary = deriveDockRunSummary([], null);
    expect(summary.anyComplete).toBe(false);
    expect(summary.anyErrored).toBe(false);
  });
});
