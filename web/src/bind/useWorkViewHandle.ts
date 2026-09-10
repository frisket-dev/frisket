// Typed domain handle for the work-view store. Reads the workView member off the
// current project's WorkspaceStores instance — the handle reference is stable
// for the lifetime of that project's stores, so it never churns a dep array
// within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { WorkViewStoreHandle } from '../state/workViewStore';

export function useWorkViewHandle(): WorkViewStoreHandle {
  return useWorkspaceStores().workView;
}
