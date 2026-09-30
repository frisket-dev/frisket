// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ColumnRun, ColumnRunsInfo } from '../../src/api/types';
import { ColumnDrawer } from '../../src/components/ColumnDrawer';
import { columnDef } from '../support/domainFixtures';

const projectApi = vi.hoisted(() => ({
  getColumnRuns: vi.fn(),
  getColumnStats: vi.fn(),
  listColumnTypes: vi.fn(),
}));

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ projectApi, gridView: { store: {} } }),
}));

vi.mock('../../src/bind/useSelector', () => ({
  useSelector: () => ({}),
}));

const genericRun: ColumnRun = {
  runId: '9', actionKind: 'map.classify', actionName: 'Classify', model: 'model',
  status: 'completed', spec: {}, totalRows: 1, completedRows: 1, failedRows: 0,
  cost: 0, startedAt: '2026-09-01T12:00:00Z', finishedAt: '2026-09-01T12:00:01Z',
  durationMs: 1_000, tokensIn: 1, tokensOut: 1, current: true,
  humanScore: { passed: 1, graded: 1 }, judgeScores: [],
};

function columnRuns(currentRun: ColumnRun): ColumnRunsInfo {
  return {
    columnName: 'match', offset: 0, limit: 20, totalRuns: 1, hasMore: false,
    nextOffset: null, currentRun, currentRunLoaded: true, latestRun: currentRun,
    latestRunLoaded: true, mixedOrigins: false, runs: [currentRun],
  };
}

beforeEach(() => {
  projectApi.getColumnRuns.mockReset();
  projectApi.getColumnStats.mockReset().mockResolvedValue({
    column: { format: null }, rowCount: 1, present: 1, missing: 0,
  });
  projectApi.listColumnTypes.mockReset().mockResolvedValue([]);
});

afterEach(cleanup);

function drawer() {
  return render(
    <ColumnDrawer
      column={columnDef({ id: '2', name: 'match', type: 'text', ai: {
        actionName: 'AI run', prompt: '', model: '', costSoFar: 0, versions: [],
      } })}
      sheetId="1"
      onClose={vi.fn()}
      onBackfillComplete={vi.fn()}
      onColumnUpdated={vi.fn()}
      onReviewRun={vi.fn()}
      onReviseRun={vi.fn()}
      requestCostConfirmation={vi.fn()}
    />,
  );
}

describe('ColumnDrawer map.find recovery controls', () => {
  it('does not offer unsupported backfill or revision for grounded-find output', async () => {
    projectApi.getColumnRuns.mockResolvedValue(columnRuns({ ...genericRun, actionKind: 'map.find', actionName: 'Find grounded occurrences' }));
    drawer();

    await waitFor(() => expect(projectApi.getColumnRuns).toHaveBeenCalledWith('2', 0, 20));
    await screen.findByText('Find grounded occurrences');
    expect(screen.queryByTestId('backfill-column-button')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Revise 1 reviewed/ })).not.toBeInTheDocument();
  });

  it('keeps recovery controls for ordinary AI output', async () => {
    projectApi.getColumnRuns.mockResolvedValue(columnRuns(genericRun));
    drawer();

    expect(await screen.findByTestId('backfill-column-button')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Revise 1 reviewed' })).toBeInTheDocument();
  });
});
