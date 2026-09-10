// The per-project store boundary. Wraps <Workspace key={project.id} .../> in
// App.tsx: builds exactly ONE createWorkspaceStores(projectId) per project mount
// via useMemo keyed on projectId (never a bare module-level call), then
// activates/deactivates its lease on mount/unmount instead of constructing or
// disposing directly.
//
// StrictMode note: React's dev double-invoke of this useMemo/effect pair must
// leave exactly one live activation for a given projectId, with the route
// controller joined once —
// not twice, not zero times. createWorkspaceStores' own construction still has
// no side effects beyond local object creation (a discarded double-construction
// is harmless, same as before), but activate()/deactivate() now carry real ones,
// so the ordering contract is enforced by the lease itself
// (state/workspaceSessionLease.ts), not by this effect being "probably fine":
// the effect's cleanup calls deactivate() (invalidate-then-dispose) before the
// replay's second mount calls activate() again, so StrictMode's
// mount→cleanup→mount always nets to exactly one live generation.
//
// activate()/deactivate() run in a LAYOUT effect, not a passive one (Codex
// review correction): React fires effects children-first/parent-last for
// PASSIVE effects (useEffect), so a plain useEffect here would let
// bind/useRouteSyncController.ts's own useEffect (mounted deep inside
// `children`, e.g. via useWorkspaceModel) call lease.joinRouteController()
// BEFORE this provider's activate() ever ran — the lease would see an
// inactive join and immediately dispose the controller it was never meant to
// tear down. every layout effect completes before any passive effect begins (the cross-phase guarantee); both phases individually mount child-first
// in the tree (including a descendant's), so activate() here is guaranteed to
// run before the child's passive join — the lease's own generation-scoped
// joinRouteController() is a defense-in-depth backstop for a join that
// somehow still arrives while inactive, not the primary correctness
// mechanism.
//
// The context object lives in bind/workspaceStoresContext.ts and the reader
// hook in bind/useWorkspaceStores.ts — split out so this file exports only
// the component (react-refresh/only-export-components).

import { useLayoutEffect, useMemo, type ReactNode } from 'react';
import { createWorkspaceStores } from '../state/createWorkspaceStores';
import { WorkspaceStoresContext } from './workspaceStoresContext';

export function WorkspaceStoresProvider({
  projectId,
  children,
}: {
  projectId: string;
  children: ReactNode;
}) {
  const stores = useMemo(() => createWorkspaceStores(projectId), [projectId]);
  useLayoutEffect(() => {
    stores.lease.activate();
    return () => stores.lease.deactivate();
  }, [stores]);
  return (
    <WorkspaceStoresContext.Provider value={stores}>{children}</WorkspaceStoresContext.Provider>
  );
}
