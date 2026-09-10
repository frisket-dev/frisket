// SEARCH section state for the command palette — project-wide FTS row search
// plus the watch-this-search action.
import { useEffect, useMemo, useRef, useState } from 'react';
import { searchProject, type SearchHit, type SheetMeta } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';

export interface PaletteSearch {
  mode: 'keyword' | 'semantic';
  setMode(mode: 'keyword' | 'semantic'): void;
  rerank: boolean;
  setRerank(rerank: boolean): void;
  /** Results for the CURRENT query only (null while empty/stale). */
  hits: SearchHit[] | null;
  searchError: string | null;
  canWatch: boolean;
  watchBusy: boolean;
  watchStatus: string | null;
  createSearchWatch(): void;
  searchGroups: { sheetId: string; hits: SearchHit[] }[];
  sheetName(id: string | number): string;
}

export function usePaletteSearch(
  query: string,
  searchSheets: SheetMeta[],
  onClose: () => void,
): PaletteSearch {
  const { projectApi, chromePreferences: { projectId } } = useWorkspaceStores();
  // The result is tagged with the query it belongs to so display derives from
  // the current query without a synchronous setState-in-effect.
  const [mode, setMode] = useState<'keyword' | 'semantic'>('keyword');
  const [rerank, setRerank] = useState(false);
  const [searchResult, setSearchResult] = useState<{ q: string; hits: SearchHit[] } | null>(null);
  const [searchFailure, setSearchFailure] = useState<{ q: string; message: string } | null>(null);
  const seq = useRef(0);
  const trimmedQuery = query.trim();
  useEffect(() => {
    const q = query.trim();
    if (!q) return;
    const mine = ++seq.current;
    const t = setTimeout(() => {
      searchProject(projectId, q, { mode, rerank })
        .then((h) => {
          if (seq.current === mine) setSearchResult({ q, hits: h });
        })
        .catch((e: Error) => {
          if (seq.current === mine) setSearchFailure({ q, message: e.message });
        });
    }, 180);
    return () => clearTimeout(t);
  }, [query, mode, rerank, projectId]);
  const hits = trimmedQuery && searchResult?.q === trimmedQuery ? searchResult.hits : null;
  const searchError =
    trimmedQuery && searchFailure?.q === trimmedQuery ? searchFailure.message : null;

  // Watch-this-search: create a keyword FTS watch from the current query.
  const [watchBusy, setWatchBusy] = useState(false);
  const [watchStatus, setWatchStatus] = useState<string | null>(null);
  const canWatch = Boolean(trimmedQuery) && mode === 'keyword' && !watchBusy;
  const createSearchWatch = () => {
    if (!canWatch) return;
    setWatchBusy(true);
    setWatchStatus(null);
    void projectApi
      .createWatch({
        name: `Search: ${trimmedQuery}`,
        scope: { kind: 'project' },
        query: { kind: 'fts', q: trimmedQuery, mode: 'keyword', rerank: rerank ? 'auto' : 'off' },
      })
      .then(() => {
        window.dispatchEvent(new Event('frisket:watches-changed'));
        onClose();
      })
      .catch((e: Error) => setWatchStatus(e.message))
      .finally(() => setWatchBusy(false));
  };

  const sheetsById = useMemo(
    () => new Map(searchSheets.map((sheet) => [String(sheet.id), sheet])),
    [searchSheets],
  );
  const sheetName = (id: string | number) => sheetsById.get(String(id))?.name ?? `sheet ${id}`;

  const searchGroups = useMemo(() => {
    const groups: { sheetId: string; hits: SearchHit[] }[] = [];
    const byId = new Map<string, { sheetId: string; hits: SearchHit[] }>();
    for (const h of hits ?? []) {
      const sid = String(h.sheet_id);
      const group = byId.get(sid);
      if (group) group.hits.push(h);
      else {
        const next = { sheetId: sid, hits: [h] };
        groups.push(next);
        byId.set(sid, next);
      }
    }
    return groups;
  }, [hits]);

  return {
    mode,
    setMode,
    rerank,
    setRerank,
    hits,
    searchError,
    canWatch,
    watchBusy,
    watchStatus,
    createSearchWatch,
    searchGroups,
    sheetName,
  };
}
