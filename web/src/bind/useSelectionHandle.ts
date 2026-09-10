// Typed domain handle for the selection store. Reads the selection member off
// the current project's WorkspaceStores instance — the handle reference is
// stable for the lifetime of that project's stores, so it never churns a dep
// array within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { SelectionStoreHandle } from '../state/selectionStore';

export function useSelectionHandle(): SelectionStoreHandle {
  return useWorkspaceStores().selection;
}
