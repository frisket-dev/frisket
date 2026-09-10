// Typed domain handle for the route-projection store. Reads the route member off
// the current project's WorkspaceStores instance — the handle reference is
// stable for the lifetime of that project's stores, so it never churns a dep
// array within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { RouteStoreHandle } from '../state/routeStore';

export function useRouteHandle(): RouteStoreHandle {
  return useWorkspaceStores().route;
}

export function useProjectDataResource() {
  return useWorkspaceStores().projectData;
}
