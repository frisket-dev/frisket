// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProjectQAApi } from '../../src/api/projectQA';
import { ApiError, type AlertInfo, type AlertCheckResult } from '../../src/api/open';
import { AlertsPanel } from '../../src/components/AlertsPanel';
import { createWorkspaceTestHarness, projectApiWith } from '../support/workspaceTestHarness';

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

function projectQAFixture(): ProjectQAApi {
  return { ...projectApiWith({}).qa, citation: vi.fn(), report: vi.fn() };
}

function alert(id: number, name: string): AlertInfo {
  return {
    id,
    name,
    sheetId: 1,
    viewId: id,
    schedule: '@hourly',
  detectionPolicy: { kind: 'conditions', entered: true, exited: false, count_changed: false, fields: [] },
    enabled: true,
    lastEvaluatedOp: 0,
    lastRunId: null,
    lastStatus: null,
    createdAt: '2026-08-31 00:00:00',
    updatedAt: '2026-08-31 00:00:00',
    latestRun: null,
  };
}

function blockedEmbeddingAlert(id: number, name: string): AlertInfo {
  return {
    ...alert(id, name),
    lastStatus: 'error',
    latestRun: {
      id: 3, alertId: id, status: 'error', opCursorBefore: 0, opCursorAfter: 0, matchedRows: 0, newRows: 0,
      error: 'embedding index is stale', errorCode: 'embedding_index_stale', resolvedQueryHash: null,
      resolvedQuery: { similarity: { kind: 'embedding_similarity', embedding_index_id: 'idx-1' } },
      startedAt: '2026-08-31 00:00:00', finishedAt: null,
    },
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
  view_id: 1,
  sheet_id: 1,
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

describe('AlertsPanel pending actions', () => {
  it('describes an enabled threshold-only alert instead of calling it paused', async () => {
    const item = alert(1, 'Threshold alert');
    item.detectionPolicy = { kind: 'conditions', entered: false, exited: false, count_changed: false,
      fields: [], threshold: { kind: 'threshold_crossed', metric: 'matched_rows', threshold: 10, direction: 'at_or_above' } };
    const projectApi = projectApiWith({
      qa: projectQAFixture(), listAlerts: vi.fn().mockResolvedValue([item]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'threshold-alert', api: { projectApi } });
    harness.render(<AlertsPanel canEdit />);
    expect(await screen.findByText('Threshold crossings')).toBeInTheDocument();
    expect(screen.queryByText('Alerts paused')).not.toBeInTheDocument();
  });

  it('changes independent alert conditions on the same saved View', async () => {
    const item = alert(1, 'Open contracts');
    const projectApi = projectApiWith({
      qa: projectQAFixture(), listAlerts: vi.fn().mockResolvedValue([item]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      updateAlert: vi.fn(async (_id, patch) => ({ ...item, detectionPolicy: patch.detection_policy })),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'conditions', api: { projectApi } });
    harness.render(<AlertsPanel canEdit />);
    fireEvent.click(await screen.findByRole('button', { name: 'Conditions' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Rows leaving this view' }));
    fireEvent.change(screen.getByRole('combobox', { name: 'Check frequency' }), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save conditions' }));
    await waitFor(() => expect(projectApi.updateAlert).toHaveBeenCalledWith(1, { detection_policy: { ...item.detectionPolicy, exited: true }, schedule: null }));
    expect(projectApi.updateAlert).toHaveBeenCalledTimes(1);
  });

  it('keeps rename cancellation available for an invalid draft and retains a failed rename', async () => {
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alert(1, 'Alpha')]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      updateAlert: vi.fn().mockRejectedValue(new Error('Rename unavailable')),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'rename-validation', api: { projectApi } });
    harness.render(<AlertsPanel canEdit />);
    fireEvent.click(await screen.findByTestId('rename-alert'));
    fireEvent.change(screen.getByTestId('alert-rename-input'), { target: { value: '   ' } });
    expect(screen.getByTestId('save-alert-rename')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(projectApi.updateAlert).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('rename-alert'));
    fireEvent.change(screen.getByTestId('alert-rename-input'), { target: { value: 'New Alpha' } });
    fireEvent.click(screen.getByTestId('save-alert-rename'));
    expect(await screen.findByTestId('alert-mutation-error')).toHaveTextContent('Rename unavailable');
    expect(screen.getByTestId('alert-rename-input')).toHaveValue('New Alpha');
    expect(screen.getByTestId('save-alert-rename')).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled();
  });

  it('creates an independent Alert from the captured current grid and shows local notification guidance', async () => {
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      createAlert: vi.fn().mockResolvedValue(alert(10, 'Watch: Current')),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'current-grid-watch', api: { projectApi } });
    harness.stores.alertCheckLink.setPendingCreateDraft(currentGridDraft);
    harness.render(
      <AlertsPanel
        canEdit
      />,
    );
    expect(await screen.findByText(notificationSetupGuidance)).toBeInTheDocument();
    expect(screen.queryByTestId('alert-create-source-summary')).not.toBeInTheDocument();
    expect(screen.queryByTestId('alert-create-mode')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Notification settings' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('create-alert-button'));
    await waitFor(() => expect(projectApi.createAlert).toHaveBeenCalledWith({
      name: 'Watch: Current',
      view_id: currentGridDraft.view_id,
      detection_policy: undefined,
      schedule: '@hourly',
    }));
    expect(await screen.findByTestId('alert-notification-setup-notice')).toHaveTextContent(notificationSetupGuidance);
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss notification setup guidance' }));
    expect(screen.queryByTestId('alert-notification-setup-notice')).not.toBeInTheDocument();
  });

  it('retains an independent Alert draft after a failed create', async () => {
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alert(1, 'Open rows')]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      createAlert: vi.fn().mockRejectedValue(new ApiError(500, 'Watch service unavailable')),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'sheetless-watches', api: { projectApi } });
    harness.stores.alertCheckLink.setPendingCreateDraft(currentGridDraft);

    harness.render(
      <AlertsPanel
        canEdit
      />,
    );

    fireEvent.click(await screen.findByTestId('create-alert-button'));
    await waitFor(() => expect(screen.getByTestId('alerts-error')).toHaveTextContent('Watch service unavailable'));

    expect(screen.getByTestId('alert-create-composer')).toBeInTheDocument();
    expect(screen.getByText(notificationSetupGuidance)).toBeInTheDocument();
    expect(screen.getByTestId('create-alert-button')).toBeEnabled();
    expect(screen.getByTestId('check-alert')).toBeEnabled();
    expect(screen.getByTestId('rename-alert')).toBeEnabled();
    expect(screen.getByTestId('pause-alert')).toBeEnabled();
    expect(screen.getByTestId('delete-alert')).toBeEnabled();
  });

  it('keeps each row independently actionable while refusing a duplicate run on one alert', async () => {
    const alpha = alert(1, 'Alpha');
    const beta = alert(2, 'Beta');
    const alphaRun = deferred<AlertCheckResult>();
    const betaRun = deferred<AlertCheckResult>();
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alpha, beta]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      checkAlert: vi.fn((alertId: number) => alertId === alpha.id ? alphaRun.promise : betaRun.promise),
    });
    const { render } = createWorkspaceTestHarness({ projectId: 'pending-actions', api: { projectApi } });

    render(<AlertsPanel canEdit />);

    await waitFor(() => expect(screen.getAllByTestId('alert-item')).toHaveLength(2));
    const [alphaRow, betaRow] = screen.getAllByTestId('alert-item');
    const alphaButton = within(alphaRow).getByTestId('check-alert');
    const betaButton = within(betaRow).getByTestId('check-alert');

    fireEvent.click(alphaButton);

    expect(alphaButton).toBeDisabled();
    expect(alphaButton).toHaveAccessibleName('Checking Alpha…');
    expect(betaButton).toBeEnabled();
    fireEvent.click(betaButton);
    // React has not needed to publish the first pending state for the second
    // row to start: ownership is keyed synchronously by alert id.
    expect(projectApi.checkAlert).toHaveBeenCalledTimes(2);

    fireEvent.click(alphaButton);
    expect(projectApi.checkAlert).toHaveBeenCalledTimes(2);

    alphaRun.resolve({ alert: alpha } as AlertCheckResult);
    betaRun.resolve({ alert: beta } as AlertCheckResult);
    await waitFor(() => expect(alphaButton).toBeEnabled());
    expect(betaButton).toBeEnabled();
  });

  it('keeps a draft claim across panel remounts and emits only one create request', async () => {
    const created = alert(3, 'View: Open rows');
    const create = deferred<AlertInfo>();
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      createAlert: vi.fn(() => create.promise),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'create-remount', api: { projectApi } });
    harness.stores.alertCheckLink.setPendingCreateDraft(currentGridDraft);

    const first = harness.render(<AlertsPanel canEdit />);
    await waitFor(() => expect(screen.getByTestId('alert-create-composer')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('create-alert-button'));
    expect(projectApi.createAlert).toHaveBeenCalledTimes(1);
    first.unmount();

    harness.render(<AlertsPanel canEdit />);
    const createButton = await screen.findByTestId('create-alert-button');
    expect(createButton).toBeDisabled();
    fireEvent.click(createButton);
    expect(projectApi.createAlert).toHaveBeenCalledTimes(1);

    create.resolve(created);
    await waitFor(() => expect(screen.queryByTestId('alert-create-composer')).not.toBeInTheDocument());
  });

  it('retains a failed create outcome across remount', async () => {
    const create = deferred<AlertInfo>();
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      createAlert: vi.fn(() => create.promise),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'create-failure-remount', api: { projectApi } });
    harness.stores.alertCheckLink.setPendingCreateDraft(currentGridDraft);
    const first = harness.render(<AlertsPanel canEdit />);
    fireEvent.click(await screen.findByTestId('create-alert-button'));
    first.unmount();
    create.reject(new Error('Watch service unavailable'));
    await Promise.resolve();
    await Promise.resolve();

    harness.render(<AlertsPanel canEdit />);
    expect(await screen.findByTestId('alerts-error')).toHaveTextContent('Watch service unavailable');
    expect(screen.getByTestId('alert-create-composer')).toBeInTheDocument();
  });

  it('keeps an Alert row claim across panel remounts and refuses a duplicate run', async () => {
    const alpha = alert(1, 'Alpha');
    const run = deferred<AlertCheckResult>();
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alpha]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      checkAlert: vi.fn(() => run.promise),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'run-remount', api: { projectApi } });

    const first = harness.render(<AlertsPanel canEdit />);
    const firstRun = await screen.findByTestId('check-alert');
    fireEvent.click(firstRun);
    expect(projectApi.checkAlert).toHaveBeenCalledTimes(1);
    first.unmount();

    harness.render(<AlertsPanel canEdit />);
    const remountedRun = await screen.findByTestId('check-alert');
    expect(remountedRun).toBeDisabled();
    expect(remountedRun).toHaveAccessibleName('Checking Alpha…');
    fireEvent.click(remountedRun);
    expect(projectApi.checkAlert).toHaveBeenCalledTimes(1);

    run.resolve({ alert: alpha } as AlertCheckResult);
    await waitFor(() => expect(remountedRun).toBeEnabled());
  });

  it('shows a run failure settled while unmounted exactly once after remount', async () => {
    const alpha = alert(1, 'Alpha');
    const run = deferred<AlertCheckResult>();
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alpha]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      checkAlert: vi.fn(() => run.promise),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'run-outcome-remount', api: { projectApi } });

    const first = harness.render(<AlertsPanel canEdit />);
    fireEvent.click(await screen.findByTestId('check-alert'));
    expect(projectApi.checkAlert).toHaveBeenCalledTimes(1);
    first.unmount();

    run.reject(new Error('Watch service unavailable'));
    await Promise.resolve();
    await Promise.resolve();

    const second = harness.render(<AlertsPanel canEdit />);
    expect(await screen.findByTestId('alerts-error')).toHaveTextContent('Watch service unavailable');
    expect(screen.getByTestId('check-alert')).toBeEnabled();
    second.unmount();

    harness.render(<AlertsPanel canEdit />);
    await screen.findByTestId('check-alert');
    expect(screen.queryByTestId('alerts-error')).not.toBeInTheDocument();
  });

  it('keeps an in-flight rename visibly owned across a panel remount', async () => {
    const alpha = alert(1, 'Alpha');
    const renamed = alert(1, 'Renamed Alpha');
    const rename = deferred<AlertInfo>();
    let settled = false;
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn(() => Promise.resolve(settled ? [renamed] : [alpha])),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      updateAlert: vi.fn(() => rename.promise),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'rename-remount', api: { projectApi } });

    const first = harness.render(<AlertsPanel canEdit />);
    await screen.findByTestId('rename-alert');
    fireEvent.click(screen.getByTestId('rename-alert'));
    fireEvent.change(screen.getByTestId('alert-rename-input'), { target: { value: renamed.name } });
    fireEvent.click(screen.getByTestId('save-alert-rename'));
    expect(projectApi.updateAlert).toHaveBeenCalledTimes(1);
    first.unmount();

    harness.render(<AlertsPanel canEdit />);
    expect(await screen.findByTestId('alert-pending-action')).toHaveTextContent('Renaming…');
    expect(screen.getByTestId('rename-alert')).toBeDisabled();

    settled = true;
    rename.resolve(renamed);
    await waitFor(() => expect(screen.queryByTestId('alert-pending-action')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId('alert-item')).toHaveTextContent(renamed.name));
  });

  it('keeps a pending delete confirmation open after forced closure and restores Cancel focus', async () => {
    const remove = deferred<void>();
    const projectApi = projectApiWith({
      qa: projectQAFixture(), listAlerts: vi.fn().mockResolvedValue([alert(1, 'Alpha')]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      deleteAlert: vi.fn(() => remove.promise),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'delete-dialog-owner', api: { projectApi } });
    harness.render(<AlertsPanel canEdit />);
    const trigger = await screen.findByTestId('delete-alert');
    fireEvent.click(trigger);
    fireEvent.click(screen.getByTestId('confirm-delete-alert'));
    const dialog = screen.getByTestId('delete-alert-confirmation') as HTMLDialogElement;
    dialog.showModal = vi.fn(() => { dialog.setAttribute('open', ''); });
    dialog.close = () => { dialog.removeAttribute('open'); fireEvent(dialog, new Event('close')); };
    expect(dialog).toHaveAttribute('aria-busy', 'true');
    fireEvent(dialog, new Event('close'));
    expect(dialog.showModal).toHaveBeenCalledTimes(1);
    fireEvent(dialog, new Event('cancel', { bubbles: true, cancelable: true }));
    expect(dialog).toHaveAttribute('open');
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    remove.reject(new Error('Delete unavailable'));
    expect(await screen.findByTestId('alert-mutation-error')).toHaveTextContent('Delete unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it('keeps an in-flight deletion visibly owned across a panel remount', async () => {
    const alpha = alert(1, 'Alpha');
    const remove = deferred<void>();
    let settled = false;
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn(() => Promise.resolve(settled ? [] : [alpha])),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      deleteAlert: vi.fn(() => remove.promise),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'delete-remount', api: { projectApi } });

    const first = harness.render(<AlertsPanel canEdit />);
    await screen.findByTestId('delete-alert');
    fireEvent.click(screen.getByTestId('delete-alert'));
    fireEvent.click(await screen.findByTestId('confirm-delete-alert'));
    expect(projectApi.deleteAlert).toHaveBeenCalledTimes(1);
    first.unmount();

    harness.render(<AlertsPanel canEdit />);
    expect(await screen.findByTestId('alert-pending-action')).toHaveTextContent('Deleting…');
    expect(screen.getByTestId('delete-alert')).toBeDisabled();
    expect(screen.queryByTestId('delete-alert-confirmation')).not.toBeInTheDocument();

    settled = true;
    remove.resolve();
    await waitFor(() => expect(screen.queryByTestId('alert-pending-action')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId('alerts-empty')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId('alerts')).toHaveFocus());
  });

  it('retires a list started during rename when the mutation settles', async () => {
    const alpha = alert(1, 'Alpha');
    const renamed = alert(1, 'Renamed Alpha');
    const staleList = deferred<AlertInfo[]>();
    const rename = deferred<AlertInfo>();
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn()
        .mockResolvedValueOnce([alpha])
        .mockImplementationOnce(() => staleList.promise)
        .mockResolvedValue([renamed]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      updateAlert: vi.fn(() => rename.promise),
    });
    const { render } = createWorkspaceTestHarness({ projectId: 'rename-list-race', api: { projectApi } });

    render(<AlertsPanel canEdit />);
    await waitFor(() => expect(screen.getByTestId('alert-item')).toHaveTextContent('Alpha'));
    fireEvent.click(screen.getByTestId('rename-alert'));
    fireEvent.change(screen.getByTestId('alert-rename-input'), { target: { value: 'Renamed Alpha' } });
    fireEvent.click(screen.getByTestId('save-alert-rename'));
    window.dispatchEvent(new Event('frisket:alerts-changed'));
    await waitFor(() => expect(projectApi.listAlerts).toHaveBeenCalledTimes(2));

    rename.resolve(renamed);
    await waitFor(() => expect(screen.getByText('Renamed Alpha', { exact: true })).toBeInTheDocument());
    staleList.resolve([alpha]);
    await waitFor(() => expect(screen.getByText('Renamed Alpha', { exact: true })).toBeInTheDocument());
    expect(screen.queryByText('Alpha', { exact: true })).not.toBeInTheDocument();
  });

  it('refreshes a blocked embedding index before re-running the alert and pins each step', async () => {
    const alpha = blockedEmbeddingAlert(1, 'Alpha');
    const refresh = deferred<{ jobId: number | null; status: string }>();
    const run = deferred<AlertCheckResult>();
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alpha]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      refreshEmbeddingIndex: vi.fn(() => refresh.promise),
      checkAlert: vi.fn(() => run.promise),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'refresh-index', api: { projectApi } });
    const { container } = harness.render(<AlertsPanel canEdit />);
    const button = await screen.findByTestId('alert-refresh-index-1');
    const steps = [container.innerHTML];

    fireEvent.click(button);
    expect(projectApi.refreshEmbeddingIndex).toHaveBeenCalledWith('idx-1', 'incremental');
    expect(projectApi.checkAlert).not.toHaveBeenCalled();
    fireEvent.click(button);
    expect(projectApi.refreshEmbeddingIndex).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(button).toHaveTextContent('Refreshing index…'));
    steps.push(container.innerHTML);

    refresh.resolve({ jobId: null, status: 'completed' });
    await waitFor(() => expect(projectApi.checkAlert).toHaveBeenCalledWith(1));
    steps.push(container.innerHTML);

    run.resolve({ alert: { ...alpha, name: 'Alpha refreshed', latestRun: null } } as AlertCheckResult);
    await waitFor(() => expect(screen.getByText('Alpha refreshed', { exact: true })).toBeInTheDocument());
    steps.push(container.innerHTML);
    expect(steps).toMatchSnapshot();
  });

  it('settles a failed index refresh without running the alert', async () => {
    const alpha = blockedEmbeddingAlert(1, 'Alpha');
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alpha]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      refreshEmbeddingIndex: vi.fn().mockRejectedValue(new Error('Index refresh unavailable')),
      checkAlert: vi.fn(),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'refresh-index-failure', api: { projectApi } });
    const { container } = harness.render(<AlertsPanel canEdit />);
    fireEvent.click(await screen.findByTestId('alert-refresh-index-1'));
    expect(await screen.findByTestId('alerts-error')).toHaveTextContent('Index refresh unavailable');
    await waitFor(() => expect(screen.getByTestId('alert-refresh-index-1')).toBeEnabled());
    expect(projectApi.checkAlert).not.toHaveBeenCalled();
    expect(container.innerHTML).toMatchSnapshot();
  });

  it('confirms a 402 embedding refresh cost gate before re-running the blocked alert', async () => {
    const { ConfirmationRequiredError } = await import('../../src/api/open');
    const alpha = blockedEmbeddingAlert(1, 'Alpha');
    const estimate = {
      cost: 0.08,
      billed_cost: 80_000,
      policy_id: 'frisket.pricing.identity.v1',
      rows: 8,
      promise_set_hash: '9'.repeat(64),
    };
    const refreshEmbeddingIndex = vi.fn()
      .mockRejectedValueOnce(new ConfirmationRequiredError(estimate, 'Confirm remote embedding refresh.'))
      .mockResolvedValueOnce({ jobId: null, status: 'completed' });
    const checkAlert = vi.fn().mockResolvedValue({
      alert: { ...alpha, name: 'Alpha confirmed', latestRun: null },
    } as AlertCheckResult);
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alpha]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      refreshEmbeddingIndex,
      checkAlert,
    });
    const harness = createWorkspaceTestHarness({ projectId: 'refresh-index-confirm', api: { projectApi } });
    harness.render(<AlertsPanel canEdit />);

    fireEvent.click(await screen.findByTestId('alert-refresh-index-1'));
    await waitFor(() => expect(harness.stores.job.store.get().costGate).not.toBeNull());
    expect(checkAlert).not.toHaveBeenCalled();

    harness.stores.job.confirmCostGate();
    await waitFor(() => {
      expect(refreshEmbeddingIndex).toHaveBeenCalledWith('idx-1', 'incremental', {
        confirmation: '9'.repeat(64),
      });
    });
    await waitFor(() => expect(checkAlert).toHaveBeenCalledWith(1));
  });

  it('aborts alert refresh cleanly when the user cancels the cost gate', async () => {
    const { ConfirmationRequiredError } = await import('../../src/api/open');
    const alpha = blockedEmbeddingAlert(1, 'Alpha');
    const estimate = {
      cost: 0.08,
      billed_cost: 80_000,
      policy_id: 'frisket.pricing.identity.v1',
      rows: 8,
      promise_set_hash: '8'.repeat(64),
    };
    const refreshEmbeddingIndex = vi.fn().mockRejectedValueOnce(
      new ConfirmationRequiredError(estimate, 'Confirm remote embedding refresh.'),
    );
    const projectApi = projectApiWith({
      qa: projectQAFixture(),
      listAlerts: vi.fn().mockResolvedValue([alpha]),
      getNotificationsSummary: vi.fn().mockResolvedValue(emptyNotificationSummary),
      refreshEmbeddingIndex,
      checkAlert: vi.fn(),
    });
    const harness = createWorkspaceTestHarness({ projectId: 'refresh-index-cancel', api: { projectApi } });
    harness.render(<AlertsPanel canEdit />);

    fireEvent.click(await screen.findByTestId('alert-refresh-index-1'));
    await waitFor(() => expect(harness.stores.job.store.get().costGate).not.toBeNull());

    harness.stores.job.cancelCostGate();
    await waitFor(() => expect(screen.getByTestId('alert-refresh-index-1')).toBeEnabled());
    expect(refreshEmbeddingIndex).toHaveBeenCalledTimes(1);
    expect(projectApi.checkAlert).not.toHaveBeenCalled();
    expect(screen.queryByTestId('alerts-error')).not.toBeInTheDocument();
  });
});
