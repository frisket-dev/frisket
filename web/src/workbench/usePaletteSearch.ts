import { useEffect, useMemo, useRef, useState } from 'react';
import {
  MAX_LENS_VIEW_ROWS,
  getModelPull,
  listModelPulls,
  orgGetModelPull,
  orgListModelPulls,
  searchProject,
  type EmbeddingIndexSummary,
  type ModelPullDto,
  type SheetMeta,
} from '../api/open';
import { ModelSetupRequestError, startEngineSetup } from '../api/engineSetup';
import type {
  ProjectSearchAnchorWire,
  ProjectSearchGroupWire,
  ProjectSearchHitWire,
  ProjectSearchRequestWire,
  ProjectSearchResponseWire,
} from '../api/projectSearch';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { parseComposedQuery } from '../embeddings/composedQuery';
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
  namedIndexes: EmbeddingIndexSummary[];
  namedIndexId: string | undefined;
  setNamedIndexId(indexId: string | undefined): void;
  namedIndexError: string | null;
  canSaveLens: boolean;
  saveBusy: boolean;
  saveStatus: string | null;
  saveSearchLens(): void;
  sheetName(id: string | number): string;
  anchor: ProjectSearchAnchorWire | null;
  anchorLabel: string | null;
  findSimilar(hit: ProjectSearchHitWire): void;
  clearAnchor(): void;
  setupBusy: boolean;
  setupError: string | null;
  setupOperation: PaletteSearchSetupOperation | null;
  startSetup(setupRef: string, scope: 'workspace' | 'organization'): void;
  finishSetup(pull: ModelPullDto): void;
}

export interface PaletteSearchSetupOperation {
  setupRef: string;
  scope: 'workspace' | 'organization';
  pull: ModelPullDto;
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

function anchorRowId(anchor: ProjectSearchAnchorWire | null): number | null {
  if (!anchor) return null;
  if (anchor.kind === 'cell') return anchor.row_id;
  return anchor.binding?.row_id ?? null;
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
  activeSheetId: string | number | null | undefined,
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
  const activeSheetKey = activeSheetId == null ? null : String(activeSheetId);
  const [namedIndexLoad, setNamedIndexLoad] = useState<{
    sheetKey: string;
    indexes: EmbeddingIndexSummary[];
    error: string | null;
  } | null>(null);

  useEffect(() => {
    let live = true;
    if (activeSheetKey == null) return undefined;
    void projectApi.embeddingIndexes(Number(activeSheetId))
      .then((indexes) => {
        if (live) setNamedIndexLoad({ sheetKey: activeSheetKey, indexes, error: null });
      })
      .catch((error: Error) => {
        if (live) setNamedIndexLoad({ sheetKey: activeSheetKey, indexes: [], error: error.message });
      });
    return () => { live = false; };
  }, [activeSheetId, activeSheetKey, projectApi]);
  const namedIndexes = namedIndexLoad?.sheetKey === activeSheetKey
    ? namedIndexLoad.indexes
    : [];
  const namedIndexError = namedIndexLoad?.sheetKey === activeSheetKey
    ? namedIndexLoad.error
    : null;
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
  const selectedIndex = namedIndexes.find((index) => index.indexId === namedIndexId);
  const selectedRowId = anchorRowId(anchor);
  const canWatch = !watchBusy && (
    (Boolean(trimmedQuery) && !anchor && !namedIndexId && mode === 'lexical')
    || (selectedIndex?.sheetId != null && selectedRowId != null)
  );
  const createSearchWatch = () => {
    if (!canWatch) return;
    const similarity = selectedIndex?.sheetId != null && selectedRowId != null;
    setWatchBusy(true);
    setWatchStatus(null);
    void projectApi
      .createWatch(similarity
        ? {
            name: `Similar to ${anchorLabel ?? `row ${selectedRowId}`}`,
            scope: { kind: 'sheet', sheet_id: selectedIndex.sheetId },
            query: {
              kind: 'embedding_similarity',
              embedding_index_id: selectedIndex.indexId,
              anchor: { kind: 'row', row_id: selectedRowId },
            },
          }
        : {
            name: `Search: ${trimmedQuery}`,
            scope: { kind: 'project' },
            query: {
              kind: 'fts',
              q: trimmedQuery,
              mode: 'keyword',
              rerank: rerank ? 'auto' : 'off',
            },
          })
      .then(() => {
        window.dispatchEvent(new Event('frisket:watches-changed'));
        onClose();
      })
      .catch((error: Error) => setWatchStatus(error.message))
      .finally(() => setWatchBusy(false));
  };

  const [saveBusy, setSaveBusy] = useState(false);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const canSaveLens = !saveBusy
    && selectedIndex?.sheetId != null
    && (selectedRowId != null || Boolean(trimmedQuery));
  const saveSearchLens = () => {
    if (!canSaveLens || !selectedIndex || selectedIndex.sheetId == null) return;
    let lensName: string;
    let lensQuery: Record<string, unknown>;
    if (selectedRowId != null) {
      lensName = `Similar to ${anchorLabel ?? `row ${selectedRowId}`}`;
      lensQuery = {
        kind: 'embedding_similarity',
        embedding_index_id: selectedIndex.indexId,
        sheet_id: selectedIndex.sheetId,
        anchor: { kind: 'row', row_id: selectedRowId },
        limit: MAX_LENS_VIEW_ROWS,
      };
    } else if (mode === 'hybrid') {
      lensName = `Search: ${trimmedQuery}`;
      lensQuery = {
        kind: 'embedding_hybrid',
        embedding_index_id: selectedIndex.indexId,
        sheet_id: selectedIndex.sheetId,
        text: trimmedQuery,
      };
    } else {
      const { terms, exclude } = parseComposedQuery(trimmedQuery);
      if (!terms.length) {
        setSaveStatus('Add a search term before saving this view.');
        return;
      }
      lensName = `Search: ${trimmedQuery}`;
      lensQuery = {
        kind: 'embedding_similarity',
        embedding_index_id: selectedIndex.indexId,
        sheet_id: selectedIndex.sheetId,
        anchor: {
          kind: 'manual_text_query',
          terms,
          ...(exclude.length ? { exclude } : {}),
        },
      };
    }
    setSaveBusy(true);
    setSaveStatus(null);
    void projectApi.saveLens({ name: lensName, query: lensQuery })
      .then(() => {
        setSaveStatus('Saved view');
        window.dispatchEvent(new Event('frisket:lenses-changed'));
      })
      .catch((error: Error) => setSaveStatus(error.message))
      .finally(() => setSaveBusy(false));
  };

  const [setupBusy, setSetupBusy] = useState(false);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [setupOperation, setSetupOperation] = useState<PaletteSearchSetupOperation | null>(null);
  const advertisedSetup = coverage?.channels.find((channel) => channel.setup)?.setup ?? null;

  usePoll(async () => {
    if (!advertisedSetup) return 'stop' as const;
    try {
      const result = advertisedSetup.scope === 'organization'
        ? await orgListModelPulls()
        : await listModelPulls();
      const pull = result.pulls
        .filter((candidate) => (
          candidate.operation_kind === 'engine_setup'
          && candidate.model === advertisedSetup.setup_ref
        ))
        .sort((left, right) => right.id - left.id)[0];
      if (pull) {
        setSetupOperation({
          setupRef: advertisedSetup.setup_ref,
          scope: advertisedSetup.scope,
          pull,
        });
        if (pull.status === 'done') {
          setSearchResult(null);
          setSearchRevision((revision) => revision + 1);
        }
      }
    } catch {
      // Recovery is best effort. The setup action remains available when no
      // durable operation can be restored.
    }
    return 'stop' as const;
  }, {
    active: Boolean(advertisedSetup)
      && setupOperation?.setupRef !== advertisedSetup?.setup_ref,
    intervalMs: 1_000,
    immediate: true,
    maxAttempts: 1,
    guardOverlap: true,
  });

  const startSetup = (setupRef: string, scope: 'workspace' | 'organization') => {
    if (setupBusy) return;
    setSetupBusy(true);
    setSetupError(null);
    void startEngineSetup(scope, setupRef)
      .then(({ pull }) => {
        setSetupOperation({ setupRef, scope, pull });
      })
      .catch(async (error: Error) => {
        if (error instanceof ModelSetupRequestError && error.activePullId != null) {
          try {
            const pull = scope === 'organization'
              ? await orgGetModelPull(error.activePullId)
              : await getModelPull(error.activePullId);
            setSetupOperation({ setupRef, scope, pull });
            return;
          } catch (recoveryError) {
            setSetupError(recoveryError instanceof Error
              ? recoveryError.message
              : String(recoveryError));
            return;
          }
        }
        setSetupError(error.message);
      })
      .finally(() => setSetupBusy(false));
  };
  const finishSetup = (pull: ModelPullDto) => {
    const matching = pull.operation_kind === 'engine_setup'
      && setupOperation?.setupRef === pull.model;
    setSetupOperation((currentOperation) => {
      if (currentOperation?.pull.id !== pull.id) return currentOperation;
      return matching ? { ...currentOperation, pull } : null;
    });
    if (matching && pull.status === 'done') {
      setSearchResult(null);
      setSearchRevision((revision) => revision + 1);
    }
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
    namedIndexes,
    namedIndexId,
    setNamedIndexId(indexId) {
      setNamedIndexId(indexId);
      if (indexId && mode === 'lexical') setMode('semantic');
      setWatchStatus(null);
      setSaveStatus(null);
    },
    namedIndexError,
    canSaveLens,
    saveBusy,
    saveStatus,
    saveSearchLens,
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
    setupOperation,
    startSetup,
    finishSetup,
  };
}
