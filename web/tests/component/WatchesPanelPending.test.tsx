// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProjectApiPort } from '../../src/api/ports';
import { ApiError, type WatchInfo, type WatchRunResult } from '../../src/api/open';
import { WatchesPanel } from '../../src/components/WatchesPanel';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const emptyNotificationSummary = {
  schemaVersion: 'frisket.notifications_summary.v1',
  total: 0,
  unseen: 0,
  seen: 0,
  read: 0,
  acknowledged: 0,
  bySeverity: {},
  bySourceKind: {},
  bySourceRef: [],
};
const notificationSetupGuidance = 'You do not have Slack or email notifications set up. Visit settings to set them up.';

function watch(id: number, name: string): WatchInfo {
  return {
    id,
    name,
    scope: 'sheet',
    sheetId: 1,
    query: { kind: 'filter' },
    queryVersion: null,
    queryHash: null,
    enabled: true,
    lastEvaluatedOp: 0,
    lastRunId: null,
    lastStatus: null,
    createdAt: '2026-08-31 00:00:00',
    updatedAt: '2026-08-31 00:00:00',
    latestRun: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const currentGridDraft = {
  query: { kind: 'filter', sheet_id: 1, filter: { status: { eq: 'open' } } },
  scope: { kind: 'sheet' as const, sheet_id: 1 },
  name: 'Watch: Current',
};

const originalShowModal = Object.getOwnPropertyDescriptor(
  HTMLDialogElement.prototype,
  'showModal',
);

beforeEach(() => {
  // jsdom exposes HTMLDialogElement but not its native showModal API. The
  // production component deliberately uses the browser dialog boundary; this
  // minimal harness shim lets the delete interaction reach its public path.
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value() {},
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  if (originalShowModal === undefined) {
    delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal;
  } else {
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
  }
});

describe('WatchesPanel pending actions', () => {
  it('creates an independent Watch from the captured current grid and shows local notification guidance', async () => {
    const projectApi = {
      listWatches: vi.fn().mockResolvedValue([]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      createWatch: vi.fn().mockResolvedValue(watch(10, 'Watch: Current')),
    } as unknown as ProjectApiPort;
    const harness = createWorkspaceTestHarness({ projectId: 'current-grid-watch', api: { projectApi } });
    harness.stores.watchRunLink.setPendingCreateDraft(currentGridDraft);
    harness.render(
      <WatchesPanel
        canEdit
      />,
    );
    expect(await screen.findByText(notificationSetupGuidance)).toBeInTheDocument();
    expect(screen.queryByTestId('watch-create-source-summary')).not.toBeInTheDocument();
    expect(screen.queryByTestId('watch-create-mode')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Notification settings' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('create-watch-button'));
    await waitFor(() => expect(projectApi.createWatch).toHaveBeenCalledWith({
      name: 'Watch: Current',
      query: currentGridDraft.query,
      scope: currentGridDraft.scope,
    }));
    expect(await screen.findByTestId('watch-notification-setup-notice')).toHaveTextContent(notificationSetupGuidance);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss notification setup guidance' }));
    expect(screen.queryByTestId('watch-notification-setup-notice')).not.toBeInTheDocument();
  });

  it('retains an independent Watch draft after a failed create', async () => {
    const projectApi = {
      listWatches: vi.fn().mockResolvedValue([watch(1, 'Open rows')]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      createWatch: vi.fn().mockRejectedValue(new ApiError(500, 'Watch service unavailable')),
    } as unknown as ProjectApiPort;
    const harness = createWorkspaceTestHarness({ projectId: 'sheetless-watches', api: { projectApi } });
    harness.stores.watchRunLink.setPendingCreateDraft(currentGridDraft);

    harness.render(
      <WatchesPanel
        canEdit
      />,
    );

    fireEvent.click(await screen.findByTestId('create-watch-button'));
    await waitFor(() => expect(screen.getByTestId('watches-error')).toHaveTextContent('Watch service unavailable'));

    expect(screen.getByTestId('watch-create-composer')).toBeInTheDocument();
    expect(screen.getByText(notificationSetupGuidance)).toBeInTheDocument();
    expect(screen.getByTestId('create-watch-button')).toBeEnabled();
    expect(screen.getByTestId('run-watch')).toBeEnabled();
    expect(screen.getByTestId('rename-watch')).toBeEnabled();
    expect(screen.getByTestId('pause-watch')).toBeEnabled();
    expect(screen.getByTestId('delete-watch')).toBeEnabled();
  });

  it('keeps each row independently actionable while refusing a duplicate run on one watch', async () => {
    const alpha = watch(1, 'Alpha');
    const beta = watch(2, 'Beta');
    const alphaRun = deferred<WatchRunResult>();
    const betaRun = deferred<WatchRunResult>();
    const projectApi = {
      listWatches: vi.fn().mockResolvedValue([alpha, beta]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      runWatch: vi.fn((watchId: number) => watchId === alpha.id ? alphaRun.promise : betaRun.promise),
    } as unknown as ProjectApiPort;
    const { render } = createWorkspaceTestHarness({ projectId: 'pending-actions', api: { projectApi } });

    render(<WatchesPanel canEdit />);

    await waitFor(() => expect(screen.getAllByTestId('watch-item')).toHaveLength(2));
    const [alphaRow, betaRow] = screen.getAllByTestId('watch-item');
    const alphaButton = within(alphaRow).getByTestId('run-watch');
    const betaButton = within(betaRow).getByTestId('run-watch');

    fireEvent.click(alphaButton);

    expect(alphaButton).toBeDisabled();
    expect(alphaButton).toHaveAccessibleName('Running Alpha…');
    expect(betaButton).toBeEnabled();
    fireEvent.click(betaButton);
    // React has not needed to publish the first pending state for the second
    // row to start: ownership is keyed synchronously by watch id.
    expect(projectApi.runWatch).toHaveBeenCalledTimes(2);

    fireEvent.click(alphaButton);
    expect(projectApi.runWatch).toHaveBeenCalledTimes(2);

    alphaRun.resolve({ watch: alpha } as WatchRunResult);
    betaRun.resolve({ watch: beta } as WatchRunResult);
    await waitFor(() => expect(alphaButton).toBeEnabled());
    expect(betaButton).toBeEnabled();
  });

  it('keeps a draft claim across panel remounts and emits only one create request', async () => {
    const created = watch(3, 'View: Open rows');
    const create = deferred<WatchInfo>();
    const projectApi = {
      listWatches: vi.fn().mockResolvedValue([]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      createWatch: vi.fn(() => create.promise),
    } as unknown as ProjectApiPort;
    const harness = createWorkspaceTestHarness({ projectId: 'create-remount', api: { projectApi } });
    harness.stores.watchRunLink.setPendingCreateDraft(currentGridDraft);

    const first = harness.render(<WatchesPanel canEdit />);
    await waitFor(() => expect(screen.getByTestId('watch-create-composer')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('create-watch-button'));
    expect(projectApi.createWatch).toHaveBeenCalledTimes(1);
    first.unmount();

    harness.render(<WatchesPanel canEdit />);
    const createButton = await screen.findByTestId('create-watch-button');
    expect(createButton).toBeDisabled();
    fireEvent.click(createButton);
    expect(projectApi.createWatch).toHaveBeenCalledTimes(1);

    create.resolve(created);
    await waitFor(() => expect(screen.queryByTestId('watch-create-composer')).not.toBeInTheDocument());
  });

  it('retains a failed create outcome across remount', async () => {
    const create = deferred<WatchInfo>();
    const projectApi = {
      listWatches: vi.fn().mockResolvedValue([]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      createWatch: vi.fn(() => create.promise),
    } as unknown as ProjectApiPort;
    const harness = createWorkspaceTestHarness({ projectId: 'create-failure-remount', api: { projectApi } });
    harness.stores.watchRunLink.setPendingCreateDraft(currentGridDraft);
    const first = harness.render(<WatchesPanel canEdit />);
    fireEvent.click(await screen.findByTestId('create-watch-button'));
    first.unmount();
    create.reject(new Error('Watch service unavailable'));
    await Promise.resolve();
    await Promise.resolve();

    harness.render(<WatchesPanel canEdit />);
    expect(await screen.findByTestId('watches-error')).toHaveTextContent('Watch service unavailable');
    expect(screen.getByTestId('watch-create-composer')).toBeInTheDocument();
  });

  it('keeps a Watch row claim across panel remounts and refuses a duplicate run', async () => {
    const alpha = watch(1, 'Alpha');
    const run = deferred<WatchRunResult>();
    const projectApi = {
      listWatches: vi.fn().mockResolvedValue([alpha]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      runWatch: vi.fn(() => run.promise),
    } as unknown as ProjectApiPort;
    const harness = createWorkspaceTestHarness({ projectId: 'run-remount', api: { projectApi } });

    const first = harness.render(<WatchesPanel canEdit />);
    const firstRun = await screen.findByTestId('run-watch');
    fireEvent.click(firstRun);
    expect(projectApi.runWatch).toHaveBeenCalledTimes(1);
    first.unmount();

    harness.render(<WatchesPanel canEdit />);
    const remountedRun = await screen.findByTestId('run-watch');
    expect(remountedRun).toBeDisabled();
    expect(remountedRun).toHaveAccessibleName('Running Alpha…');
    fireEvent.click(remountedRun);
    expect(projectApi.runWatch).toHaveBeenCalledTimes(1);

    run.resolve({ watch: alpha } as WatchRunResult);
    await waitFor(() => expect(remountedRun).toBeEnabled());
  });

  it('shows a run failure settled while unmounted exactly once after remount', async () => {
    const alpha = watch(1, 'Alpha');
    const run = deferred<WatchRunResult>();
    const projectApi = {
      listWatches: vi.fn().mockResolvedValue([alpha]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      runWatch: vi.fn(() => run.promise),
    } as unknown as ProjectApiPort;
    const harness = createWorkspaceTestHarness({ projectId: 'run-outcome-remount', api: { projectApi } });

    const first = harness.render(<WatchesPanel canEdit />);
    fireEvent.click(await screen.findByTestId('run-watch'));
    expect(projectApi.runWatch).toHaveBeenCalledTimes(1);
    first.unmount();

    run.reject(new Error('Watch service unavailable'));
    await Promise.resolve();
    await Promise.resolve();

    const second = harness.render(<WatchesPanel canEdit />);
    expect(await screen.findByTestId('watches-error')).toHaveTextContent('Watch service unavailable');
    expect(screen.getByTestId('run-watch')).toBeEnabled();
    second.unmount();

    harness.render(<WatchesPanel canEdit />);
    await screen.findByTestId('run-watch');
    expect(screen.queryByTestId('watches-error')).not.toBeInTheDocument();
  });

  it('keeps an in-flight rename visibly owned across a panel remount', async () => {
    const alpha = watch(1, 'Alpha');
    const renamed = watch(1, 'Renamed Alpha');
    const rename = deferred<WatchInfo>();
    let settled = false;
    const projectApi = {
      listWatches: vi.fn(() => Promise.resolve(settled ? [renamed] : [alpha])),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      updateWatch: vi.fn(() => rename.promise),
    } as unknown as ProjectApiPort;
    const harness = createWorkspaceTestHarness({ projectId: 'rename-remount', api: { projectApi } });

    const first = harness.render(<WatchesPanel canEdit />);
    await screen.findByTestId('rename-watch');
    fireEvent.click(screen.getByTestId('rename-watch'));
    fireEvent.change(screen.getByTestId('watch-rename-input'), { target: { value: renamed.name } });
    fireEvent.click(screen.getByTestId('save-watch-rename'));
    expect(projectApi.updateWatch).toHaveBeenCalledTimes(1);
    first.unmount();

    harness.render(<WatchesPanel canEdit />);
    expect(await screen.findByTestId('watch-pending-action')).toHaveTextContent('Renaming…');
    expect(screen.getByTestId('rename-watch')).toBeDisabled();

    settled = true;
    rename.resolve(renamed);
    await waitFor(() => expect(screen.queryByTestId('watch-pending-action')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId('watch-item')).toHaveTextContent(renamed.name));
  });

  it('keeps an in-flight deletion visibly owned across a panel remount', async () => {
    const alpha = watch(1, 'Alpha');
    const remove = deferred<void>();
    let settled = false;
    const projectApi = {
      listWatches: vi.fn(() => Promise.resolve(settled ? [] : [alpha])),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      deleteWatch: vi.fn(() => remove.promise),
    } as unknown as ProjectApiPort;
    const harness = createWorkspaceTestHarness({ projectId: 'delete-remount', api: { projectApi } });

    const first = harness.render(<WatchesPanel canEdit />);
    await screen.findByTestId('delete-watch');
    fireEvent.click(screen.getByTestId('delete-watch'));
    fireEvent.click(await screen.findByTestId('confirm-delete-watch'));
    expect(projectApi.deleteWatch).toHaveBeenCalledTimes(1);
    first.unmount();

    harness.render(<WatchesPanel canEdit />);
    expect(await screen.findByTestId('watch-pending-action')).toHaveTextContent('Deleting…');
    expect(screen.getByTestId('delete-watch')).toBeDisabled();
    expect(screen.queryByTestId('delete-watch-confirmation')).not.toBeInTheDocument();

    settled = true;
    remove.resolve();
    await waitFor(() => expect(screen.queryByTestId('watch-pending-action')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId('watches-empty')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId('watches')).toHaveFocus());
  });

  it('retires a list started during rename when the mutation settles', async () => {
    const alpha = watch(1, 'Alpha');
    const renamed = watch(1, 'Renamed Alpha');
    const staleList = deferred<WatchInfo[]>();
    const rename = deferred<WatchInfo>();
    const projectApi = {
      listWatches: vi.fn()
        .mockResolvedValueOnce([alpha])
        .mockImplementationOnce(() => staleList.promise)
        .mockResolvedValue([renamed]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      updateWatch: vi.fn(() => rename.promise),
    } as unknown as ProjectApiPort;
    const { render } = createWorkspaceTestHarness({ projectId: 'rename-list-race', api: { projectApi } });

    render(<WatchesPanel canEdit />);
    await waitFor(() => expect(screen.getByTestId('watch-item')).toHaveTextContent('Alpha'));
    fireEvent.click(screen.getByTestId('rename-watch'));
    fireEvent.change(screen.getByTestId('watch-rename-input'), { target: { value: 'Renamed Alpha' } });
    fireEvent.click(screen.getByTestId('save-watch-rename'));
    window.dispatchEvent(new Event('frisket:watches-changed'));
    await waitFor(() => expect(projectApi.listWatches).toHaveBeenCalledTimes(2));

    rename.resolve(renamed);
    await waitFor(() => expect(screen.getByText('Renamed Alpha', { exact: true })).toBeInTheDocument());
    staleList.resolve([alpha]);
    await waitFor(() => expect(screen.getByText('Renamed Alpha', { exact: true })).toBeInTheDocument());
    expect(screen.queryByText('Alpha', { exact: true })).not.toBeInTheDocument();
  });
});
