// Typed domain handle for the chrome store. Reads the chrome member off the
// current project's WorkspaceStores instance — the handle reference is stable
// for the lifetime of that project's stores, so it never churns a dep array
// within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { ChromeStoreHandle } from '../state/chromeStore';

export function useChromeHandle(): ChromeStoreHandle {
  return useWorkspaceStores().chrome;
}
