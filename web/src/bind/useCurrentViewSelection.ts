import { useMemo } from 'react';
import type { SheetMeta } from '../api/types';
import { selectionFromGrid } from '../workspace/viewDefinition';
import { useGridViewHandle } from './useGridViewHandle';
import { useDetailHandle } from './useDetailHandle';
import { useSelector } from './useSelector';

/** Every sheet presentation and action consumes the same transient definition. */
export function useCurrentViewSelection(sheet: SheetMeta | undefined | null) {
  const grid = useGridViewHandle();
  const detail = useDetailHandle();
  const applied = useSelector(grid.store, (state) => state.applied);
  const child = useSelector(detail.store, (state) => state.childFilter);
  return useMemo(() => {
    if (!sheet) return null;
    const selection = selectionFromGrid(sheet, applied.filter, applied.sort, applied.scopeRowIds ?? null, applied.selection, applied);
    if (child?.sheetId === sheet.id && selection.scope.kind === 'sheet') selection.scope.parent_row_id = Number(child.parentRowId);
    return selection;
  }, [sheet, applied, child]);
}
