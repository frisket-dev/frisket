// Typed domain handle for the alert-check-link store. Reads the alertCheckLink
// member off the current project's WorkspaceStores instance — the handle
// reference is stable for the lifetime of that project's stores, so it never
// churns a dep array within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { AlertCheckLinkStoreHandle } from '../state/alertCheckLinkStore';

export function useAlertCheckLinkHandle(): AlertCheckLinkStoreHandle {
  return useWorkspaceStores().alertCheckLink;
}
