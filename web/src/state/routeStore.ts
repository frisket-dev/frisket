// Canonical same-project route owner. URL/popstate projection and every command
// mutation enter through this owner, including one disposable Ask view.
// Raw Store.set remains closure-private.

import { createStore } from '../core/store/createStore';
import type { Store, Unsubscribe } from '../core/store/types';
import { routeStatesEqual, type RouteState } from '../core/route/RouteState';

export type RouteReadStore = Pick<Store<RouteState>, 'get' | 'subscribe'>;
export type RouteHistoryMode = 'push' | 'replace';

export type RouteHistoryCommand =
  | {
      kind: 'navigate';
      prev: RouteState;
      next: RouteState;
      historyMode?: RouteHistoryMode;
    }
  | {
      kind: 'normalize';
      prev: RouteState;
      next: RouteState;
    };

export interface RouteStoreDependencies {
  /** Fixed six-class sheet-change matrix. Called only when sheetId changes. */
  resetForSheetChange(nextSheetId: string): void;
}

export interface RouteStoreHandle {
  readonly store: RouteReadStore;
  /** One disposable workspace projection; these never add browser history. */
  beginTemporary(next: RouteState): void;
  finishTemporary(mode: 'restore' | 'commit'): void;
  isTemporary(): boolean;
  baseRoute(): RouteState;

  /** Same-project URL/popstate projection. Never requests a history write. */
  projectExternal(next: RouteState): void;

  /** Same-project command navigation. History mode defaults to pushVsReplace. */
  navigate(next: RouteState, historyMode?: RouteHistoryMode): void;

  /** Canonical correction. Always requests replace for a real route change. */
  normalize(next: RouteState): void;

  /** Controller-only history-command subscription; at most one live writer. */
  subscribeHistoryCommands(listener: (command: RouteHistoryCommand) => void): Unsubscribe;

  /** Installs the existing preview cancellation/generation invalidation step. */
  registerSheetChangeTeardown(teardown: () => void): Unsubscribe;
}

export const selectActiveSheetId = (route: RouteState): string | null => route.sheetId;

function assertSameProject(projectId: string, next: RouteState): void {
  if (next.projectId !== projectId) {
    throw new Error(
      `same-project route rejected projectId "${next.projectId}" for active project "${projectId}"`,
    );
  }
}

export function createRouteStore(
  projectId: string,
  dependencies: RouteStoreDependencies = { resetForSheetChange() {} },
): RouteStoreHandle {
  const mutableStore = createStore<RouteState>({
    projectId,
    sheetId: null,
    actionKind: null,
    review: false,
    panel: null,
  });
  const readStore: RouteReadStore = {
    get: mutableStore.get,
    subscribe: mutableStore.subscribe,
  };
  let temporaryBase: RouteState | null = null;
  let historyListener: ((command: RouteHistoryCommand) => void) | null = null;
  let sheetChangeTeardown: () => void = () => {};

  /**
   * The one mutation composition. Order is load-bearing:
   * invalidate/cancel old-sheet work, apply classes 1/2 (classes 4-6 survive
   * by omission), commit the route, then subscribers may hydrate class 3.
   */
  function applyRoute(prev: RouteState, next: RouteState): boolean {
    if (routeStatesEqual(prev, next)) return false;
    if (prev.sheetId !== next.sheetId) {
      sheetChangeTeardown();
      dependencies.resetForSheetChange(next.sheetId ?? '');
    }
    mutableStore.set(next);
    return true;
  }

  function checked(next: RouteState): RouteState {
    assertSameProject(projectId, next);
    return next;
  }

  return {
    store: readStore,
    baseRoute: () => temporaryBase ?? mutableStore.get(),
    isTemporary: () => temporaryBase !== null,
    beginTemporary(next) {
      checked(next);
      temporaryBase ??= mutableStore.get();
      applyRoute(mutableStore.get(), next);
    },
    finishTemporary(mode) {
      const base = temporaryBase;
      if (!base) return;
      temporaryBase = null;
      const current = mutableStore.get();
      if (mode === 'restore') applyRoute(current, base);
      else if (!routeStatesEqual(base, current)) historyListener?.({ kind: 'navigate', prev: base, next: current });
    },
    projectExternal(next) {
      if (temporaryBase && routeStatesEqual(temporaryBase, next)) return;
      temporaryBase = null;
      const prev = mutableStore.get();
      applyRoute(prev, checked(next));
    },
    navigate(next, historyMode) {
      const prev = mutableStore.get();
      const checkedNext = checked(next);
      if (!applyRoute(prev, checkedNext)) return;
      if (!temporaryBase) historyListener?.({ kind: 'navigate', prev, next: checkedNext, historyMode });
    },
    normalize(next) {
      const prev = mutableStore.get();
      const checkedNext = checked(next);
      if (!applyRoute(prev, checkedNext)) return;
      if (!temporaryBase) historyListener?.({ kind: 'normalize', prev, next: checkedNext });
    },
    subscribeHistoryCommands(listener) {
      if (historyListener !== null) {
        throw new Error('route history writer already attached');
      }
      historyListener = listener;
      return () => {
        if (historyListener === listener) historyListener = null;
      };
    },
    registerSheetChangeTeardown(teardown) {
      sheetChangeTeardown = teardown;
      return () => {
        if (sheetChangeTeardown === teardown) sheetChangeTeardown = () => {};
      };
    },
  };
}
