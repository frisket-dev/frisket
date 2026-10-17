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
  it('keeps queued indexing in the bottom-left control until manually opened', () => {
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
    expect(activity).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();

    fireEvent.click(activity);
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();
    expect(activity).toHaveAttribute('aria-expanded', 'true');

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

    expect(screen.queryByRole('button', { name: 'Review text' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
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
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    fireEvent.click(screen.getByTestId('search-preparation-dismiss'));
    view.rerender(statusBar({ ...run, status: 'complete', completedRows: 2 }));
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();
  });

  it('stays minimized through phases, completion, and new failures until manually opened', () => {
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
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
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
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
    expect(screen.getByTestId('search-activity-toggle')).toHaveTextContent('Search preparation needs attention');
    expect(screen.getByTestId('search-activity-toggle')).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();
  });

  it.each(['search', 'action'] as const)('minimizes and reopens only %s when both activities are visible', (activity) => {
    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'preparation:prep-44', reviewBatchId: 'prep-44',
      stage: 'prepare', tone: 'active', title: 'Preparing search',
      detail: 'Reading scanned PDFs · 1 file', progress: null, details: [],
    });
    const run: RunProgress = {
      runId: 'concurrent-run', actionName: 'Translate', actionKind: 'map.translate',
      sheetId: '1', targetColumnId: 'translated', status: 'running',
      completedRows: 1, totalRows: 2, failedRows: 0, costSoFar: 0,
    };
    render(
      <StatusBar
        run={run} history={null} reviewCount={0} projectionStatus={null}
        onUndo={vi.fn()} onRedo={vi.fn()} onOpenReview={vi.fn()} onCancelRun={vi.fn()}
      />,
    );
    expect(screen.getByTestId('active-run-banner')).toBeVisible();
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();

    const dismissedBanner = activity === 'search' ? 'search-preparation-banner' : 'active-run-banner';
    const remainingBanner = activity === 'search' ? 'active-run-banner' : 'search-preparation-banner';
    const dismiss = activity === 'search' ? 'search-preparation-dismiss' : 'active-run-dismiss';
    const toggle = activity === 'search' ? 'search-activity-toggle' : 'run-watcher-toggle';
    const otherToggle = activity === 'search' ? 'run-watcher-toggle' : 'search-activity-toggle';
    fireEvent.click(screen.getByTestId(dismiss));
    expect(screen.queryByTestId(dismissedBanner)).not.toBeInTheDocument();
    expect(screen.getByTestId(remainingBanner)).toBeVisible();
    expect(screen.getByTestId(toggle)).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByTestId(otherToggle)).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(screen.getByTestId(toggle));
    expect(screen.getByTestId(dismissedBanner)).toBeVisible();
    expect(screen.getByTestId(remainingBanner)).toBeVisible();
  });

  it('opens only each new action after search and an earlier action were minimized', () => {
    const notice = {
      activityKey: 'preparation:prep-45', reviewBatchId: 'prep-45',
      stage: 'prepare', tone: 'active', title: 'Preparing search',
      detail: 'Reading scanned PDFs · 1 file', progress: null, details: [],
    };
    useSearchPreparationNotice.mockReturnValue(notice);
    const run: RunProgress = {
      runId: 'first-run', actionName: 'Translate', actionKind: 'map.translate',
      sheetId: '1', targetColumnId: 'translated', status: 'running',
      completedRows: 1, totalRows: 2, failedRows: 0, costSoFar: 0,
    };
    const statusBar = (currentRun: RunProgress | null) => (
      <StatusBar
        run={currentRun} history={null} reviewCount={0} projectionStatus={null}
        onUndo={vi.fn()} onRedo={vi.fn()} onOpenReview={vi.fn()} onCancelRun={vi.fn()}
      />
    );
    const view = render(statusBar(null));
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    fireEvent.click(screen.getByTestId('search-preparation-dismiss'));

    view.rerender(statusBar(run));
    expect(screen.getByTestId('active-run-banner')).toBeVisible();
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('active-run-dismiss'));

    useSearchPreparationNotice.mockReturnValue({ ...notice, activityKey: 'index:46', stage: 'index' });
    view.rerender(statusBar({ ...run, completedRows: 2 }));
    expect(screen.queryByTestId('active-run-banner')).not.toBeInTheDocument();
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();
    expect(screen.queryByTestId('active-run-banner')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('search-preparation-dismiss'));

    view.rerender(statusBar({ ...run, runId: 'second-run', completedRows: 0 }));
    expect(screen.getByTestId('active-run-banner')).toBeVisible();
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
  });

});
