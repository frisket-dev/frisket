// The store→URL write path ONLY. This controller owns the direction from
// routeStore writes (caused by internal commands, e.g. a future
// ctx.route.navigate) to history, via an injected `write` that calls routes.ts's
// navigate/replaceRoute. It does NOT read window.location and does NOT listen to
// popstate — both stay with useRoute() (routes.ts), the one owner of
// URL→React-state.
//
// Re-entry guard: a naive design used an "applying epoch" flag because one
// object owned BOTH directions and could self-echo. This design splits the two
// directions across two mechanisms (this controller writes; useRoute reads), so
// the only echo to guard is a write-induced synthetic popstate that round-trips
// back to a structurally-EQUAL RouteState. The structural-equality bail below
// (belt) plus the projection's own set-bail (suspenders) is the whole guard — no
// epoch flag.

import type { RouteStoreHandle } from '../../state/routeStore';
import { panelsEqual, type RouteState } from './RouteState';

/**
 * Encodes the expanded push/replace matrix. Lifted from current call-site
 * intent:
 *  - push (a back-stack entry): sheet select; open row/column/sourceHealth
 *    panel; open review; open/change actionKind; row→row identity change.
 *  - replace (a back entry would be noise): close panel; clear actionKind;
 *    close review; and the fallback (normalization writes).
 * Note: map/graph entry normalization always passes mode='replace' explicitly
 * (projectRouteState) and never reaches this function; review close via
 * history.back() (useWorkspaceModel) bypasses the write path entirely and is
 * exercised through useRoute()'s popstate listener.
 */
export function pushVsReplace(prev: RouteState, next: RouteState): 'push' | 'replace' {
  if (prev.sheetId !== next.sheetId) return 'push';
  if (next.review !== prev.review) return next.review ? 'push' : 'replace';
  if (next.actionKind !== prev.actionKind) return next.actionKind !== null ? 'push' : 'replace';

  const prevHasPanel = prev.panel !== null;
  const nextHasPanel = next.panel !== null;
  if (!prevHasPanel && nextHasPanel) return 'push'; // open panel
  if (prevHasPanel && !nextHasPanel) return 'replace'; // close panel
  if (prevHasPanel && nextHasPanel && !panelsEqual(prev.panel, next.panel)) return 'push'; // A→B

  return 'replace'; // no back-stack-worthy change → normalization
}

export interface RouteSyncDeps {
  /** Commits a RouteState to history via routes.ts navigate/replaceRoute. */
  write(next: RouteState, mode: 'push' | 'replace'): void;
  routeStore: Pick<RouteStoreHandle, 'subscribeHistoryCommands'>;
}

export interface RouteSyncController {
  dispose(): void;
}

export function createRouteSyncController(deps: RouteSyncDeps): RouteSyncController {
  const unsubscribe = deps.routeStore.subscribeHistoryCommands((command) => {
    const mode =
      command.kind === 'normalize'
        ? 'replace'
        : command.historyMode ?? pushVsReplace(command.prev, command.next);
    deps.write(command.next, mode);
  });
  return {
    dispose() {
      unsubscribe();
    },
  };
}
