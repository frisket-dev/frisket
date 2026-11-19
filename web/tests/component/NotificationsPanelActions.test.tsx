// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { NotificationItem } from '../../src/api/types';
import { NotificationsPanel } from '../../src/components/NotificationsPanel';
import { NOTIFICATIONS_CHANGED_EVENT } from '../../src/components/notifications/useNotificationSummary';
import { createWorkspaceTestHarness, projectApiWith } from '../support/workspaceTestHarness';

function notification(id: number, state: NotificationItem['state'] = 'unseen'): NotificationItem {
  return {
    id, state, sourceKind: 'watch', sourceRef: { watch_id: 3 }, sourceEventIds: [11],
    eventCount: 1, eventKinds: ['row_entered'], title: `Watch ${id}`, summary: 'New match',
    severity: 'info', deepLink: { kind: 'watch_run', watch_id: 3, run_id: 5, event_ids: [11] },
    createdAt: '', updatedAt: '', seenAt: null, readAt: null, acknowledgedAt: null, acknowledgedBy: null,
  };
}

function renderNotifications(items: NotificationItem[]) {
  const projectApi = {
    qa: { ...projectApiWith({}).qa, citation: vi.fn(), report: vi.fn() },
    listNotifications: vi.fn().mockResolvedValue({ notifications: items }),
    getNotificationsSummary: vi.fn().mockResolvedValue({ unseen: 1 }),
    markNotificationsSeen: vi.fn().mockResolvedValue({ seenCount: 1 }),
    markNotificationRead: vi.fn(),
    ackNotification: vi.fn(),
    unackNotification: vi.fn(),
  };
  const harness = createWorkspaceTestHarness({
    projectId: 'notification-actions', api: { projectApi: projectApiWith(projectApi) },
  });
  harness.render(<NotificationsPanel />);
  return { ...harness, api: projectApi };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it.each([
  ['notification-seen', 'markNotificationsSeen', 'unseen', 'seen'],
  ['notification-ack', 'ackNotification', 'unseen', 'acknowledged'],
  ['notification-unack', 'unackNotification', 'acknowledged', 'read'],
  ['notification-read', 'markNotificationRead', 'unseen', 'read'],
] as const)('%s retains the state change event and refresh', async (testId, method, initialState, nextState) => {
  const { api } = renderNotifications([notification(1, initialState)]);
  api[method].mockResolvedValue({ notificationId: 1, state: nextState });
  const dispatch = vi.spyOn(window, 'dispatchEvent');
  fireEvent.click(await screen.findByTestId(testId));
  await waitFor(() => expect(api[method]).toHaveBeenCalledWith(
    method === 'markNotificationsSeen' ? { notificationIds: [1] } : 1,
  ));
  await waitFor(() => expect(dispatch.mock.calls.filter(([event]) => event.type === NOTIFICATIONS_CHANGED_EVENT)).toHaveLength(1));
  expect(api.listNotifications.mock.calls.length).toBeGreaterThan(1);
});

it('releases a failed row action while another notification remains actionable', async () => {
  const { api } = renderNotifications([notification(1), notification(2)]);
  let reject!: (error: Error) => void;
  api.ackNotification.mockReturnValue(new Promise((_, rejectRequest) => { reject = rejectRequest; }));
  await screen.findAllByTestId('notification-item');
  const [first, second] = screen.getAllByTestId('notification-item');
  const acknowledge = within(first).getByTestId('notification-ack');
  fireEvent.click(acknowledge);
  expect(acknowledge).toBeDisabled();
  expect(within(second).getByTestId('notification-ack')).toBeEnabled();
  reject(new Error('Could not acknowledge'));
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not acknowledge');
  await waitFor(() => expect(acknowledge).toBeEnabled());
});

it('keeps the second row pending when the first concurrent action completes', async () => {
  const { api } = renderNotifications([notification(1), notification(2)]);
  let resolveFirst!: (value: unknown) => void;
  let resolveSecond!: (value: unknown) => void;
  api.ackNotification.mockImplementation((id: number) => new Promise((resolve) => {
    if (id === 1) resolveFirst = resolve;
    else resolveSecond = resolve;
  }));
  const [first, second] = await screen.findAllByTestId('notification-item');
  fireEvent.click(within(first).getByTestId('notification-ack'));
  fireEvent.click(within(second).getByTestId('notification-ack'));
  resolveFirst({ notificationId: 1, state: 'acknowledged' });
  await waitFor(() => expect(within(first).getByTestId('notification-open-source')).toBeEnabled());
  expect(within(second).getByTestId('notification-open-source')).toBeDisabled();
  fireEvent.click(within(second).getByTestId('notification-ack'));
  expect(api.ackNotification).toHaveBeenCalledTimes(2);
  resolveSecond({ notificationId: 2, state: 'acknowledged' });
  await waitFor(() => expect(within(second).getByTestId('notification-open-source')).toBeEnabled());
});

it('opens an acknowledged alert source without marking it read again', async () => {
  const { api, stores } = renderNotifications([notification(1, 'acknowledged')]);
  fireEvent.click(await screen.findByTestId('notification-open-source'));
  await waitFor(() => expect(stores.alertCheckLink.store.get().pendingAlertCheckLink?.link).toEqual({
    alertId: 3, runId: 5, eventIds: [11],
  }));
  expect(api.markNotificationRead).not.toHaveBeenCalled();
});

function runNotification(deepLink: Record<string, unknown>): NotificationItem {
  return {
    ...notification(7, 'read'), sourceKind: 'run', sourceRef: {}, sourceEventIds: [],
    title: 'Run completed #12', deepLink,
  };
}

afterEach(() => window.history.replaceState(null, '', '/'));

it('opens a run notification through the run link of the open project', async () => {
  window.history.replaceState(null, '', '/p/notification-actions/s/4');
  const { api } = renderNotifications([
    runNotification({ kind: 'run', project_id: 'notification-actions', run_id: 12 }),
  ]);
  api.markNotificationRead.mockResolvedValue({ notificationId: 7, state: 'read' });
  const pushState = vi.spyOn(window.history, 'pushState');
  fireEvent.click(await screen.findByRole('button', { name: 'Open run' }));
  await waitFor(() => expect(pushState).toHaveBeenCalledWith(null, '', '/p/notification-actions/s/4?run=12'));
  expect(api.markNotificationRead).toHaveBeenCalledWith(7);
});

it('opens another project\'s run notification through that project\'s run link', async () => {
  window.history.replaceState(null, '', '/p/notification-actions');
  const { api } = renderNotifications([
    runNotification({ kind: 'run', project_id: 'other-project', run_id: 12 }),
  ]);
  api.markNotificationRead.mockResolvedValue({ notificationId: 7, state: 'read' });
  const pushState = vi.spyOn(window.history, 'pushState');
  fireEvent.click(await screen.findByRole('button', { name: 'Open run' }));
  await waitFor(() => expect(pushState).toHaveBeenCalledWith(null, '', '/p/other-project?run=12'));
});

it('ignores a run notification without a usable run id', async () => {
  const { api } = renderNotifications([runNotification({ kind: 'run', run_id: 'gone' })]);
  api.markNotificationRead.mockResolvedValue({ notificationId: 7, state: 'read' });
  const pushState = vi.spyOn(window.history, 'pushState');
  fireEvent.click(await screen.findByRole('button', { name: 'Open run' }));
  await waitFor(() => expect(api.markNotificationRead).toHaveBeenCalledWith(7));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Open run' })).toBeEnabled());
  expect(pushState).not.toHaveBeenCalled();
});

function importNotification(deepLink: Record<string, unknown>): NotificationItem {
  return {
    ...notification(8, 'read'), sourceKind: 'import',
    sourceRef: { project_id: 'notification-actions', import_ref: 'import-1', sheet_id: 5 },
    sourceEventIds: [], severity: 'warning', title: 'Import finished with 2 pages not read', deepLink,
  };
}

it('opens an import notification on its sheet in the open project', async () => {
  const { api, stores } = renderNotifications([
    importNotification({ kind: 'sheet', project_id: 'notification-actions', sheet_id: 5 }),
  ]);
  api.markNotificationRead.mockResolvedValue({ notificationId: 8, state: 'read' });
  expect(await screen.findByText('Import')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Open sheet' }));
  await waitFor(() => expect(stores.route.store.get().sheetId).toBe('5'));
  expect(api.markNotificationRead).toHaveBeenCalledWith(8);
});

it('opens an import notification for another project through that project route', async () => {
  const { api, stores } = renderNotifications([
    importNotification({ kind: 'sheet', project_id: 'other-project', sheet_id: 5 }),
  ]);
  api.markNotificationRead.mockResolvedValue({ notificationId: 8, state: 'read' });
  const pushState = vi.spyOn(window.history, 'pushState');
  fireEvent.click(await screen.findByRole('button', { name: 'Open sheet' }));
  await waitFor(() => expect(pushState).toHaveBeenCalledWith(null, '', '/p/other-project/s/5'));
  expect(stores.route.store.get().sheetId).not.toBe('5');
});
