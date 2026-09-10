// Owns exactly ONE published field: the last-fetched plugin runtime index,
// plus that field's request/poll/visibility lifecycle. Everything
// downstream (pluginPanelDescriptors, per-host filtered descriptor lists,
// mapViewDescriptor, activeMainViewPluginViewDescriptor, resolvedWorkbenchLayout/
// resolvedRegion, workbenchCommandEntries, …) is a PURE memoized derivation of
// this one field, computed at the bind layer via resolveWorkbenchLayout
// (workbench/layout.ts).
//
// Explicitly does NOT own:
//   - hiddenContributionIds / isContributionHidden / revealContribution /
//     hideContribution — owned by useWorkbenchContributionVisibility.ts. This
//     store only holds the READ-ONLY inputs resolveWorkbenchLayout consumes; it
//     does not write them.
//   - pluginPeekState / openPluginPeekDescriptor / dismissPluginPeek — owned by
//     usePluginPeekController.ts, a separate hook.
//   - pluginManagerOperations — a memoized bundle of bound `api.*` methods
//     (installLocalWorkbenchPlugin, activateWorkbenchPlugin, …) plus this
//     resource's invalidate command; not state, so it stays a bind-layer value.
//
// createPluginLayoutStore() is per-project, assembled by
// state/createWorkspaceStores.ts — never a module-level singleton.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import { getRuntimeConfig, type WorkbenchPluginRuntimeIndex } from '../api/open';
import type { WorkbenchApiPort } from '../api/ports';

const RUNTIME_INDEX_POLL_MS = 30_000;
const TRANSIENT_FAILURE_MESSAGE =
  'refreshWorkbenchPluginRuntimeIndex: keeping the last-known-good runtime index after a transient fetch failure';

type RuntimeIndexApi = Pick<WorkbenchApiPort, 'getWorkbenchPluginRuntimeIndex'>;
export type PluginAvailability = () => Promise<boolean>;

const runtimePluginsAvailable: PluginAvailability = () =>
  getRuntimeConfig().then(
    (config) => config.plugins_available === true,
    () => false,
  );

export interface PluginLayoutState {
  workbenchPluginRuntimeIndex: WorkbenchPluginRuntimeIndex | null;
}

export function createPluginLayoutState(): PluginLayoutState {
  return { workbenchPluginRuntimeIndex: null };
}

export interface PluginLayoutStoreHandle {
  store: Store<PluginLayoutState>;
  start(): Promise<void>;
  refresh(): Promise<void>;
  invalidate(): Promise<void>;
  dispose(): void;
}

export function createPluginLayoutStore(
  runtimeApi: RuntimeIndexApi,
  pluginsAvailable: PluginAvailability = runtimePluginsAvailable,
): PluginLayoutStoreHandle {
  const store = createStore<PluginLayoutState>(createPluginLayoutState());
  let generation = 0;
  let active = false;
  let timer: ReturnType<typeof setInterval> | null = null;
  let onVisibility: (() => void) | null = null;
  let inFlight: Promise<void> | null = null;
  let startFlight: Promise<void> | null = null;
  let available = false;

  const hidden = (): boolean =>
    typeof document !== 'undefined' && document.hidden === true;

  function isCurrent(requestGeneration: number): boolean {
    return active && generation === requestGeneration;
  }

  function accept(index: WorkbenchPluginRuntimeIndex): void {
    store.set((state) =>
      state.workbenchPluginRuntimeIndex === index
        ? state
        : { ...state, workbenchPluginRuntimeIndex: index },
    );
  }

  function refresh(): Promise<void> {
    if (!active || !available) {
      return startFlight ?? Promise.resolve();
    }
    if (inFlight) return inFlight;

    const requestGeneration = generation;
    const request: Promise<void> = runtimeApi
      .getWorkbenchPluginRuntimeIndex()
      .then((index) => {
        if (isCurrent(requestGeneration)) accept(index);
      })
      .catch((error: unknown) => {
        if (!isCurrent(requestGeneration)) return;
        if (store.get().workbenchPluginRuntimeIndex === null) return;
        console.error(TRANSIENT_FAILURE_MESSAGE, error);
      })
      .finally(() => {
        if (inFlight === request) inFlight = null;
      });
    inFlight = request;
    return request;
  }

  function start(): Promise<void> {
    if (active) return startFlight ?? inFlight ?? Promise.resolve();

    generation += 1;
    active = true;
    const requestGeneration = generation;

    const beginPolling = (): Promise<void> => {
      if (!isCurrent(requestGeneration)) return Promise.resolve();
      available = true;
      onVisibility = () => {
        if (!active || hidden()) return;
        void refresh();
      };
      if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', onVisibility);
      }
      timer = setInterval(() => {
        if (!hidden()) void refresh();
      }, RUNTIME_INDEX_POLL_MS);
      return refresh();
    };

    const started = pluginsAvailable()
      .then((allowed) => (allowed ? beginPolling() : undefined), () => undefined)
      .finally(() => {
        if (startFlight === started) startFlight = null;
      });
    startFlight = started;
    return started;
  }

  function dispose(): void {
    active = false;
    generation += 1;
    inFlight = null;
    startFlight = null;
    available = false;
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
    if (onVisibility !== null) {
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
      onVisibility = null;
    }
  }

  return {
    store,
    start,
    refresh,
    invalidate: refresh,
    dispose,
  };
}
