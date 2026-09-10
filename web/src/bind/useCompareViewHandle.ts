// Typed domain handle for the compare-view store. Reads the compareView member
// off the current project's WorkspaceStores instance; the reference is stable
// for that project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { CompareViewStoreHandle } from '../state/compareViewStore';

export function useCompareViewHandle(): CompareViewStoreHandle {
  return useWorkspaceStores().compareView;
}
