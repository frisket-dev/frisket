import { useEffect, useMemo, useState } from 'react';
import type { SheetMeta } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { facetBehaviorFor } from '../grid/typeRegistry';
import { facetColumnsOf, isSearchOnly } from './rowFiltering';

interface FacetEligibility {
  /** Facetable by type but left to search by the server's counts. */
  searchOnly: ReadonlySet<string>;
  /** Not yet counted; no card is shown until the count arrives. */
  pending: ReadonlySet<string>;
}

/** Which of a sheet's facetable columns get a facet card. The decision uses
 *  each column's whole-column distinct count from the server, never the rows
 *  the browser has loaded. Range columns need no count. */
export function useFacetEligibility(sheet: SheetMeta | null | undefined, dataVersion: number): FacetEligibility {
  const { projectApi } = useWorkspaceStores();
  const candidates = useMemo(
    () => facetColumnsOf(sheet?.columns ?? []).filter((column) => facetBehaviorFor(column.type)?.kind !== 'range'),
    [sheet],
  );
  const sheetId = sheet?.id ?? null;
  const [decided, setDecided] = useState<{ key: string; searchOnly: Map<string, boolean> }>({
    key: '', searchOnly: new Map(),
  });
  const key = `${sheetId}:${dataVersion}:${candidates.map((column) => `${column.id}/${column.type}`).join(',')}`;

  useEffect(() => {
    if (sheetId === null) return undefined;
    let alive = true;
    for (const column of candidates) {
      void projectApi
        .columnValuesPreview({ sheetId, inputColumn: column.name, valueMode: 'bounded', limit: 1 })
        .then((preview) => {
          const collection = facetBehaviorFor(column.type)?.kind === 'collection';
          const distinct = collection ? preview.listFacet?.distinct ?? 0 : preview.distinct;
          return isSearchOnly(column, { distinct, omittedRows: preview.omittedRows });
        })
        .catch(() => false)
        .then((searchOnly) => {
          if (!alive) return;
          setDecided((current) => {
            const map = new Map(current.key === key ? current.searchOnly : []);
            map.set(column.name, searchOnly);
            return { key, searchOnly: map };
          });
        });
    }
    return () => {
      alive = false;
    };
  }, [candidates, key, projectApi, sheetId]);

  return useMemo(() => {
    const known = decided.key === key ? decided.searchOnly : new Map<string, boolean>();
    return {
      searchOnly: new Set([...known].filter(([, value]) => value).map(([name]) => name)),
      pending: new Set(candidates.filter((column) => !known.has(column.name)).map((column) => column.name)),
    };
  }, [candidates, decided, key]);
}
