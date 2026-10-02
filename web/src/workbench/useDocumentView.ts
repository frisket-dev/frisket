import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DocumentListItem, DocumentListPage, Row, SheetMeta } from '../api/types';
import { resolveMediaValue } from '../media/resolveMediaValue';
import type { DocumentViewState } from '../workspace/useWorkspaceChromeState';
import { documentMediaKind, documentSources } from './documentMedia';
import { resolveTitleColumn } from './rowTitle';
import { useWindowedRowList } from './useWindowedRowList';

const PAGE_SIZE = 100;
const MAX_PAGES = 3;
const MAX_PAGE_COUNTS = PAGE_SIZE * MAX_PAGES;

interface ListState {
  key: string;
  pages: DocumentListPage[];
  pinned: DocumentListItem | null;
  loading: boolean;
  error: string | null;
}

export interface UseDocumentViewArgs {
  projectId: string;
  sheet: SheetMeta;
  state: DocumentViewState;
  onChangeState(next: DocumentViewState): void;
  onDocumentFocus(rowId: string): void;
  queryDocuments(args: {
    sourceColumnId: string; titleColumnId: string | null; query: string;
    cursor?: string; anchorRowId?: string; limit: number;
  }): Promise<DocumentListPage>;
  hydrateRow(rowId: string, columnIds: string[]): Promise<Row | null>;
  orderKey: string;
  titleColumnOrder?: readonly string[];
  annotatedTextColumnIds: readonly string[];
}

export function useDocumentView(args: UseDocumentViewArgs) {
  const { projectId, sheet, state, onChangeState, onDocumentFocus, queryDocuments,
    hydrateRow, orderKey, titleColumnOrder, annotatedTextColumnIds } = args;
  const sources = useMemo(() => documentSources(sheet, annotatedTextColumnIds), [sheet, annotatedTextColumnIds]);
  const source = useMemo(() => sources.find((entry) => String(entry.column.id) === state.sourceColumnId)
    ?? sources[0] ?? null, [sources, state.sourceColumnId]);
  const sourceColumn = source?.column ?? null;
  const defaultTitleColumn = useMemo(() => resolveTitleColumn(sheet, { columnOrder: titleColumnOrder }), [sheet, titleColumnOrder]);
  const titleColumn = useMemo(() => resolveTitleColumn(sheet, {
    overrideColumnId: state.titleColumnId, columnOrder: titleColumnOrder,
  }), [sheet, state.titleColumnId, titleColumnOrder]);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 180);
    return () => window.clearTimeout(timer);
  }, [search]);
  const listKey = `${projectId}:${sheet.id}:${orderKey}:${sourceColumn?.id ?? ''}:${titleColumn?.id ?? ''}:${query}`;
  const [list, setList] = useState<ListState>({ key: listKey, pages: [], pinned: null, loading: true, error: null });
  const visibleList = list.key === listKey ? list : { key: listKey, pages: [], pinned: null, loading: true, error: null };
  const [pageCounts, setPageCounts] = useState<Record<string, number>>({});
  const [optionsOpen, setOptionsOpen] = useState(false);
  const queryDocumentsRef = useRef(queryDocuments);
  const hydrateRowRef = useRef(hydrateRow);
  const listGeneration = useRef(0);
  const boundaryPending = useRef(false);
  useEffect(() => { queryDocumentsRef.current = queryDocuments; }, [queryDocuments]);
  useEffect(() => { hydrateRowRef.current = hydrateRow; }, [hydrateRow]);

  useEffect(() => {
    if (!sourceColumn) return;
    const generation = ++listGeneration.current;
    let cancelled = false;
    const request = (anchorRowId?: string) => queryDocumentsRef.current({ sourceColumnId: String(sourceColumn.id),
      titleColumnId: titleColumn ? String(titleColumn.id) : null, query, anchorRowId, limit: PAGE_SIZE });
    const anchored = state.activeRowId ?? undefined;
    void request(anchored)
      .catch((error: unknown) => anchored && typeof error === 'object' && error !== null
        && 'status' in error && error.status === 400 ? request() : Promise.reject(error))
      .then((page) => { if (!cancelled) setList({ key: listKey, pages: [page], pinned: null, loading: false, error: null }); })
      .catch((error: unknown) => { if (!cancelled) setList({ key: listKey, pages: [], pinned: null, loading: false,
        error: error instanceof Error ? error.message : 'Could not load documents.' }); });
    return () => { cancelled = true; if (listGeneration.current === generation) listGeneration.current += 1; };
    // activeRowId is the initial anchor, not a reason to restart the list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listKey, sourceColumn, titleColumn, query]);

  const items = useMemo(() => {
    const seen = new Set<string>();
    return visibleList.pages.flatMap((page) => page.items).filter((item) => !seen.has(item.rowId) && Boolean(seen.add(item.rowId)));
  }, [visibleList.pages]);
  const activeItem = items.find((item) => item.rowId === state.activeRowId)
    ?? (visibleList.pinned?.rowId === state.activeRowId ? visibleList.pinned : null)
    ?? items[0] ?? null;
  const activeRowId = activeItem?.rowId ?? null;

  const selectItem = useCallback((item: DocumentListItem) => {
    setList((prev) => ({ ...prev, pinned: item }));
    onChangeState({ ...state, sync: false, activeRowId: item.rowId });
    onDocumentFocus(item.rowId);
  }, [onChangeState, onDocumentFocus, state]);
  const selectDocument = useCallback((rowId: string) => {
    const item = items.find((candidate) => candidate.rowId === rowId);
    if (item) selectItem(item);
  }, [items, selectItem]);

  const loadBoundary = useCallback(async (direction: 'previous' | 'next', selectBoundary = false) => {
    if (visibleList.loading || boundaryPending.current || !sourceColumn || visibleList.pages.length === 0) return;
    const cursor = direction === 'next'
      ? visibleList.pages.at(-1)?.nextCursor : visibleList.pages[0]?.previousCursor;
    if (!cursor) return;
    const generation = listGeneration.current;
    const requestKey = listKey;
    boundaryPending.current = true;
    setList((prev) => ({ ...prev, loading: true }));
    try {
      const page = await queryDocumentsRef.current({ sourceColumnId: String(sourceColumn.id),
        titleColumnId: titleColumn ? String(titleColumn.id) : null, query, cursor, limit: PAGE_SIZE });
      if (generation !== listGeneration.current) return;
      setList((prev) => {
        if (prev.key !== requestKey) return prev;
        let pages = direction === 'next' ? [...prev.pages, page] : [page, ...prev.pages];
        const evicting = pages.length > MAX_PAGES;
        const current = prev.pages.flatMap((candidate) => candidate.items)
          .find((item) => item.rowId === activeRowId) ?? prev.pinned;
        if (pages.length > MAX_PAGES) pages = direction === 'next' ? pages.slice(-MAX_PAGES) : pages.slice(0, MAX_PAGES);
        return { ...prev, pages, pinned: evicting ? current ?? prev.pinned : prev.pinned, loading: false, error: null };
      });
      if (selectBoundary && page.items.length && generation === listGeneration.current) {
        selectItem(direction === 'next' ? page.items[0] : page.items.at(-1)!);
      }
    } catch (error) {
      if (generation === listGeneration.current) {
        setList((prev) => prev.key === requestKey ? ({ ...prev, loading: false,
          error: error instanceof Error ? error.message : 'Could not load documents.' }) : prev);
      }
    } finally {
      boundaryPending.current = false;
    }
  }, [activeRowId, listKey, query, selectItem, sourceColumn, titleColumn, visibleList]);

  const [hydrated, setHydrated] = useState<{ key: string; row: Row | null; loading: boolean; error: string | null }>({ key: '', row: null, loading: false, error: null });
  const hydrationColumns = useMemo(() => Array.from(new Set([
    source?.kind === 'media' ? sourceColumn?.id : null,
  ].filter(Boolean).map(String))), [source, sourceColumn]);
  const hydrationKey = `${projectId}:${sheet.id}:${activeRowId ?? ''}:${hydrationColumns.join(',')}`;
  useEffect(() => {
    if (!activeRowId) return;
    let cancelled = false;
    void hydrateRowRef.current(activeRowId, hydrationColumns).then((row) => {
      if (!cancelled) setHydrated({ key: hydrationKey, row, loading: false, error: null });
    }).catch((error: unknown) => {
      if (!cancelled) setHydrated({ key: hydrationKey, row: null, loading: false, error: error instanceof Error ? error.message : 'Could not load document.' });
    });
    return () => { cancelled = true; };
  }, [activeRowId, hydrationColumns, hydrationKey]);
  const activeRow = hydrated.key === hydrationKey ? hydrated.row : null;
  const activeMedia = activeRow && source?.kind === 'media'
    ? resolveMediaValue(activeRow.cells[String(source.column.id)] ?? null, projectId) : null;
  const activeMediaKind = activeMedia ? documentMediaKind(activeMedia, sourceColumn?.type ?? 'file') : null;
  const recordPageCount = useCallback((rowKey: string, count: number | null) => {
    if (count === null) return;
    setPageCounts((prev) => {
      const entries = Object.entries(prev).filter(([key]) => key !== rowKey).slice(-(MAX_PAGE_COUNTS - 1));
      return Object.fromEntries([...entries, [rowKey, count]]);
    });
  }, []);
  const navigateBoundary = useCallback(async (direction: 'previous' | 'next') => {
    if (!activeRowId || items.some((item) => item.rowId === activeRowId) || !sourceColumn) {
      await loadBoundary(direction, true);
      return;
    }
    const generation = listGeneration.current;
    try {
      const anchorPage = await queryDocumentsRef.current({ sourceColumnId: String(sourceColumn.id),
        titleColumnId: titleColumn ? String(titleColumn.id) : null, query, anchorRowId: activeRowId, limit: PAGE_SIZE });
      if (generation !== listGeneration.current) return;
      if (direction === 'next') {
        const next = anchorPage.items.find((item) => item.rowId !== activeRowId);
        setList((prev) => prev.key === listKey ? { ...prev, pages: [anchorPage], loading: false } : prev);
        if (next) selectItem(next);
        else if (anchorPage.nextCursor) await loadBoundary('next', true);
        return;
      }
      if (!anchorPage.previousCursor) return;
      const previousPage = await queryDocumentsRef.current({ sourceColumnId: String(sourceColumn.id),
        titleColumnId: titleColumn ? String(titleColumn.id) : null, query,
        cursor: anchorPage.previousCursor, limit: PAGE_SIZE });
      if (generation !== listGeneration.current) return;
      setList((prev) => prev.key === listKey ? { ...prev, pages: [previousPage, anchorPage], loading: false } : prev);
      const previous = previousPage.items.at(-1);
      if (previous) selectItem(previous);
    } catch (error) {
      if (generation === listGeneration.current) setList((prev) => prev.key === listKey ? { ...prev,
        error: error instanceof Error ? error.message : 'Could not load documents.' } : prev);
    }
  }, [activeRowId, items, listKey, loadBoundary, query, selectItem, sourceColumn, titleColumn]);
  const { listBodyRef, onListScroll, onListKeyDown, startIndex, windowRows } = useWindowedRowList({
    rows: items.map((item) => ({ ...item, id: item.rowId })), activeId: activeRowId,
    onSelect: selectDocument, onBoundary: (direction) => void navigateBoundary(direction),
  });
  const setState = useCallback((patch: Partial<DocumentViewState>) => onChangeState({ ...state, ...patch }), [state, onChangeState]);
  return { sources, source, sourceColumn, defaultTitleColumn, titleColumn, list: visibleList, items,
    pageCounts, search, setSearch, optionsOpen, setOptionsOpen, listBodyRef, activeRowId, activeItem,
    activeRow, activeMedia, activeMediaKind, hydrationLoading: hydrated.key !== hydrationKey || hydrated.loading,
    hydrationError: hydrated.key === hydrationKey ? hydrated.error : null, recordPageCount, selectDocument,
    onListKeyDown, onListScroll, startIndex, windowRows, loadMore: () => loadBoundary('next'), setState };
}
