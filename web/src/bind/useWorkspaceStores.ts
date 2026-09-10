// Reader hook for the per-project WorkspaceStores context. Split from
// WorkspaceStoresProvider.tsx so that file exports only a component
// (react-refresh/only-export-components) — this file exports only hooks.

import { useContext } from 'react';
import { WorkspaceStoresContext } from './workspaceStoresContext';
import type { WorkspaceStores } from '../state/createWorkspaceStores';

export function useWorkspaceStores(): WorkspaceStores {
  const stores = useContext(WorkspaceStoresContext);
  if (!stores) {
    throw new Error('useWorkspaceStores must be used within WorkspaceStoresProvider');
  }
  return stores;
}
