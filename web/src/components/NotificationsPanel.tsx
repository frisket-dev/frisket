import { useCallback, useEffect, useReducer } from 'react';
import { PanelEmpty, PanelError } from './PanelPrimitives';
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
  type NotificationSummary,
} from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useWorkspaceChromeCommands } from '../bind/useWorkspaceChromeState';
import { useWatchRunLinkHandle } from '../bind/useWatchRunLinkHandle';
import type { WatchRunLink } from '../state/watchRunLinkStore';

export const NOTIFICATIONS_CHANGED_EVENT = 'frisket:notifications-changed';

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

function dispatchNotificationsChanged() {
  window.dispatchEvent(new CustomEvent(NOTIFICATIONS_CHANGED_EVENT));
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
  summary: NotificationSummary | null;
  items: NotificationItem[] | null;
  busy: number | 'load' | null;
  error: string | null;
}

type NotificationsPanelPatch =
  | Partial<NotificationsPanelState>
  | ((state: NotificationsPanelState) => Partial<NotificationsPanelState>);

const initialNotificationsPanelState: NotificationsPanelState = {
  summary: null,
  items: null,
  busy: null,
  error: null,
};

function reduceNotificationsPanelState(
  state: NotificationsPanelState,
  patch: NotificationsPanelPatch,
): NotificationsPanelState {
  return { ...state, ...(typeof patch === 'function' ? patch(state) : patch) };
}

export function NotificationsPanel() {
  const { projectApi: api } = useWorkspaceStores();
  const [panelState, setPanelState] = useReducer(
    reduceNotificationsPanelState,
    initialNotificationsPanelState,
  );
  const { summary, items, busy, error } = panelState;
  const { openDiscover } = useWorkspaceChromeCommands();
  const watchRunLinkHandle = useWatchRunLinkHandle();

  const refreshSummary = useCallback(() => {
    void api
      .getNotificationsSummary()
      .then((nextSummary) => setPanelState({ summary: nextSummary }))
      .catch(() => undefined);
  }, []);

  const load = useCallback(() => {
    setPanelState((current) => ({ busy: current.busy ?? 'load', error: null }));
    void Promise.all([
      api.getNotificationsSummary(),
      api.listNotifications({ state: 'all', offset: 0, limit: 20 }),
    ])
      .then(([nextSummary, page]) => {
        setPanelState({ summary: nextSummary, items: page.notifications });
      })
      .catch((e: Error) => setPanelState({ error: e.message }))
      .finally(() => {
        setPanelState((current) => ({ busy: current.busy === 'load' ? null : current.busy }));
      });
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(load, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const onChanged = () => load();
    window.addEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged);
    return () => window.removeEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged);
  }, [load]);

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
    refreshSummary();
    dispatchNotificationsChanged();
  };

  const markSeen = (item: NotificationItem) => {
    setPanelState({ busy: item.id });
    void api
      .markNotificationsSeen({ notificationIds: [item.id] })
      .then(() => load())
      .then(dispatchNotificationsChanged)
      .catch((e: Error) => setPanelState({ error: e.message }))
      .finally(() => setPanelState({ busy: null }));
  };

  const markRead = (item: NotificationItem, openSource = false) => {
    setPanelState({ busy: item.id });
    const next = item.state === 'acknowledged'
      ? Promise.resolve<NotificationActorState | null>(null)
      : api.markNotificationRead(item.id);
    void next
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
      })
      .catch((e: Error) => setPanelState({ error: e.message }))
      .finally(() => setPanelState({ busy: null }));
  };

  const ack = (item: NotificationItem) => {
    setPanelState({ busy: item.id });
    void api
      .ackNotification(item.id)
      .then(applyState)
      .catch((e: Error) => setPanelState({ error: e.message }))
      .finally(() => setPanelState({ busy: null }));
  };

  const unack = (item: NotificationItem) => {
    setPanelState({ busy: item.id });
    void api
      .unackNotification(item.id)
      .then(applyState)
      .catch((e: Error) => setPanelState({ error: e.message }))
      .finally(() => setPanelState({ busy: null }));
  };

  const unseen = summary?.unseen ?? 0;

  return (
    <section className="sidebar-notifications" data-testid="notifications">
      {/* Notifications is its own Discover tab
          — the tab strip already carries the label/icon, so the panel itself
          only surfaces the unseen count, matching WatchesPanel's slim header. */}
      {unseen > 0 && (
        <div className="notifications-header-slim">
          <span className="notifications-count" data-testid="notifications-count">
            {unseen}
          </span>
        </div>
      )}

      <div className="notifications-body" data-testid="notifications-panel">
        {error && (
          <PanelError>{error}</PanelError>
        )}
        {items === null || busy === 'load' ? (
          <PanelEmpty>loading...</PanelEmpty>
          ) : items.length === 0 ? (
            <PanelEmpty>No notifications.</PanelEmpty>
          ) : (
            <ul className="notifications-list">
              {items.map((item) => (
                <li
                  className={`notification-item severity-${item.severity} state-${item.state}`}
                  data-testid="notification-item"
                  key={item.id}
                >
                  <div className="notification-head">
                    <span className="notification-source">{itemSourceLabel(item)}</span>
                    <span className="notification-state" data-testid="notification-state">
                      {item.state}
                    </span>
                  </div>
                  <div className="notification-title">{item.title}</div>
                  <div className="notification-summary">{item.summary}</div>
                  <div className="notification-ref" data-testid="notification-source-ref">
                    {sourceRefLabel(item.sourceRef)}
                  </div>
                  <div className="notification-actions">
                    <button
                      type="button"
                      className="icon-btn"
                      title="Open source"
                      aria-label="Open source"
                      data-testid="notification-open-source"
                      disabled={busy === item.id}
                      onClick={() => markRead(item, true)}
                    >
                      {busy === item.id ? <RefreshCw size={12} className="spin" /> : <ExternalLink size={12} />}
                    </button>
                    {item.state === 'unseen' && (
                      <button
                        type="button"
                        className="icon-btn"
                        title="Mark seen"
                        aria-label="Mark seen"
                        data-testid="notification-seen"
                        disabled={busy === item.id}
                        onClick={() => markSeen(item)}
                      >
                        <Eye size={12} />
                      </button>
                    )}
                    {item.state !== 'read' && item.state !== 'acknowledged' && (
                      <button
                        type="button"
                        className="icon-btn"
                        title="Mark read"
                        aria-label="Mark read"
                        data-testid="notification-read"
                        disabled={busy === item.id}
                        onClick={() => markRead(item)}
                      >
                        <Check size={12} />
                      </button>
                    )}
                    {item.state === 'acknowledged' ? (
                      <button
                        type="button"
                        className="icon-btn"
                        title="Unacknowledge"
                        aria-label="Unacknowledge"
                        data-testid="notification-unack"
                        disabled={busy === item.id}
                        onClick={() => unack(item)}
                      >
                        <RotateCcw size={12} />
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="icon-btn"
                        title="Acknowledge"
                        aria-label="Acknowledge"
                        data-testid="notification-ack"
                        disabled={busy === item.id}
                        onClick={() => ack(item)}
                      >
                        <Check size={12} />
                      </button>
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
