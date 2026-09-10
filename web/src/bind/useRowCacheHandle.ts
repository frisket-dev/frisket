// Typed domain handle for the row-cache registry. Reads the rowCache member off
// the current project's WorkspaceStores instance — the handle reference is
// stable for the lifetime of that project's stores, so it never churns a dep
// array within one project session. SheetGrid.tsx (grid/) does NOT import this
// directly — grid/ stays layering-pure with no bind/ import — the workspace
// layer (useWorkspaceModel.tsx) calls this and threads the handle down as a prop.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { RowCacheStoreHandle } from '../state/rowCacheStore';

export function useRowCacheHandle(): RowCacheStoreHandle {
  return useWorkspaceStores().rowCache;
}
