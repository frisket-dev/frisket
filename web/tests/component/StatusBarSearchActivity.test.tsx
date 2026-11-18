// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const useSearchPreparationNotice = vi.hoisted(() => vi.fn());

vi.mock('../../src/components/useSearchPreparationNotice', () => ({
  useSearchPreparationNotice,
}));

import { defineEditionModule, EditionModuleProvider } from '../../src/editions/module';
import { TEAM_EDITION_DESCRIPTOR } from '../../src/editions/posture';
import { StatusBar } from '../../src/components/StatusBar';
import { importActivity, type ImportProgressController } from '../../src/components/useImportProgress';
import type { ImportSessionStatus } from '../../src/api/open';
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

function importProgressController(
  overrides: Partial<ImportProgressController> = {},
): ImportProgressController {
  return {
    activities: [], refresh: vi.fn(async () => {}), message: null, busyRef: null,
    preparingRef: null, retryConsent: null, resume: vi.fn(), stop: vi.fn(async () => {}),
    keep: vi.fn(), remove: vi.fn(), retryFailedPages: vi.fn(), confirmRetry: vi.fn(),
    cancelRetry: vi.fn(), ...overrides,
  };
}

describe('StatusBar search activity', () => {
  it('offers explicit Keep or Remove after cancellation without choosing for the user', () => {
    useSearchPreparationNotice.mockReturnValue(null);
    const session: ImportSessionStatus = {
      import_ref: 'import-cancelled', state: 'cancelled', intake_kind: 'table',
      admitted_files: 1, admitted_bytes: 100, through: 1,
      committed_rows: 3, committed_bytes: 30, processed_rows: 3,
      sheet_id: 1, sheet_name: 'Records', cancel_requested: true, sealed: true,
      error: null, search_preparation_status: 'none', search_preparation_batch_id: null,
      selected_total: 20, preparation_completed: 0, preparation_total: 0,
      ocr_completed: 0, ocr_total: 0, unreadable_total: 0,
      preparation_children: [],
      reading_row_ids: [], waiting_row_ids: [], failed_pages: 0,
    };
    const activity = importActivity(session)!;
    const keep = vi.fn();
    const remove = vi.fn();
    render(<StatusBar run={null} history={null} reviewCount={0} projectionStatus={null}
      importProgress={importProgressController({ activities: [activity], keep, remove })}
      onUndo={vi.fn()} onRedo={vi.fn()} onOpenReview={vi.fn()} onCancelRun={vi.fn()} />);
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    expect(screen.getByRole('button', { name: 'Keep completed work' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Remove imported work' })).toBeVisible();
    expect(keep).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Remove imported work' }));
    expect(remove).toHaveBeenCalledWith(activity);
    expect(keep).not.toHaveBeenCalled();
  });

  it('offers Retry failed pages once nothing waits and gates a priced retry on consent', () => {
    useSearchPreparationNotice.mockReturnValue(null);
    const session: ImportSessionStatus = {
      import_ref: 'import-failed', state: 'completed', intake_kind: 'files',
      admitted_files: 2, admitted_bytes: 100, through: 2,
      committed_rows: 2, committed_bytes: 100,
      sheet_id: 1, sheet_name: 'PDFs', cancel_requested: false, sealed: true,
      error: 'Some PDF pages could not be prepared.', search_preparation_status: 'none',
      search_preparation_batch_id: null,
      selected_total: 2, preparation_completed: 3, preparation_total: 5,
      ocr_completed: 3, ocr_total: 5, unreadable_total: 0,
      preparation_children: [],
      reading_row_ids: [], waiting_row_ids: [], failed_pages: 2,
    };
    const activity = importActivity(session)!;
    const retryFailedPages = vi.fn();
    const confirmRetry = vi.fn();
    const consent = {
      importRef: 'import-failed', planToken: 'retry:plan', confirmation: 'hash-2', pages: 2,
      estimate: {
        cost: 0.02, billed_cost: 20_000, policy_id: 'pdf-ocr-v1', rows: 2,
        requires_confirmation: true, promise_set_hash: 'hash-2',
      },
    };
    const view = render(<StatusBar run={null} history={null} reviewCount={0} projectionStatus={null}
      importProgress={importProgressController({ activities: [activity], retryFailedPages })}
      onUndo={vi.fn()} onRedo={vi.fn()} onOpenReview={vi.fn()} onCancelRun={vi.fn()} />);
    fireEvent.click(screen.getByTestId('search-activity-toggle'));
    expect(screen.getByText('3 of 5 · 2 pages not read')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Stop remaining' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry failed pages' }));
    expect(retryFailedPages).toHaveBeenCalledWith(activity);

    view.rerender(<StatusBar run={null} history={null} reviewCount={0} projectionStatus={null}
      importProgress={importProgressController({ activities: [activity], retryConsent: consent, confirmRetry })}
      onUndo={vi.fn()} onRedo={vi.fn()} onOpenReview={vi.fn()} onCancelRun={vi.fn()} />);
    expect(screen.getByText('Read text again from 2 pages with OCR?')).toBeVisible();
    expect(screen.getByTestId('cost-gate-estimate')).toHaveTextContent('$0.02');
    fireEvent.click(screen.getByTestId('cost-gate-confirm'));
    expect(confirmRetry).toHaveBeenCalledWith(consent);
  });

  it('does not infer model installation from a hosted edition having teams', () => {
    useSearchPreparationNotice.mockReturnValue(null);
    const hosted = defineEditionModule({ descriptor: { ...TEAM_EDITION_DESCRIPTOR, id: 'hosted-test' } });
    render(
      <EditionModuleProvider edition={hosted}>
        <StatusBar run={null} history={null} reviewCount={0} projectionStatus={null}
          onUndo={vi.fn()} onRedo={vi.fn()} onOpenReview={vi.fn()} onCancelRun={vi.fn()} />
      </EditionModuleProvider>,
    );
    expect(useSearchPreparationNotice).toHaveBeenCalledWith('status-search-activity', null);
  });

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
      title: 'Search preparation needs your consent',
      detail: '1 file is waiting for consent',
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
