// Typed domain handle for the saved-views store. Reads the savedViews member off
// the current project's WorkspaceStores instance — the handle reference is
// stable for the lifetime of that project's stores, so it never churns a dep
// array within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { SavedViewsStoreHandle } from '../state/savedViewsStore';

export function useSavedViewsHandle(): SavedViewsStoreHandle {
  return useWorkspaceStores().savedViews;
}
