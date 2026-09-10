// The createWorkspaceStores.ts wiring point for the row-cache registry. The
// actual pure (framework-free) implementation lives in grid/rowCacheStore.ts —
// it's grid-cache domain logic, not workspace UI state, and workspace/ already
// depends on grid/'s plain helpers (grid/typeRegistry's applyColumnTypeRegistry)
// the same direction. This file just re-exports it so
// createWorkspaceStores.ts's per-project factory can assemble one registry per
// project — never a module-level singleton (sheet ids repeat across projects;
// see SheetGrid.tsx's widthsKey comment).

export { createRowCacheStore, type RowCacheStoreHandle } from '../grid/rowCacheStore';
