// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ProjectApiPort } from '../../src/api/ports';
import type { NotificationItem } from '../../src/api/types';
import { NOTIFICATIONS_CHANGED_EVENT, NotificationsPanel } from '../../src/components/NotificationsPanel';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

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
    qa: { citation: vi.fn(), report: vi.fn() },
    listNotifications: vi.fn().mockResolvedValue({ notifications: items }),
    getNotificationsSummary: vi.fn().mockResolvedValue({ unseen: 1 }),
    markNotificationsSeen: vi.fn().mockResolvedValue({ seenCount: 1 }),
    markNotificationRead: vi.fn(),
    ackNotification: vi.fn(),
    unackNotification: vi.fn(),
  };
  const harness = createWorkspaceTestHarness({
    projectId: 'notification-actions', api: { projectApi: projectApi as unknown as ProjectApiPort },
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

it('opens an acknowledged watch source without marking it read again', async () => {
  const { api, stores } = renderNotifications([notification(1, 'acknowledged')]);
  fireEvent.click(await screen.findByTestId('notification-open-source'));
  await waitFor(() => expect(stores.watchRunLink.store.get().pendingWatchRunLink?.link).toEqual({
    watchId: 3, runId: 5, eventIds: [11],
  }));
  expect(api.markNotificationRead).not.toHaveBeenCalled();
});
