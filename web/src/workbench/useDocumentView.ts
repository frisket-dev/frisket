// Container/presenter split for DocumentView: this hook owns loading + client-side
// title search + the active-document resolution + keyboard nav + lightweight row
// virtualization; the component owns only the JSX.
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Row, SheetMeta } from '../api/types';
import { resolveMediaValue, type ResolvedMediaValue } from '../media/resolveMediaValue';
import type { DocumentViewState } from '../workspace/useWorkspaceChromeState';
import {
  documentMediaKind,
  documentSources,
  type DocumentMediaKind,
  type DocumentSource,
} from './documentMedia';
import { resolveTitleColumn, rowTitle as resolveRowTitle } from './rowTitle';
import { useWindowedRowList } from './useWindowedRowList';

const LIST_PAGE_SIZE = 100;

interface ListState {
  key: string;
  rows: Row[];
  total: number;
  loading: boolean;
  error: string | null;
}

export interface UseDocumentViewArgs {
  projectId: string;
  sheet: SheetMeta;
  state: DocumentViewState;
  onChangeState(next: DocumentViewState): void;
  onDocumentFocus(rowId: string): void;
  queryRows(args: { columnIds?: string[]; offset: number; limit: number }): Promise<{
    rows: Row[];
    total: number;
  }>;
  orderKey: string;
  titleColumnOrder?: readonly string[];
  /** SheetMeta.annotatedTextColumnIds — what makes a text column readable as a
   *  document at all (documentSources). */
  annotatedTextColumnIds: readonly string[];
}

export function useDocumentView({
  projectId,
  sheet,
  state,
  onChangeState,
  onDocumentFocus,
  queryRows,
  orderKey,
  titleColumnOrder,
  annotatedTextColumnIds,
}: UseDocumentViewArgs) {
  const sources = useMemo(
    () => documentSources(sheet, annotatedTextColumnIds),
    [sheet, annotatedTextColumnIds],
  );
  const source: DocumentSource | null = useMemo(
    () =>
      sources.find((entry) => String(entry.column.id) === state.sourceColumnId) ??
      sources[0] ??
      null,
    [sources, state.sourceColumnId],
  );
  const sourceColumn = source?.column ?? null;
  // state.titleColumnId is now a per-VIEW override of the sheet-level
  // default (sheet.titleColumnId, set via the column '...' menu's "Use as
  // row title") — resolveTitleColumn/rowTitle (./rowTitle) are the ONE
  // shared implementation of this priority order, also adopted by
  // InspectDetailColumn and App.tsx's document-view sort-direction lookup.
  const defaultTitleColumn = useMemo(
    () => resolveTitleColumn(sheet, { columnOrder: titleColumnOrder }),
    [sheet, titleColumnOrder],
  );
  const titleColumn = useMemo(
    () =>
      resolveTitleColumn(sheet, {
        overrideColumnId: state.titleColumnId,
        columnOrder: titleColumnOrder,
      }),
    [sheet, state.titleColumnId, titleColumnOrder],
  );

  const listKey = `${sheet.id}:${orderKey}`;
  const [list, setList] = useState<ListState>({
    key: listKey,
    rows: [],
    total: sheet.rowCount,
    loading: true,
    error: null,
  });
  const visibleList: ListState = useMemo(
    () => list.key === listKey
      ? list
      : {
          key: listKey,
          rows: [],
          total: sheet.rowCount,
          loading: true,
          error: null,
        },
    [list, listKey, sheet.rowCount],
  );
  const [pageCounts, setPageCounts] = useState<Record<string, number>>({});
  const [search, setSearch] = useState('');
  const [optionsOpen, setOptionsOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setList({ key: listKey, rows: [], total: sheet.rowCount, loading: true, error: null });
    void queryRows({ offset: 0, limit: LIST_PAGE_SIZE })
      .then((page) => {
        if (cancelled) return;
        setList({ key: listKey, rows: page.rows, total: page.total, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setList({
          key: listKey,
          rows: [],
          total: sheet.rowCount,
          loading: false,
          error: err instanceof Error ? err.message : 'Could not load documents.',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [listKey, queryRows, sheet.rowCount]);

  const loadMore = useCallback(async () => {
    if (visibleList.loading || visibleList.rows.length >= visibleList.total) return;
    setList((prev) => ({ ...prev, loading: true }));
    try {
      const page = await queryRows({ offset: visibleList.rows.length, limit: LIST_PAGE_SIZE });
      setList((prev) => ({
        key: prev.key,
        rows: [...prev.rows, ...page.rows],
        total: page.total,
        loading: false,
        error: null,
      }));
    } catch (err) {
      setList((prev) => ({
        ...prev,
        loading: false,
        error: err instanceof Error ? err.message : 'Could not load documents.',
      }));
    }
  }, [queryRows, visibleList]);

  // A text source resolves to no media at ALL layers (resolveMediaValue,
  // documentMediaKind) — which is correct, and is why the reader for it is a
  // separate component rather than another branch of DocumentReader.
  const resolveRowMedia = useCallback(
    (row: Row): ResolvedMediaValue | null => {
      if (source === null || source.kind !== 'media') return null;
      return resolveMediaValue(row.cells[String(source.column.id)] ?? null, projectId);
    },
    [projectId, source],
  );

  const rowTitle = useCallback(
    (row: Row, media: ResolvedMediaValue | null): string =>
      resolveRowTitle(sheet, row, {
        overrideColumnId: state.titleColumnId,
        columnOrder: titleColumnOrder,
        media,
      }),
    [sheet, state.titleColumnId, titleColumnOrder],
  );

  // Client-side title SEARCH over the loaded rows (a list convenience — distinct
  // from the sheet's filter/sort, which the list always honors as its order).
  const filteredRows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return visibleList.rows;
    return visibleList.rows.filter((row) => {
      const media = resolveRowMedia(row);
      return rowTitle(row, media).toLowerCase().includes(needle);
    });
  }, [visibleList, search, resolveRowMedia, rowTitle]);

  // The active document is local reading state, not an action row selection.
  // Fall back to the first row so the reader always previews something.
  const activeRowId = useMemo(() => {
    const ids = new Set(filteredRows.map((row) => String(row.id)));
    if (state.activeRowId && ids.has(state.activeRowId)) {
      return state.activeRowId;
    }
    return filteredRows[0] ? String(filteredRows[0].id) : null;
  }, [filteredRows, state.activeRowId]);

  const activeRow = useMemo(
    () => filteredRows.find((row) => String(row.id) === activeRowId) ?? null,
    [filteredRows, activeRowId],
  );
  const activeMedia = activeRow ? resolveRowMedia(activeRow) : null;
  const activeMediaKind: DocumentMediaKind | null = activeMedia
    ? documentMediaKind(activeMedia, sourceColumn?.type ?? 'file')
    : null;

  const selectDocument = useCallback(
    (rowId: string) => {
      onChangeState({ ...state, sync: false, activeRowId: rowId });
      // If Row Detail is already open, keep that reader aimed at the document
      // without turning document focus into an action selection.
      onDocumentFocus(rowId);
    },
    [state, onDocumentFocus, onChangeState],
  );

  const recordPageCount = useCallback((rowKey: string, count: number | null) => {
    setPageCounts((prev) => {
      if (count === null || prev[rowKey] === count) return prev;
      return { ...prev, [rowKey]: count };
    });
  }, []);

  // Scroll/window/measure + keyboard document nav (↑/↓ change the DOCUMENT)
  // — shared with AnswersView's row list (useWindowedRowList). Row.id is
  // already typed `string`, so `activeRowId`/`selectDocument` need no
  // String() wrap here.
  const { listBodyRef, onListScroll, onListKeyDown, startIndex, windowRows } = useWindowedRowList({
    rows: filteredRows,
    activeId: activeRowId,
    onSelect: selectDocument,
  });

  const setState = useCallback(
    (patch: Partial<DocumentViewState>) => onChangeState({ ...state, ...patch }),
    [state, onChangeState],
  );

  return {
    sources,
    source,
    sourceColumn,
    defaultTitleColumn,
    titleColumn,
    list: visibleList,
    pageCounts,
    search,
    setSearch,
    optionsOpen,
    setOptionsOpen,
    listBodyRef,
    filteredRows,
    activeRowId,
    activeRow,
    activeMedia,
    activeMediaKind,
    resolveRowMedia,
    rowTitle,
    recordPageCount,
    selectDocument,
    onListKeyDown,
    onListScroll,
    startIndex,
    windowRows,
    loadMore,
    setState,
  };
}
