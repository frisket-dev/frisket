// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SourceHealthMainView } from '../../src/components/sources/SourceHealthMainView';
import type { SourceHealth } from '../../src/api/types';
import type { SourceApiPort } from '../../src/api/ports';

afterEach(cleanup);

const health: SourceHealth = {
    schemaVersion: 'frisket.source_health.v1',
    source: { id: 4, name: 'Records', kind: 'rss', url: null, enabled: false, schedule: null, sheetId: 2, createdAt: null, redactions: ['token'] },
    summary: { status: 'failing', lastSuccessAt: null, lastFailureAt: null, consecutiveFailures: 2,
      newRowsTotal: 9, newRowsRecent: 4, changedRowsRecent: 1, skippedRowsRecent: 3, revisionsRecent: 2, recentRunCount: 1, lastCursorSummary: 'page 2' },
    runsPage: { schemaVersion: 'frisket.source_runs_page.v1', order: 'desc', offset: 0, limit: 20, total: 1, hasMore: false, nextOffset: null },
    runs: [{ id: 8, sourceId: 4, opId: null, status: 'error', newRows: 4, skippedRows: 3, changedRows: 1, revisions: 2,
      warningCount: 1, durationMs: 1000, costMicro: 4200, receiptId: null, error: 'Fetch failed', errorSummary: 'Fetch failed',
      startedAt: '2026-10-19T12:00:00Z', finishedAt: null, cursorBeforePresent: false, cursorAfterPresent: true, summaryPresent: true }],
    downstreamJobs: [{ jobId: 1, kind: 'extract', status: 'failed', attempts: 1, maxAttempts: 3,
      createdAt: null, startedAt: null, finishedAt: null, refs: {}, resultSummary: {}, errorSummary: 'Extraction failed', stalled: false }],
    costs: { recentActualMicro: 4200, recentEstimatedMicro: 5000, basis: 'runs' },
    alerts: [{ message: 'Attention needed' }], warnings: [{ message: 'Incomplete metadata' }],
  };
it('keeps summaries, run details, jobs, warnings and poll behavior in shared source-health content', async () => {
  const sourceApi: SourceApiPort = {
    listSources: vi.fn(), getSource: vi.fn(), createSource: vi.fn(), updateSource: vi.fn(), deleteSource: vi.fn(),
    getSourceHealth: vi.fn().mockResolvedValue(health),
    fetchSource: vi.fn().mockResolvedValue({ sheetId: 2 }),
  };
  const polled = vi.fn();
  render(<SourceHealthMainView sourceApi={sourceApi} sourceId={4} onClose={vi.fn()} onPolled={polled} />);
  expect(await screen.findByTestId('source-health-status-4')).toHaveTextContent('failing');
  expect(screen.getByTestId('source-health-deltas-4')).toHaveTextContent('+4 new');
  expect(screen.getByTestId('source-health-runs-4')).toHaveTextContent('Fetch failed');
  expect(screen.getByTestId('source-health-jobs-4')).toHaveTextContent('Extraction failed');
  expect(screen.getByTestId('source-health-warnings-4')).toHaveTextContent('Incomplete metadata');
  expect(screen.getByTestId('source-health-redactions-4')).toHaveTextContent('token / cursor page 2');
  fireEvent.click(screen.getByRole('button', { name: 'Poll now' }));
  await waitFor(() => expect(polled).toHaveBeenCalledWith(2));
  expect(sourceApi.fetchSource).toHaveBeenCalledWith(4);
});

it('ignores an earlier source health response after changing sources', async () => {
  let resolvePrevious!: (value: SourceHealth) => void;
  const previous = new Promise<SourceHealth>((resolve) => { resolvePrevious = resolve; });
  const next = { ...health, source: { ...health.source, id: 5, name: 'Next records' } };
  const sourceApi: SourceApiPort = {
    listSources: vi.fn(), getSource: vi.fn(), createSource: vi.fn(), updateSource: vi.fn(), deleteSource: vi.fn(),
    getSourceHealth: vi.fn().mockReturnValueOnce(previous).mockResolvedValue(next), fetchSource: vi.fn(),
  };
  const props = { sourceApi, onClose: vi.fn() };
  const view = render(<SourceHealthMainView {...props} sourceId={4} />);
  view.rerender(<SourceHealthMainView {...props} sourceId={5} />);
  await screen.findByText('Next records');
  await act(async () => { resolvePrevious(health); await previous; });
  expect(screen.getByText('Next records')).toBeInTheDocument();
  expect(screen.queryByText('Records')).not.toBeInTheDocument();
});
