// Typed domain handle for the lens-view store. Reads the lensView member off
// the current project's WorkspaceStores instance — the handle reference is
// stable for the lifetime of that project's stores, so it never churns a dep
// array within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { LensViewStoreHandle } from '../state/lensViewStore';

export function useLensViewHandle(): LensViewStoreHandle {
  return useWorkspaceStores().lensView;
}
