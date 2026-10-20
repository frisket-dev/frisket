import { useCallback, useEffect, useReducer, useRef } from 'react';
import { PanelEmpty, PanelError, StatusChip } from './PanelPrimitives';
import { IconButton } from './IconButton';
import {
  Check,
  ExternalLink,
  Eye,
  RefreshCw,
  RotateCcw,
} from 'lucide-react';
import {
  type NotificationActorState,
  type NotificationItem,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useWorkspaceChromeCommands } from '../bind/useWorkspaceChromeState';
import { useWatchRunLinkHandle } from '../bind/useWatchRunLinkHandle';
import type { WatchRunLink } from '../state/watchRunLinkStore';
import './NotificationsPanel.css';

import { dispatchNotificationsChanged, NOTIFICATIONS_CHANGED_EVENT, useNotificationSummary } from './notifications/useNotificationSummary';

function sourceRefLabel(ref: Record<string, unknown>): string {
  const entries = Object.entries(ref);
  if (entries.length === 0) return 'source: {}';
  return entries
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(' · ');
}

function itemSourceLabel(item: NotificationItem): string {
  if (item.sourceKind === 'watch') return 'Watch';
  if (item.sourceKind === 'run') return 'Run';
  if (item.sourceKind === 'job') return 'Job';
  return item.sourceKind || 'Source';
}

function watchRunLink(item: NotificationItem): WatchRunLink | null {
  if (item.deepLink.kind !== 'watch_run') return null;
  const watchId = Number(item.deepLink.watch_id ?? item.sourceRef.watch_id);
  const runId = Number(item.deepLink.run_id ?? item.sourceRef.run_id);
  if (!Number.isInteger(watchId) || !Number.isInteger(runId)) return null;
  const rawEventIds = Array.isArray(item.deepLink.event_ids)
    ? item.deepLink.event_ids
    : item.sourceEventIds;
  const eventIds: number[] = [];
  for (const value of rawEventIds) {
    const eventId = Number(value);
    if (Number.isInteger(eventId) && eventId > 0) eventIds.push(eventId);
  }
  return { watchId, runId, eventIds };
}

interface NotificationsPanelState {
  items: NotificationItem[] | null;
  pending: ReadonlySet<number>;
  error: string | null;
}

type NotificationsPanelPatch =
  | Partial<NotificationsPanelState>
  | ((state: NotificationsPanelState) => Partial<NotificationsPanelState>);

const initialNotificationsPanelState: NotificationsPanelState = {
  items: null,
  pending: new Set(),
  error: null,
};

function reduceNotificationsPanelState(
  state: NotificationsPanelState,
  patch: NotificationsPanelPatch,
): NotificationsPanelState {
  return { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
}

export function NotificationsPanel() {
  const { projectApi: api, chromePreferences: { projectId } } = useWorkspaceStores();
  const summary = useNotificationSummary();
  const pendingItems = useRef(new Set<number>());
  const listGeneration = useRef(0);
  const [panelState, setPanelState] = useReducer(
    reduceNotificationsPanelState,
    initialNotificationsPanelState,
  );
  const { items, pending, error } = panelState;
  const { openDiscover } = useWorkspaceChromeCommands();
  const watchRunLinkHandle = useWatchRunLinkHandle();

  const load = useCallback(() => {
    const generation = ++listGeneration.current;
    setPanelState({ error: null });
    return api.listNotifications({ state: 'all', offset: 0, limit: 20 })
      .then((page) => {
        if (generation === listGeneration.current) setPanelState({ items: page.notifications });
      })
      .catch((e: Error) => {
        if (generation === listGeneration.current) setPanelState({ error: e.message });
      });
  }, [api]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => { window.clearTimeout(timer); listGeneration.current += 1; };
  }, [load]);

  useEffect(() => {
    const onChanged = (event: Event) => {
      const scope = (event as CustomEvent<{ projectId?: string }>).detail?.projectId;
      if (scope === undefined || scope === projectId) void load();
    };
    window.addEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged);
    return () => window.removeEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged);
  }, [load, projectId]);

  const applyState = (state: NotificationActorState) => {
    setPanelState((current) => ({
      items: (current.items ?? []).map((item) =>
        item.id === state.notificationId
          ? {
              ...item,
              state: state.state,
              seenAt: state.seenAt,
              readAt: state.readAt,
              acknowledgedAt: state.acknowledgedAt,
              acknowledgedBy: state.acknowledgedBy,
              updatedAt: state.updatedAt,
            }
          : item,
      ),
    }));
    dispatchNotificationsChanged(projectId);
  };

  const mutateItem = (item: NotificationItem, operation: () => Promise<unknown>) => {
    if (pendingItems.current.has(item.id)) return;
    pendingItems.current.add(item.id);
    setPanelState({ pending: new Set(pendingItems.current) });
    void operation()
      .catch((e: Error) => setPanelState({ error: e.message }))
      .finally(() => {
        pendingItems.current.delete(item.id);
        setPanelState({ pending: new Set(pendingItems.current) });
      });
  };

  const markSeen = (item: NotificationItem) => mutateItem(item, () => api
    .markNotificationsSeen({ notificationIds: [item.id] })
    .then(() => dispatchNotificationsChanged(projectId)));

  const markRead = (item: NotificationItem, openSource = false) => {
    mutateItem(item, () => (item.state === 'acknowledged'
      ? Promise.resolve<NotificationActorState | null>(null)
      : api.markNotificationRead(item.id))
      .then((state) => {
        if (state) applyState(state);
        if (!openSource) return;
        const link = watchRunLink(item);
        if (link) {
          // Store-intent handoff: write the slot
          // THEN switch tabs via the existing chrome action directly — no
          // event. WatchesPanel (Discover tab OR the bottom-dock co-mount)
          // picks the link up by subscription, applies it, and acks.
          watchRunLinkHandle.setPendingWatchRunLink(link, 'watches');
          openDiscover('Watches');
        }
      }));
  };

  const ack = (item: NotificationItem) => mutateItem(item, () => api.ackNotification(item.id).then(applyState));
  const unack = (item: NotificationItem) => mutateItem(item, () => api.unackNotification(item.id).then(applyState));

  const unseen = summary?.unseen ?? 0;

  return (
    <section className="content-stack" data-testid="notifications">
      {/* Notifications is its own Discover tab
          — the tab strip already carries the label/icon, so the panel itself
          only surfaces the unseen count, matching WatchesPanel's slim header. */}
      {unseen > 0 && (
        <div className="control-row" data-align="end">
          <StatusChip tone="info" size="sm" testId="notifications-count">
            {unseen}
          </StatusChip>
        </div>
      )}

      <div className="content-stack" data-testid="notifications-panel">
        {error && (
          <PanelError>{error}</PanelError>
        )}
        {items === null ? (
          <PanelEmpty>loading...</PanelEmpty>
          ) : items.length === 0 ? (
            <PanelEmpty>No notifications.</PanelEmpty>
          ) : (
            <ul className="panel-list">
              {items.map((item) => (
                <li
                  className={`panel-card notification-item severity-${item.severity} state-${item.state}`}
                  data-testid="notification-item"
                  key={item.id}
                >
                  <div className="control-row">
                    <span className="panel-item-meta panel-item-main">{itemSourceLabel(item)}</span>
                    <StatusChip tone="neutral" size="sm" uppercase testId="notification-state">
                      {item.state}
                    </StatusChip>
                  </div>
                  <div className="panel-item-title">{item.title}</div>
                  <div className="panel-item-description">{item.summary}</div>
                  <div className="panel-item-meta" data-testid="notification-source-ref">
                    {sourceRefLabel(item.sourceRef)}
                  </div>
                  <div className="control-row">
                    <IconButton icon={pending.has(item.id) ? RefreshCw : ExternalLink} size="inline" label="Open source"
                      title="Open source" data-testid="notification-open-source" aria-busy={pending.has(item.id)}
                      disabled={pending.has(item.id)} onClick={() => markRead(item, true)} />
                    {item.state === 'unseen' && (
                      <IconButton icon={Eye} size="inline" label="Mark seen" title="Mark seen"
                        data-testid="notification-seen" disabled={pending.has(item.id)} onClick={() => markSeen(item)} />
                    )}
                    {item.state !== 'read' && item.state !== 'acknowledged' && (
                      <IconButton icon={Check} size="inline" label="Mark read" title="Mark read"
                        data-testid="notification-read" disabled={pending.has(item.id)} onClick={() => markRead(item)} />
                    )}
                    {item.state === 'acknowledged' ? (
                      <IconButton icon={RotateCcw} size="inline" label="Unacknowledge" title="Unacknowledge"
                        data-testid="notification-unack" disabled={pending.has(item.id)} onClick={() => unack(item)} />
                    ) : (
                      <IconButton icon={Check} size="inline" label="Acknowledge" title="Acknowledge"
                        data-testid="notification-ack" disabled={pending.has(item.id)} onClick={() => ack(item)} />
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
      </div>
    </section>
  );
}
