// @vitest-environment jsdom
//
// StatusBar wiring for the failure triage: a FINISHED run's watcher popover
// carries the outcome buckets from RunProgress.rowErrors; bucket actions act
// on the run's target column ("Show" also closes the popover so the filtered
// grid is visible); a run with no remembered target column renders buckets
// with no actions.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getRunRows: vi.fn(async () => ({ rows: [], total: 0 })),
    },
  };
});

import { StatusBar } from '../../src/components/StatusBar';
import { createProjectApi } from '../../src/api/real';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';
import type { RunProgress } from '../../src/api/types';
const projectApi = createProjectApi('status-bar-failure-triage');
vi.spyOn(projectApi, 'getRunRows').mockResolvedValue({ rows: [], total: 0 });
const { render } = createWorkspaceTestHarness({
  projectId: 'status-bar-failure-triage',
  api: { projectApi: projectApi },
});


beforeAll(() => {
  installPopoverPolyfill();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const finishedRun = (over: Partial<RunProgress> = {}): RunProgress => ({
  runId: 'run-9',
  actionName: 'Classify docket',
  actionKind: 'map.classify',
  sheetId: 'sheet-1',
  targetColumnId: 'relevance',
  status: 'complete',
  completedRows: 100,
  totalRows: 100,
  failedRows: 12,
  costSoFar: 1.5,
  rowErrors: {
    totalFailedRows: 12,
    groups: [
      {
        message: 'provider rate limited',
        count: 8,
        code: 'provider_rate_limited',
        outcome: 'model_error',
        terminal: false,
        rowIds: ['3', '4'],
      },
      {
        message: 'no output over a non-empty source',
        count: 3,
        code: 'empty_output',
        outcome: 'empty_output',
        terminal: true,
        rowIds: ['7'],
      },
      {
        message: 'schema validation failed',
        count: 1,
        code: 'invalid_output',
        outcome: 'invalid_output',
        terminal: false,
        rowIds: ['11'],
      },
    ],
  },
  ...over,
});

function renderBar(run: RunProgress, over: Partial<React.ComponentProps<typeof StatusBar>> = {}) {
  const onShowFailedRows = vi.fn();
  const onRetryFailedRows = vi.fn();
  render(
    <StatusBar
      run={run}
      history={null}
      reviewCount={0}
      projectionStatus={null}
      onUndo={() => {}}
      onRedo={() => {}}
      onOpenReview={() => {}}
      onCancelRun={() => {}}
      onShowFailedRows={onShowFailedRows}
      onRetryFailedRows={onRetryFailedRows}
      {...over}
    />,
  );
  return { onShowFailedRows, onRetryFailedRows };
}

describe('StatusBar failure triage', () => {
  it('opens the finished-run watcher with buckets and acts on the target column', () => {
    const { onShowFailedRows, onRetryFailedRows } = renderBar(finishedRun());
    fireEvent.click(screen.getByTestId('run-watcher-toggle'));
    expect(screen.getByTestId('run-failure-triage')).toHaveTextContent('12 failed rows');

    fireEvent.click(screen.getByTestId('run-failure-bucket-retry-model_error'));
    expect(onRetryFailedRows).toHaveBeenCalledWith('relevance', 'model_error');
    // Retry keeps the popover open so "Retry requested" stays visible.
    expect(screen.getByTestId('run-failure-triage')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('run-failure-bucket-show-empty_output'));
    expect(onShowFailedRows).toHaveBeenCalledWith('relevance', 'empty_output');
    // Show closes the popover so the freshly filtered grid is visible.
    expect(screen.queryByTestId('run-failure-triage')).not.toBeInTheDocument();
  });

  it('a run restored without launch context shows buckets but no actions', () => {
    renderBar(finishedRun({ targetColumnId: '' }));
    fireEvent.click(screen.getByTestId('run-watcher-toggle'));
    expect(screen.getByTestId('run-failure-bucket-model_error')).toBeInTheDocument();
    expect(screen.queryByTestId('run-failure-bucket-show-model_error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('run-failure-bucket-retry-model_error')).not.toBeInTheDocument();
  });

  it('an active run renders no triage (retry against a moving run is wrong)', () => {
    renderBar(finishedRun({ status: 'running' }));
    expect(screen.queryByTestId('run-failure-triage')).not.toBeInTheDocument();
  });
});

// The label used to read "Classify docket · complete · 100 rows · 12 failed":
// a success claim contradicted by the very next chip, and wearing the green
// `run-done` tone while it did it. The dock's job detail pane had called the
// same run "complete with errors" all along.
describe('StatusBar terminal-run label honesty', () => {
  it('says complete with errors, without the done tone, when rows failed', () => {
    renderBar(finishedRun());
    const label = document.querySelector('.run-label');
    expect(label).toHaveTextContent('complete with errors · 100 rows');
    expect(label).not.toHaveClass('run-done');
    fireEvent.click(screen.getByTestId('run-watcher-toggle'));
    const detailStatus = document.querySelector('.run-watcher-status');
    expect(detailStatus).toHaveTextContent('complete with errors');
    expect(detailStatus).not.toHaveClass('status-complete');
    expect(detailStatus).toHaveClass('run-failed');
  });

  it('keeps the plain complete label and the done tone for a clean run', () => {
    renderBar(finishedRun({ failedRows: 0, completedRows: 100, rowErrors: undefined }));
    const label = document.querySelector('.run-label');
    expect(label).toHaveTextContent('complete · 100 rows');
    expect(label).not.toHaveTextContent('with errors');
    expect(label).toHaveClass('run-done');
    fireEvent.click(screen.getByTestId('run-watcher-toggle'));
    const detailStatus = document.querySelector('.run-watcher-status');
    expect(detailStatus).toHaveTextContent('complete');
    expect(detailStatus).not.toHaveTextContent('with errors');
    expect(detailStatus).toHaveClass('status-complete');
  });
});

describe('StatusBar queued-run feedback', () => {
  it('shows visible activity while a queued run waits to start', () => {
    renderBar(finishedRun({ status: 'queued', completedRows: 0, failedRows: 0 }));
    const banner = screen.getByTestId('active-run-banner');
    expect(banner).toHaveTextContent('queued — waiting to start');
    expect(banner.querySelector('.run-spinner')).toBeInTheDocument();
  });
});

describe('StatusBar review entry point', () => {
  it('stays out of the idle status bar when there is nothing to review', () => {
    renderBar(finishedRun(), { reviewCount: 0 });
    expect(screen.queryByTestId('review-queue-button')).not.toBeInTheDocument();
  });

  it('appears with its count when review work is pending', () => {
    const onOpenReview = vi.fn();
    renderBar(finishedRun(), { reviewCount: 3, onOpenReview });
    const button = screen.getByTestId('review-queue-button');
    expect(button).toHaveTextContent('Review');
    expect(button).toHaveTextContent('3');
    fireEvent.click(button);
    expect(onOpenReview).toHaveBeenCalledOnce();
  });
});
