// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import type { ColumnRun, ColumnRunsInfo } from '../../src/api/types';
import { ActionCompareHistoryDialog } from '../../src/workspace/ActionCompareHistoryDialog';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('compare-history');
const { render } = createWorkspaceTestHarness({
  projectId: 'compare-history',
  api: { projectApi },
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function run(overrides: Partial<ColumnRun> = {}): ColumnRun {
  return {
    runId: 'run-42',
    actionKind: 'map.extract',
    actionName: 'Extract structured fields',
    model: 'Qwen 3 4B',
    status: 'completed',
    spec: { private_prompt: 'DO_NOT_RENDER' },
    totalRows: 240,
    completedRows: 239,
    failedRows: 1,
    cost: 0.12,
    startedAt: '2026-10-25T10:12:00Z',
    finishedAt: '2026-10-25T10:13:00Z',
    durationMs: 60_000,
    tokensIn: 10,
    tokensOut: 20,
    current: true,
    humanScore: { passed: 0, graded: 0 },
    judgeScores: [],
    ...overrides,
  };
}

function page(overrides: Partial<ColumnRunsInfo> = {}): ColumnRunsInfo {
  return {
    columnName: 'plaintiff',
    offset: 0,
    limit: 20,
    totalRuns: 21,
    hasMore: true,
    nextOffset: 20,
    currentRun: null,
    currentRunLoaded: false,
    latestRun: null,
    latestRunLoaded: false,
    mixedOrigins: true,
    runs: [run()],
    ...overrides,
  };
}

const anchor = { sheetId: 'sheet-1', runId: 'run-42', columnId: 'column-7' };

describe('ActionCompareHistoryDialog', () => {
  it('pages actual stored runs and returns an explicit run anchor', async () => {
    const getColumnRuns = vi.spyOn(projectApi, 'getColumnRuns')
      .mockResolvedValueOnce(page())
      .mockResolvedValueOnce(page({
        offset: 20,
        hasMore: false,
        nextOffset: null,
        runs: [run({
          runId: 'run-older',
          model: 'Claude Sonnet',
          current: false,
          completedRows: 220,
          failedRows: 20,
        })],
      }));
    const choose = vi.fn();
    const close = vi.fn();
    render(<ActionCompareHistoryDialog anchor={anchor} onChoose={choose} onClose={close} />);

    await waitFor(() => expect(getColumnRuns).toHaveBeenCalledWith('column-7', 0, 20));
    expect(await screen.findByText('Extract structured fields')).toBeInTheDocument();
    expect(screen.getByText('Qwen 3 4B')).toBeInTheDocument();
    expect(screen.getByText('Current')).toBeInTheDocument();
    expect(screen.getByText(/Current values include more than one run or manual edits/)).toBeInTheDocument();
    expect(screen.queryByText('DO_NOT_RENDER')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('action-compare-history-choose-run-42'));
    expect(choose).toHaveBeenCalledWith(anchor);

    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    await waitFor(() => expect(getColumnRuns).toHaveBeenCalledWith('column-7', 20, 20));
    expect(await screen.findByText('Claude Sonnet')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('action-compare-history-choose-run-older'));
    expect(choose).toHaveBeenLastCalledWith({ ...anchor, runId: 'run-older' });

    fireEvent.click(screen.getByRole('button', { name: 'Close earlier runs' }));
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('shows a bounded retry state without fabricating history', async () => {
    const getColumnRuns = vi.spyOn(projectApi, 'getColumnRuns')
      .mockRejectedValueOnce(new Error('History is temporarily unavailable'))
      .mockResolvedValueOnce(page({ totalRuns: 0, hasMore: false, nextOffset: null, mixedOrigins: false, runs: [] }));
    render(<ActionCompareHistoryDialog anchor={anchor} onChoose={vi.fn()} onClose={vi.fn()} />);

    expect(await screen.findByText('History is temporarily unavailable')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('No stored runs are available.')).toBeInTheDocument();
    expect(getColumnRuns).toHaveBeenCalledTimes(2);
  });
});
