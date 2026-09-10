// The React context object for the per-project WorkspaceStores, split into its
// own file so WorkspaceStoresProvider.tsx exports only a component and
// useWorkspaceStores.ts exports only a hook (react-refresh/only-export-components).

import { createContext } from 'react';
import type { WorkspaceStores } from '../state/createWorkspaceStores';

export const WorkspaceStoresContext = createContext<WorkspaceStores | null>(null);
