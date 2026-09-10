// Mounts the RouteSyncController — the store→URL write path, THE single owner of
// history writes for the workspace route. One controller per workspace: the
// effect creates it on mount and JOINS the workspace substrate's activation
// lease (state/workspaceSessionLease.ts) instead of running its own independent
// dispose-on-unmount — the lease's deactivate() calls the controller's dispose
// in the same ordered teardown as jobStore's poll lanes (WEB-01: "The route
// controller participates in the same session lifecycle/disposer."). Both
// `route` (the project's stores handle) and `write` (the caller-stable
// commitRoute adapter) are stable references, so the effect mounts exactly once
// per project session and never re-subscribes.
//
// The effect returns the lease's own unregister() as its cleanup (Codex review
// correction): joinRouteController() is generation-scoped, so unregister()
// disposes the controller exactly once regardless of WHICH direction tears it
// down first — a real dependency rerun/unmount (this effect's cleanup runs) or
// deactivate() running first (unregister() then finds the registration already
// cleared and no-ops). Returning nothing here (the pre-correction shape) left
// a dependency rerun leaking the outgoing controller's subscription, since
// joinRouteController used to blindly overwrite its stored callback with no
// disposal of the one being replaced.
//
// Safe to mount only after every same-project command uses route.navigate or
// route.normalize. URL/popstate enters through route.projectExternal, which
// emits no history command, so projection echoes cannot double-write.

import { useEffect } from 'react';
import { createRouteSyncController } from '../core/route/RouteSyncController';
import type { RouteState } from '../core/route/RouteState';
import type { RouteStoreHandle } from '../state/routeStore';
import { useWorkspaceStores } from './useWorkspaceStores';

export function useRouteSyncController(
  route: RouteStoreHandle,
  write: (next: RouteState, mode: 'push' | 'replace') => void,
): void {
  // Raw substrate access stays inside bind/ (§14.1: "raw session access
  // limited to bind/composition") — this hook reaches the lease only to call
  // its narrow joinRouteController() capability, not to traverse the wider
  // command tree.
  const { lease } = useWorkspaceStores();
  useEffect(() => {
    const controller = createRouteSyncController({
      write,
      routeStore: route,
    });
    // Registers this generation's dispose callback with the lease and
    // returns a lease-owned unregister() as this effect's OWN cleanup — see
    // workspaceSessionLease.ts's joinRouteController doc for the full
    // generation-scoped contract (immediate-dispose if inactive, dispose-
    // previous-before-replace, idempotent unregister either direction).
    return lease.joinRouteController(() => controller.dispose());
  }, [lease, route, write]);
}
