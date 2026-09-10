// Typed domain handle for the detail store. Reads the detail member off the
// current project's WorkspaceStores instance — the handle reference is stable
// for the lifetime of that project's stores, so it never churns a dep array
// within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { DetailStoreHandle } from '../state/detailStore';

export function useDetailHandle(): DetailStoreHandle {
  return useWorkspaceStores().detail;
}
