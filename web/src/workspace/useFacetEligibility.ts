import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { SheetMeta } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { facetBehaviorFor } from '../grid/typeRegistry';
import { facetColumnsOf, isSearchOnly } from './rowFiltering';

interface FacetEligibility {
  /** Facetable by type but left to search by the server's counts. */
  searchOnly: ReadonlySet<string>;
  /** Not yet counted; no card is shown until the count arrives. */
  pending: ReadonlySet<string>;
  /** Counts the sheet's candidate columns, once per sheet and data version.
   *  Nothing is requested until a caller (the Filter sidebar, a column menu)
   *  asks. */
  ensure(): void;
}

/** Which of a sheet's facetable columns get a facet card. The decision uses
 *  each column's whole-column distinct count from the server, never the rows
 *  the browser has loaded, fetched for every column in one request. Range
 *  columns need no count. */
export function useFacetEligibility(sheet: SheetMeta | null | undefined, dataVersion: number): FacetEligibility {
  const { projectApi } = useWorkspaceStores();
  const candidates = useMemo(
    () => facetColumnsOf(sheet?.columns ?? []).filter((column) => facetBehaviorFor(column.type)?.kind !== 'range'),
    [sheet],
  );
  const sheetId = sheet?.id ?? null;
  const key = `${sheetId}:${dataVersion}:${candidates.map((column) => `${column.id}/${column.type}`).join(',')}`;
  const [decided, setDecided] = useState<{ key: string; searchOnly: Map<string, boolean> }>({
    key: '', searchOnly: new Map(),
  });
  const requested = useRef('');
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const ensure = useCallback(() => {
    if (sheetId === null || candidates.length === 0 || requested.current === key) return;
    requested.current = key;
    void projectApi
      .columnValueCounts({ sheetId, inputColumns: candidates.map((column) => column.name) })
      .then(({ columns }) => {
        const counts = new Map(columns.map((count) => [count.inputColumn, count]));
        return new Map(candidates.map((column) => {
          const count = counts.get(column.name);
          if (!count) return [column.name, false];
          const collection = facetBehaviorFor(column.type)?.kind === 'collection';
          return [column.name, isSearchOnly(column, {
            distinct: collection ? count.listDistinct ?? 0 : count.distinct,
            omittedRows: count.omittedRows,
          })];
        }));
      })
      .catch(() => new Map(candidates.map((column) => [column.name, false])))
      .then((searchOnly) => {
        if (alive.current) setDecided({ key, searchOnly });
      });
  }, [candidates, key, projectApi, sheetId]);

  return useMemo(() => {
    const known = decided.key === key ? decided.searchOnly : new Map<string, boolean>();
    return {
      searchOnly: new Set([...known].filter(([, value]) => value).map(([name]) => name)),
      pending: new Set(candidates.filter((column) => !known.has(column.name)).map((column) => column.name)),
      ensure,
    };
  }, [candidates, decided, ensure, key]);
}
