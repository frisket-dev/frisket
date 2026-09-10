// Typed domain handle for the plugin-layout runtime-index resource. Reads the
// pluginLayout member off the current project's WorkspaceStores instance — the
// handle reference is stable for the lifetime of that project's stores, so it
// never churns a dep array within one project session. This accessor owns no
// effect: both workspace composition and the Errors region consume it, while
// the one resource instance owns request/timer/visibility lifecycle.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { PluginLayoutStoreHandle } from '../state/pluginLayoutStore';

export function usePluginLayoutHandle(): PluginLayoutStoreHandle {
  return useWorkspaceStores().pluginLayout;
}
