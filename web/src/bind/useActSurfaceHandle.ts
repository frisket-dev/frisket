// Typed domain handle for the act-surface store. Reads the actSurface member off
// the current project's WorkspaceStores instance — the handle reference is
// stable for the lifetime of that project's stores, so it never churns a dep
// array within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { ActSurfaceStoreHandle } from '../state/actSurfaceStore';

export function useActSurfaceHandle(): ActSurfaceStoreHandle {
  return useWorkspaceStores().actSurface;
}
