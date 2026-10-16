import { useCallback } from 'react';
import type { AskCitation } from '../api/projectQA';
import type { GridFilterSpec, GridSortSpec, SheetMeta } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type {
  DocumentViewState,
  EvidenceViewerFocus,
  EvidenceViewerHost,
  OpenSplitState,
} from '../state/chromeStore';

interface AskNavigationCommands {
  openEvidenceViewer(
    linkId: string | number,
    host?: EvidenceViewerHost,
    focus?: EvidenceViewerFocus,
  ): void;
  setDocumentView(documentView: DocumentViewState | null): void;
  setOpenSplit(split: OpenSplitState | null): void;
}

/** Open Ask sources through the workspace's ordinary route and viewer owners. */
export function useAskNavigation(
  sheets: SheetMeta[],
  commands: AskNavigationCommands,
) {
  const { route, gridView, selection, detail, workView } = useWorkspaceStores();

  const showGrid = useCallback((sheetId: string) => {
    commands.setOpenSplit(null);
    commands.setDocumentView(null);
    workView.setActivePromotedKey(null);
    workView.setAnswersViewSheetId(null);
    workView.setGridOnlySheetId(sheetId);
  }, [commands, workView]);

  const open = useCallback((citation: AskCitation) => {
    const target = citation.target;
    if (!target || target.kind === 'web') return;
    if (target.kind === 'evidence') {
      commands.openEvidenceViewer(target.evidence_link_id, 'modalOrPeek', {
        scopeSpanId: target.span_id,
        highlight: citation.status === 'current',
      });
      return;
    }

    const sheet = sheets.find((item) => item.id === String(target.sheet_id));
    if (!sheet) return;
    const documentNavigation = target.kind === 'cell' ? target.navigation : null;
    route.navigate({
      ...route.store.get(),
      sheetId: sheet.id,
      actionKind: null,
      review: false,
      panel: target.kind === 'cell' && !documentNavigation
        ? {
            kind: 'row',
            rowId: String(target.row_id),
            columnId: String(target.column_id),
          }
        : null,
    });
    showGrid(sheet.id);

    if (documentNavigation) {
      const rowId = String(documentNavigation.row_id);
      commands.setDocumentView({
        sheetId: String(documentNavigation.sheet_id),
        sourceColumnId: String(documentNavigation.column_id),
        titleColumnId: null,
        layout: 'continuous',
        fit: 'width',
        videoFit: 'full',
        textLayer: true,
        sync: false,
        activeRowId: rowId,
        readerLocationKey: crypto.randomUUID(),
        ...(documentNavigation.page
          ? { readerPage: { rowId, page: documentNavigation.page } }
          : {}),
        ...(documentNavigation.time_seconds !== null
          && documentNavigation.time_seconds !== undefined
          ? { readerTime: { rowId, seconds: documentNavigation.time_seconds } }
          : {}),
      });
      return;
    }

    if (target.kind === 'query') {
      gridView.store.set((state) => ({
        ...state,
        applied: {
          filter: target.filter as GridFilterSpec,
          sort: target.sort as GridSortSpec | null,
          filterValueLabel: null,
          scopeRowIds: target.row_ids ?? null,
        },
        activeSavedViewId: null,
      }));
      detail.clearForGridTransition();
      selection.clearRowSelection(sheet.id);
      return;
    }
  }, [commands, detail, gridView, route, selection, sheets, showGrid]);

  return { open };
}
