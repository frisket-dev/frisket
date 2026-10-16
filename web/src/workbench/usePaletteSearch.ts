import { useEffect, useMemo, useRef, useState } from 'react';
import { searchProject, type SheetMeta } from '../api/open';
import { startEngineSetup } from '../api/engineSetup';
import type {
  ProjectSearchAnchorWire,
  ProjectSearchGroupWire,
  ProjectSearchHitWire,
  ProjectSearchRequestWire,
  ProjectSearchResponseWire,
} from '../api/projectSearch';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { usePoll } from '../hooks/usePoll';

export interface PaletteSearchSeed {
  anchor?: ProjectSearchAnchorWire;
  namedIndexId?: string;
  label?: string;
}

export interface PaletteSearch {
  mode: 'lexical' | 'semantic' | 'hybrid';
  setMode(mode: 'lexical' | 'semantic' | 'hybrid'): void;
  rerank: boolean;
  setRerank(rerank: boolean): void;
  groups: ProjectSearchGroupWire[] | null;
  coverage: ProjectSearchResponseWire['coverage'] | null;
  searchError: string | null;
  canWatch: boolean;
  watchBusy: boolean;
  watchStatus: string | null;
  createSearchWatch(): void;
  sheetName(id: string | number): string;
  anchor: ProjectSearchAnchorWire | null;
  anchorLabel: string | null;
  findSimilar(hit: ProjectSearchHitWire): void;
  clearAnchor(): void;
  setupBusy: boolean;
  setupError: string | null;
  startSetup(setupRef: string, scope: 'workspace' | 'organization'): void;
}

type ResponseAnchor = NonNullable<ProjectSearchResponseWire['response_anchor']>;

function labelForResponseAnchor(anchor: ResponseAnchor): string | null {
  const { pages, time } = anchor.location;
  if (pages) {
    return pages.start === pages.end
      ? `page ${pages.start}`
      : `pages ${pages.start}–${pages.end}`;
  }
  if (time) {
    const media = anchor.location.kind.startsWith('audio')
      ? 'audio'
      : anchor.location.kind.startsWith('video')
        ? 'video'
        : 'time';
    return time.start_seconds === time.end_seconds
      ? `${media} ${time.start_seconds.toFixed(1)}s`
      : `${media} ${time.start_seconds.toFixed(1)}–${time.end_seconds.toFixed(1)}s`;
  }
  return null;
}

function locatedAnchor(hit: ProjectSearchHitWire): ProjectSearchAnchorWire {
  return {
    kind: 'located',
    source: hit.source,
    location: hit.location,
    binding: hit.binding,
    exclude_exact: true,
  };
}

function labelForHit(hit: ProjectSearchHitWire): string {
  return hit.snippet.replaceAll('<b>', '').replaceAll('</b>', '')
    || hit.column_name
    || 'selected result';
}

function searchRequest(
  query: string,
  mode: PaletteSearch['mode'],
  rerank: boolean,
  anchor: ProjectSearchAnchorWire | null,
  namedIndexId: string | undefined,
): ProjectSearchRequestWire | null {
  if (!anchor && !query) return null;
  return {
    scope: { kind: 'project' },
    query: anchor
      ? { kind: 'similar', anchor }
      : { kind: 'text', text: query, lexical_syntax: 'fts' },
    mode: anchor ? 'semantic' : mode,
    group_by: namedIndexId ? 'row' : 'source',
    limit: 50,
    ...(namedIndexId ? { named_index_id: namedIndexId } : {}),
    ranking: { rerank: rerank ? 'on' : 'off' },
  };
}

export function usePaletteSearch(
  query: string,
  searchSheets: SheetMeta[],
  onClose: () => void,
  seed?: PaletteSearchSeed | null,
): PaletteSearch {
  const {
    projectApi,
    chromePreferences: { projectId },
  } = useWorkspaceStores();
  const [mode, setMode] = useState<'lexical' | 'semantic' | 'hybrid'>('hybrid');
  const [rerank, setRerank] = useState(false);
  const [anchor, setAnchor] = useState<ProjectSearchAnchorWire | null>(
    seed?.anchor ?? null,
  );
  const [anchorLabel, setAnchorLabel] = useState<string | null>(
    seed?.label ?? (seed?.anchor ? 'selected source' : null),
  );
  const [namedIndexId, setNamedIndexId] = useState<string | undefined>(
    seed?.namedIndexId,
  );
  const [searchResult, setSearchResult] = useState<{
    key: string;
    response: ProjectSearchResponseWire;
  } | null>(null);
  const [searchFailure, setSearchFailure] = useState<{
    key: string;
    message: string;
  } | null>(null);
  const [searchRevision, setSearchRevision] = useState(0);
  const seq = useRef(0);
  const trimmedQuery = query.trim();
  const request = useMemo(
    () => searchRequest(trimmedQuery, mode, rerank, anchor, namedIndexId),
    [anchor, mode, namedIndexId, rerank, trimmedQuery],
  );
  const requestKey = request ? JSON.stringify(request) : '';

  useEffect(() => {
    if (!request) return;
    const mine = ++seq.current;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void searchProject(projectId, request, { signal: controller.signal })
        .then((response) => {
          if (seq.current === mine) {
            setSearchResult({ key: requestKey, response });
            setSearchFailure(null);
          }
        })
        .catch((error: Error) => {
          if (seq.current === mine && error.name !== 'AbortError') {
            setSearchFailure({ key: requestKey, message: error.message });
          }
        });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [projectId, request, requestKey, searchRevision]);

  const current = searchResult?.key === requestKey ? searchResult.response : null;
  const groups = request ? current?.groups ?? null : null;
  const coverage = request ? current?.coverage ?? null : null;
  const effectiveAnchorLabel = anchor && current?.response_anchor
    ? labelForResponseAnchor(current.response_anchor)
    : null;
  const searchError =
    request && searchFailure?.key === requestKey ? searchFailure.message : null;
  const preparing = coverage?.state === 'preparing'
    || coverage?.channels.some((channel) => channel.state === 'pending') === true;

  usePoll(async () => {
    if (!request) return;
    const mine = ++seq.current;
    try {
      const response = await searchProject(projectId, request);
      if (seq.current === mine) {
        setSearchResult({ key: requestKey, response });
        setSearchFailure(null);
      }
    } catch (error) {
      if (seq.current === mine) {
        setSearchFailure({
          key: requestKey,
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }, {
    active: Boolean(request) && preparing,
    intervalMs: 2_000,
    guardOverlap: true,
  });

  const [watchBusy, setWatchBusy] = useState(false);
  const [watchStatus, setWatchStatus] = useState<string | null>(null);
  const canWatch = Boolean(trimmedQuery) && !anchor && mode === 'lexical' && !watchBusy;
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
      .catch((error: Error) => setWatchStatus(error.message))
      .finally(() => setWatchBusy(false));
  };

  const [setupBusy, setSetupBusy] = useState(false);
  const [setupError, setSetupError] = useState<string | null>(null);
  const startSetup = (setupRef: string, scope: 'workspace' | 'organization') => {
    if (setupBusy) return;
    setSetupBusy(true);
    setSetupError(null);
    void startEngineSetup(scope, setupRef)
      .then(() => {
        setSearchResult(null);
        setSearchRevision((revision) => revision + 1);
      })
      .catch((error: Error) => setSetupError(error.message))
      .finally(() => setSetupBusy(false));
  };

  const sheetsById = useMemo(
    () => new Map(searchSheets.map((sheet) => [String(sheet.id), sheet])),
    [searchSheets],
  );
  const sheetName = (id: string | number) =>
    sheetsById.get(String(id))?.name ?? `sheet ${id}`;

  return {
    mode,
    setMode,
    rerank,
    setRerank,
    groups,
    coverage,
    searchError,
    canWatch,
    watchBusy,
    watchStatus,
    createSearchWatch,
    sheetName,
    anchor,
    anchorLabel: effectiveAnchorLabel ?? anchorLabel,
    findSimilar(hit) {
      setAnchor(locatedAnchor(hit));
      setAnchorLabel(labelForHit(hit));
    },
    clearAnchor() {
      setAnchor(null);
      setAnchorLabel(null);
      setNamedIndexId(undefined);
    },
    setupBusy,
    setupError,
    startSetup,
  };
}
