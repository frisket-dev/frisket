// Typed domain handle for the watch-run-link store. Reads the watchRunLink
// member off the current project's WorkspaceStores instance — the handle
// reference is stable for the lifetime of that project's stores, so it never
// churns a dep array within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { WatchRunLinkStoreHandle } from '../state/watchRunLinkStore';

export function useWatchRunLinkHandle(): WatchRunLinkStoreHandle {
  return useWorkspaceStores().watchRunLink;
}
