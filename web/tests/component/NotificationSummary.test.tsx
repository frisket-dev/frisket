// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { NotificationSummary } from '../../src/api/open';
import type { ProjectApiPort } from '../../src/api/ports';
import { NotificationsPanel } from '../../src/components/NotificationsPanel';
import { WatchesPanel } from '../../src/components/WatchesPanel';
import { dispatchNotificationsChanged, NOTIFICATIONS_CHANGED_EVENT, useNotificationSummary } from '../../src/components/notifications/useNotificationSummary';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

afterEach(cleanup);

function summary(unseen: number): NotificationSummary {
  return { schemaVersion: 'frisket.notifications_summary.v1', total: unseen, unseen,
    seen: 0, read: 0, acknowledged: 0, bySeverity: {}, bySourceKind: { watch: unseen },
    bySourceRef: [{ sourceKind: 'watch', sourceRef: { watch_id: 3 }, unseen }] };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((accept) => { resolve = accept; });
  return { promise, resolve };
}
function api() {
  return { qa: { citation: vi.fn(), report: vi.fn() },
    getNotificationsSummary: vi.fn(), listWatches: vi.fn().mockResolvedValue([]),
    listNotifications: vi.fn().mockResolvedValue({ notifications: [] }) };
}
function Readout({ name }: { name: string }) {
  const value = useNotificationSummary();
  return <output data-testid={name}>{value?.unseen ?? 0}</output>;
}

it('deduplicates co-mounted notification/watch reads and legacy event invalidation', async () => {
  const projectApi = api();
  projectApi.getNotificationsSummary.mockResolvedValue(summary(5));
  const harness = createWorkspaceTestHarness({ projectId: 'summary-shared',
    api: { projectApi: projectApi as unknown as ProjectApiPort } });
  harness.render(<><NotificationsPanel /><WatchesPanel canEdit={false} /></>);
  expect(await screen.findByTestId('notifications-count')).toHaveTextContent('5');
  expect(await screen.findByTestId('watches-notification-count')).toHaveTextContent('5');
  expect(projectApi.getNotificationsSummary).toHaveBeenCalledTimes(1);
  act(() => { window.dispatchEvent(new Event(NOTIFICATIONS_CHANGED_EVENT)); });
  await waitFor(() => expect(projectApi.getNotificationsSummary).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(projectApi.listNotifications).toHaveBeenCalledTimes(2));
});

it('rejects an older read after a newer invalidation completes', async () => {
  const projectApi = api();
  const first = deferred<NotificationSummary>();
  const next = deferred<NotificationSummary>();
  projectApi.getNotificationsSummary.mockReturnValueOnce(first.promise).mockReturnValueOnce(next.promise);
  const harness = createWorkspaceTestHarness({ projectId: 'summary-current',
    api: { projectApi: projectApi as unknown as ProjectApiPort } });
  harness.render(<><Readout name="one" /><Readout name="two" /></>);
  await waitFor(() => expect(projectApi.getNotificationsSummary).toHaveBeenCalledTimes(1));
  act(() => dispatchNotificationsChanged('summary-current'));
  await waitFor(() => expect(projectApi.getNotificationsSummary).toHaveBeenCalledTimes(2));
  await act(async () => { next.resolve(summary(8)); await next.promise; });
  expect(screen.getByTestId('one')).toHaveTextContent('8');
  await act(async () => { first.resolve(summary(1)); await first.promise; });
  expect(screen.getByTestId('one')).toHaveTextContent('8');
  expect(screen.getByTestId('two')).toHaveTextContent('8');
});

it('scopes identical API instances by project and retires reads on the final unmount', async () => {
  const projectApi = api();
  const oldA = deferred<NotificationSummary>();
  const oldB = deferred<NotificationSummary>();
  projectApi.getNotificationsSummary.mockReturnValueOnce(oldA.promise).mockReturnValueOnce(oldB.promise)
    .mockResolvedValue(summary(9));
  const a = createWorkspaceTestHarness({ projectId: 'summary-A', api: { projectApi: projectApi as unknown as ProjectApiPort } });
  const b = createWorkspaceTestHarness({ projectId: 'summary-B', api: { projectApi: projectApi as unknown as ProjectApiPort } });
  const mountedA = a.render(<Readout name="A" />);
  b.render(<Readout name="B" />);
  await waitFor(() => expect(projectApi.getNotificationsSummary).toHaveBeenCalledTimes(2));
  await act(async () => { oldB.resolve(summary(2)); await oldB.promise; });
  expect(screen.getByTestId('A')).toHaveTextContent('0');
  expect(screen.getByTestId('B')).toHaveTextContent('2');
  mountedA.unmount();
  a.render(<Readout name="A" />);
  await waitFor(() => expect(screen.getByTestId('A')).toHaveTextContent('9'));
  await act(async () => { oldA.resolve(summary(1)); await oldA.promise; });
  expect(screen.getByTestId('A')).toHaveTextContent('9');
  act(() => dispatchNotificationsChanged('summary-A'));
  await waitFor(() => expect(projectApi.getNotificationsSummary).toHaveBeenCalledTimes(4));
  expect(screen.getByTestId('B')).toHaveTextContent('2');
});
