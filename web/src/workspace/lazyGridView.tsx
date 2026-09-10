// LazySheetGrid: the code-split SheetGrid wrapper the workspace view-model hook
// mounts. Lives in its own module so useWorkspaceModel.tsx exports only the hook
// (react-refresh/only-export-components).
import { lazy } from 'react';

export const LazySheetGrid = lazy(() =>
  import('../grid/SheetGrid').then(({ SheetGrid }) => ({ default: SheetGrid })),
);
