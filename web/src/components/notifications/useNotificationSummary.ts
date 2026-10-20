import { useSyncExternalStore } from 'react';
import type { NotificationSummary } from '../../api/open';
import type { ProjectApiPort } from '../../api/ports';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { createStore } from '../../core/store/createStore';

export const NOTIFICATIONS_CHANGED_EVENT = 'frisket:notifications-changed';

export function dispatchNotificationsChanged(projectId: string): void {
  window.dispatchEvent(new CustomEvent(NOTIFICATIONS_CHANGED_EVENT, { detail: { projectId } }));
}

/** One summary reader per project/API session. The event remains a legacy
 * invalidation adapter; panels own their separate lists and mutations. */
function createNotificationSummary(api: ProjectApiPort, projectId: string) {
  const store = createStore<NotificationSummary | null>(null);
  let subscribers = 0;
  let generation = 0;
  let scheduled = false;
  const refresh = () => {
    generation += 1; // Retire old reads immediately, before the queued request.
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      if (!subscribers) return;
      const request = ++generation;
      void api.getNotificationsSummary().then((summary) => {
        if (subscribers && request === generation) store.set(summary);
      }).catch(() => undefined);
    });
  };
  const onChanged = (event: Event) => {
    const scope = (event as CustomEvent<{ projectId?: string }>).detail?.projectId;
    if (scope === undefined || scope === projectId) refresh();
  };
  return {
    get: store.get,
    subscribe(listener: () => void) {
      const unsubscribe = store.subscribe(listener);
      if (++subscribers === 1) {
        window.addEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged);
        refresh();
      }
      return () => {
        unsubscribe();
        if (--subscribers === 0) {
          generation += 1;
          window.removeEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged);
        }
      };
    },
  };
}

const summaries = new WeakMap<ProjectApiPort, Map<string, ReturnType<typeof createNotificationSummary>>>();

export function useNotificationSummary(): NotificationSummary | null {
  const { projectApi: api, chromePreferences: { projectId } } = useWorkspaceStores();
  let projects = summaries.get(api);
  if (!projects) { projects = new Map(); summaries.set(api, projects); }
  let resource = projects.get(projectId);
  if (!resource) { resource = createNotificationSummary(api, projectId); projects.set(projectId, resource); }
  return useSyncExternalStore(resource.subscribe, resource.get, resource.get);
}
