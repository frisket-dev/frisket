// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const useSearchPreparationNotice = vi.hoisted(() => vi.fn());

vi.mock('../../src/components/useSearchPreparationNotice', () => ({
  useSearchPreparationNotice,
}));

import { StatusBar } from '../../src/components/StatusBar';
import { createProjectApi } from '../../src/api/real';
import type { RunProgress } from '../../src/api/open';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const { render, stores } = createWorkspaceTestHarness({
  projectId: 'status-search-activity',
  api: { projectApi: createProjectApi('status-search-activity') },
});

afterEach(() => {
  cleanup();
  stores.actSurface.closeImportDialog();
  vi.clearAllMocks();
});

describe('StatusBar search activity', () => {
  it('uses the bottom-left activity control to dismiss and reopen queued indexing without calling it idle', () => {
    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'index:41',
      reviewBatchId: null,
      stage: 'index',
      tone: 'active',
      title: 'Preparing search',
      detail: 'Search indexing queued',
      progress: null,
      details: [],
    });
    render(
      <StatusBar
        run={null}
        history={null}
        reviewCount={0}
        projectionStatus={null}
        onUndo={vi.fn()}
        onRedo={vi.fn()}
        onOpenReview={vi.fn()}
        onCancelRun={vi.fn()}
      />,
    );

    expect(screen.queryByText('idle')).not.toBeInTheDocument();
    const activity = screen.getByTestId('search-activity-toggle');
    expect(activity).toHaveTextContent('Preparing search · Search indexing queued');
    expect(activity).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();

    fireEvent.click(screen.getByTestId('search-preparation-dismiss'));
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
    expect(activity).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(activity);
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();
    expect(activity).toHaveAttribute('aria-expanded', 'true');
  });

  it('reopens the matching preparation receipt in the import workspace', () => {
    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'preparation:prep-42',
      reviewBatchId: 'prep-42',
      stage: 'approval',
      tone: 'warning',
      title: 'Search preparation needs approval',
      detail: '1 file is waiting for approval',
      progress: null,
      details: [],
    });
    render(
      <StatusBar
        run={null}
        history={null}
        reviewCount={0}
        projectionStatus={null}
        onUndo={vi.fn()}
        onRedo={vi.fn()}
        onOpenReview={vi.fn()}
        onCancelRun={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Review text' }));
    expect(stores.actSurface.store.get()).toMatchObject({
      importDialogOpen: true,
      importPreparationBatchId: 'prep-42',
    });
  });

  it('keeps preparation dismissed when a concurrent tracked run finishes', () => {
    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'preparation:prep-43', reviewBatchId: 'prep-43',
      stage: 'prepare', tone: 'active', title: 'Preparing search',
      detail: 'Reading scanned PDFs · 1 file', progress: null, details: [],
    });
    const run: RunProgress = {
      runId: 'concurrent-run', actionName: 'Translate', actionKind: 'map.translate',
      sheetId: '1', targetColumnId: 'translated', status: 'running',
      completedRows: 1, totalRows: 2, failedRows: 0, costSoFar: 0,
    };
    const statusBar = (currentRun: RunProgress) => (
      <StatusBar
        run={currentRun} history={null} reviewCount={0} projectionStatus={null}
        onUndo={vi.fn()} onRedo={vi.fn()} onOpenReview={vi.fn()} onCancelRun={vi.fn()}
      />
    );
    const view = render(statusBar(run));
    fireEvent.click(screen.getByTestId('search-preparation-dismiss'));
    view.rerender(statusBar({ ...run, status: 'complete', completedRows: 2 }));
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();
  });

  it('stays dismissed through normal phases and completion, then reopens for a new failure', () => {
    const renderStatusBar = () => (
      <StatusBar
        run={null}
        history={null}
        reviewCount={0}
        projectionStatus={null}
        onUndo={vi.fn()}
        onRedo={vi.fn()}
        onOpenReview={vi.fn()}
        onCancelRun={vi.fn()}
      />
    );
    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'preparation:prep-43',
      reviewBatchId: 'prep-43',
      stage: 'prepare',
      tone: 'active',
      title: 'Preparing search',
      detail: 'Reading scanned PDFs · 1 file',
      progress: 50,
      details: [],
    });
    const view = render(renderStatusBar());
    fireEvent.click(screen.getByTestId('search-preparation-dismiss'));

    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'preparation:prep-43',
      reviewBatchId: 'prep-43',
      stage: 'index',
      tone: 'active',
      title: 'Preparing search',
      detail: 'Indexing search content',
      progress: null,
      details: [],
    });
    view.rerender(renderStatusBar());
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();

    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'preparation:prep-43',
      reviewBatchId: null,
      stage: 'complete',
      tone: 'complete',
      title: 'Search index ready',
      detail: 'Search indexing finished.',
      progress: 100,
      details: [],
    });
    view.rerender(renderStatusBar());
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();

    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'preparation:prep-43:failed',
      reviewBatchId: 'prep-43',
      stage: 'failed',
      tone: 'error',
      title: 'Search preparation needs attention',
      detail: '1 of 1 file failed',
      progress: 100,
      details: [],
    });
    view.rerender(renderStatusBar());
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();
  });

});
