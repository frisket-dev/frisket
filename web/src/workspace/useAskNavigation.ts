import { useCallback, useEffect, useRef, useState } from 'react';
import type { AskCitation } from '../api/projectQA';
import type { GridFilterSpec, GridSortSpec, SheetMeta } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { documentSources } from '../workbench/documentMedia';

/** A single return point, using the normal route owner and workspace stores. */
export function useAskNavigation(sheets: SheetMeta[]) {
  const { route, gridView, selection, detail, workView, chrome } = useWorkspaceStores();
  const [source, setSource] = useState<AskCitation | null>(null);
  const previous = useRef<{
    grid: ReturnType<typeof gridView.store.get>;
    selection: ReturnType<typeof selection.store.get>;
    detail: ReturnType<typeof detail.store.get>;
    work: ReturnType<typeof workView.store.get>;
    document: ReturnType<typeof chrome.store.get>['documentView'];
    split: ReturnType<typeof chrome.store.get>['openSplit'];
  } | null>(null);

  const open = useCallback((citation: AskCitation) => {
    const target = citation.target;
    if (!target) return;
    const sheet = sheets.find((item) => item.id === String(target.sheet_id));
    if (!sheet) return;
    if (!previous.current) previous.current = {
      grid: gridView.store.get(), selection: selection.store.get(), detail: detail.store.get(),
      work: workView.store.get(), document: chrome.store.get().documentView, split: chrome.store.get().openSplit,
    };
    route.beginTemporary({ ...route.store.get(), sheetId: sheet.id, actionKind: null, review: false, panel: null });
    gridView.store.set((state) => ({ ...state, activeSavedViewId: null, applied: {
      filter: target.kind === 'query' ? target.filter as GridFilterSpec : null,
      sort: target.kind === 'query' ? target.sort as GridSortSpec | null : null,
      filterValueLabel: null,
      scopeRowIds: target.kind === 'query' ? target.row_ids : [target.row_id],
    } }));
    selection.clearRowSelection(sheet.id);
    detail.clearRowDrawer();
    chrome.setTransientReader({ openSplit: null, documentView: null });
    workView.setAnswersViewSheetId(null);
    workView.setGridOnlySheetId(sheet.id);
    if (target.kind === 'cell' && documentSources(sheet, sheet.annotatedTextColumnIds ?? []).some((item) => item.column.id === String(target.column_id))) {
      chrome.setTransientReader({ documentView: { sheetId: sheet.id, sourceColumnId: String(target.column_id), titleColumnId: null, layout: 'continuous', fit: 'width', videoFit: 'full', textLayer: true, sync: false, activeRowId: String(target.row_id) } });
    }
    setSource(citation);
  }, [sheets, route, gridView, selection, detail, workView, chrome]);

  const back = useCallback(() => {
    const snapshot = previous.current;
    route.finishTemporary('restore');
    if (snapshot) {
      gridView.store.set(snapshot.grid);
      selection.store.set(snapshot.selection);
      detail.store.set(snapshot.detail);
      workView.store.set(snapshot.work);
      chrome.setTransientReader({ documentView: snapshot.document, openSplit: snapshot.split });
    }
    previous.current = null;
    setSource(null);
  }, [route, gridView, selection, detail, workView, chrome]);

  const commit = useCallback(() => {
    route.finishTemporary('commit');
    previous.current = null;
    setSource(null);
  }, [route]);

  // Ordinary route changes discard this disposable view. URL echoes of the
  // unchanged base route are ignored by its single canonical owner.
  useEffect(() => route.store.subscribe(() => {
    if (!route.isTemporary()) { previous.current = null; setSource(null); }
  }), [route]);

  return { source, open, back, commit };
}
