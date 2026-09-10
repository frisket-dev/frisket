// Typed domain handle for the action-catalog resource. The handle is stable
// for the lifetime of the current project's WorkspaceStores instance.

import { useWorkspaceStores } from './useWorkspaceStores';
import type { ActionCatalogResource } from '../state/workspaceResources';

export function useActionCatalogHandle(): ActionCatalogResource {
  return useWorkspaceStores().actionCatalog;
}
