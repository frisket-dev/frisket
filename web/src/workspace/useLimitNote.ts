import { useEffect, useMemo, useRef, useState } from 'react';
import type { SheetMeta, ViewSelection } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { limitNote } from './rowFiltering';

/** The note under the last row while a limit hides matching rows. The count
 *  of matching rows comes from the server (the same selection without its
 *  limit), never from the rows the browser has loaded. */
export function useLimitNote(
  sheet: SheetMeta | undefined,
  selection: ViewSelection | null,
  dataVersion: number,
  onShowAll: () => void,
): { note: string; showAll: string; onShowAll(): void } | null {
  const { projectApi } = useWorkspaceStores();
  const limit = selection?.limit ?? null;
  const unlimited = useMemo(
    () => (selection && limit !== null ? { ...selection, limit: null } : null),
    [limit, selection],
  );
  const requestKey = unlimited && sheet ? `${sheet.id}:${dataVersion}:${JSON.stringify(unlimited)}` : '';
  const unlimitedRef = useRef(unlimited);
  useEffect(() => {
    unlimitedRef.current = unlimited;
  }, [unlimited]);
  const [matching, setMatching] = useState<{ key: string; total: number } | null>(null);

  useEffect(() => {
    const request = unlimitedRef.current;
    if (!requestKey || !sheet || !request) return undefined;
    let alive = true;
    void projectApi
      .getSheetData(sheet.id, 0, 1, {
        selection: request,
        valueMode: 'bounded',
        columnIds: sheet.columns.slice(0, 1).map((column) => column.id),
      })
      .then((page) => {
        if (alive) setMatching({ key: requestKey, total: page.total });
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [projectApi, requestKey, sheet]);

  const total = matching?.key === requestKey ? matching.total : null;
  return useMemo(() => {
    const note = limitNote(limit, total);
    return note ? { ...note, onShowAll } : null;
  }, [limit, onShowAll, total]);
}
