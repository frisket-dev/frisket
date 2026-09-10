// Typed domain handle for the preview-view store. Reads the previewView member
// off the current project's WorkspaceStores instance — the handle reference is
// stable for the lifetime of that project's stores, so it never churns a dep
// array within one project session.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { PreviewViewStoreHandle } from '../state/previewViewStore';

export function usePreviewViewHandle(): PreviewViewStoreHandle {
  return useWorkspaceStores().previewView;
}
