// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { FeedHealthMainView } from '../../src/components/feeds/FeedHealthMainView';
import type { FeedHealth } from '../../src/api/types';
import type { FeedApiPort } from '../../src/api/ports';
import { createStore } from '../../src/core/store/createStore';

afterEach(cleanup);

const health: FeedHealth = {
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
const jobs = { store: createStore({ actionJobs: { error: null, jobs: [], loading: false } }), refresh: vi.fn(async () => {}) };
it('keeps summaries, run details, jobs, warnings and poll behavior in shared source-health content', async () => {
  const feedApi: FeedApiPort = {
    listFeeds: vi.fn(), getFeed: vi.fn(), createFeed: vi.fn(), updateFeed: vi.fn(), deleteFeed: vi.fn(),
    getFeedHealth: vi.fn().mockResolvedValue(health),
    fetchFeed: vi.fn().mockResolvedValue({ sheetId: 2 }),
  };
  const polled = vi.fn();
  render(<FeedHealthMainView feedApi={feedApi} jobs={jobs} sourceId={4} onClose={vi.fn()} onPolled={polled} />);
  expect(await screen.findByTestId('feed-health-status-4')).toHaveTextContent('failing');
  expect(screen.getByTestId('feed-health-deltas-4')).toHaveTextContent('+4 new');
  expect(screen.getByTestId('feed-health-checks-4')).toHaveTextContent('Fetch failed');
  expect(screen.getByTestId('feed-health-jobs-4')).toHaveTextContent('Extraction failed');
  expect(screen.getByTestId('feed-health-warnings-4')).toHaveTextContent('Incomplete metadata');
  expect(screen.getByTestId('feed-health-redactions-4')).toHaveTextContent('token / cursor page 2');
  fireEvent.click(screen.getByRole('button', { name: 'Check now' }));
  await waitFor(() => expect(polled).toHaveBeenCalledWith(2));
  expect(feedApi.fetchFeed).toHaveBeenCalledWith(4);
});

it('ignores an earlier source health response after changing sources', async () => {
  let resolvePrevious!: (value: FeedHealth) => void;
  const previous = new Promise<FeedHealth>((resolve) => { resolvePrevious = resolve; });
  const next = { ...health, source: { ...health.source, id: 5, name: 'Next records' } };
  const feedApi: FeedApiPort = {
    listFeeds: vi.fn(), getFeed: vi.fn(), createFeed: vi.fn(), updateFeed: vi.fn(), deleteFeed: vi.fn(),
    getFeedHealth: vi.fn().mockReturnValueOnce(previous).mockResolvedValue(next), fetchFeed: vi.fn(),
  };
  const props = { feedApi, jobs, onClose: vi.fn() };
  const view = render(<FeedHealthMainView {...props} sourceId={4} />);
  view.rerender(<FeedHealthMainView {...props} sourceId={5} />);
  await screen.findByText('Next records');
  await act(async () => { resolvePrevious(health); await previous; });
  expect(screen.getByText('Next records')).toBeInTheDocument();
  expect(screen.queryByText('Records')).not.toBeInTheDocument();
});
