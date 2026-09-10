
import type { WorkspaceStores } from './createWorkspaceStores';
import type {
  GridViewFilterApplication,
  GridViewSavedViewApplication,
  GridViewSortApplication,
} from './gridViewStore';
import type { DocumentViewState, OpenSplitState, PromotedView, WorkViewKind } from './chromeStore';
import type { ActionLaunch, AddColumnPrompt } from './actSurfaceStore';
import type { ActionLaunchPrefill } from '../actions/actionFormInitial';
import { type RouteState } from '../core/route/RouteState';
import type { ColumnDef, PreviewCellDetail, Row } from '../api/open';
import type { LensGridView } from './lensViewStore';
import type { CompareKind } from './compareViewStore';

type FilterSortStores = Pick<WorkspaceStores, 'gridView' | 'detail' | 'selection'>;
type ClearStores = Pick<WorkspaceStores, 'gridView' | 'selection'>;
type SelectSheetStores = Pick<
  WorkspaceStores,
  'gridView' | 'selection' | 'detail' | 'workView' | 'lensView' | 'previewView'
>;

/** Callers must pass the bind-layer wrappers that persist these chrome writes. */
export interface PersistedChromeWrites {
  setOpenSplit(split: OpenSplitState | null): void;
  setDocumentView(documentView: DocumentViewState | null): void;
  setPromotedViews(views: PromotedView[]): void;
}

export function applyGridFilterTransition(
  stores: FilterSortStores,
  sheetId: string,
  filter: GridViewFilterApplication,
): void {
  stores.gridView.applyFilter(filter);
  stores.detail.clearForGridTransition();
  stores.selection.clearRowSelection(sheetId);
}

/** Applying a bbox filter reveals the grid and demotes a compatible promoted view into the split. */
export function applyGridBboxFilterTransition(
  stores: Pick<WorkspaceStores, 'gridView' | 'detail' | 'selection' | 'workView'>,
  chrome: PersistedChromeWrites,
  layout: { promotedViews: PromotedView[]; activePromotedKey: string | null },
  sheetId: string,
  filter: GridViewFilterApplication,
): void {
  applyGridFilterTransition(stores, sheetId, filter);
  const hidingView = layout.activePromotedKey
    ? layout.promotedViews.find(
        (view) => view.key === layout.activePromotedKey && view.sheetId === sheetId,
      )
    : undefined;
  if (!hidingView) return;
  stores.workView.setActivePromotedKey(null);
  if (hidingView.kind === 'map' || hidingView.kind === 'graph') {
    chrome.setPromotedViews(
      layout.promotedViews.filter((view) => view.key !== hidingView.key),
    );
    chrome.setOpenSplit({
      kind: hidingView.kind,
      sheetId: hidingView.sheetId,
      columnId: hidingView.columnId,
    });
  }
}

export function applyGridSortTransition(
  stores: FilterSortStores,
  sheetId: string,
  sort: GridViewSortApplication,
): void {
  stores.gridView.applySort(sort);
  stores.detail.clearForGridTransition();
  stores.selection.clearRowSelection(sheetId);
}

export function clearGridFilterTransition(stores: ClearStores, sheetId: string | undefined): void {
  stores.gridView.clearFilter();
  if (sheetId) stores.selection.clearRowSelection(sheetId);
}

export function clearGridSortTransition(stores: ClearStores, sheetId: string | undefined): void {
  stores.gridView.clearSort();
  if (sheetId) stores.selection.clearRowSelection(sheetId);
}

/** Applying a saved view deliberately preserves the child filter. */
export function applySavedViewTransition(
  stores: FilterSortStores,
  sheetId: string,
  grid: GridViewSavedViewApplication,
): void {
  stores.gridView.applySavedViewGrid(grid);
  stores.detail.clearForSavedView();
  stores.selection.clearRowSelection(sheetId);
}

/** Route changes reset classes 1–2 here; class 3 hydrates after route commit. */
export function resetForRouteSheetChange(stores: SelectSheetStores, sheetId: string): void {
  stores.gridView.resetForSheetChange();
  stores.selection.clearRowSelection(sheetId);
  stores.detail.closeHeaderMenu();
  stores.workView.resetForSheetChange();
  stores.lensView.resetForSheetChange();
  stores.previewView.resetForSheetChange();
}


type WorkViewHandle = WorkspaceStores['workView'];
type CompareViewHandle = WorkspaceStores['compareView'];

export function setWorkViewResetTransition(
  chrome: Pick<PersistedChromeWrites, 'setDocumentView'>,
  workView: WorkViewHandle,
  compareView: CompareViewHandle,
  kind: WorkViewKind,
): void {
  workView.setActivePromotedKey(null);
  compareView.standDown();
  if (kind !== 'document') chrome.setDocumentView(null);
  if (kind !== 'answers') workView.setAnswersViewSheetId(null);
}

export function setWorkViewGridTransition(
  chrome: Pick<PersistedChromeWrites, 'setOpenSplit'>,
  workView: WorkViewHandle,
  sheetId: string | null,
): void {
  chrome.setOpenSplit(null);
  workView.setGridOnlySheetId(sheetId);
}

export function setWorkViewDocumentTransition(
  chrome: Pick<PersistedChromeWrites, 'setOpenSplit' | 'setDocumentView'>,
  workView: WorkViewHandle,
  sheetId: string | null,
  documentView: DocumentViewState | null,
): void {
  chrome.setOpenSplit(null);
  workView.setGridOnlySheetId(null);
  if (sheetId && documentView) chrome.setDocumentView(documentView);
}

/** Switching to Answers preserves its selected column and active link. */
export function setWorkViewAnswersTransition(
  chrome: Pick<PersistedChromeWrites, 'setOpenSplit'>,
  workView: WorkViewHandle,
  sheetId: string | null,
): void {
  chrome.setOpenSplit(null);
  workView.setGridOnlySheetId(null);
  workView.setAnswersViewSheetId(sheetId);
}

export function setWorkViewGalleryTransition(
  chrome: Pick<PersistedChromeWrites, 'setOpenSplit'>,
  workView: WorkViewHandle,
): void {
  chrome.setOpenSplit(null);
  workView.setGridOnlySheetId(null);
}

export function promoteCurrentViewTransition(
  chrome: Pick<PersistedChromeWrites, 'setOpenSplit' | 'setPromotedViews'>,
  workView: WorkViewHandle,
  promotedViews: PromotedView[],
  newView: PromotedView,
): void {
  if (!promotedViews.some((view) => view.key === newView.key)) {
    chrome.setPromotedViews([...promotedViews, newView]);
  }
  chrome.setOpenSplit(null);
  workView.setGridOnlySheetId(null);
  workView.setActivePromotedKey(newView.key);
}

/** The caller selects the sheet before activating the promoted key. */
export function openPromotedTabTransition(
  chrome: Pick<PersistedChromeWrites, 'setOpenSplit'>,
  workView: WorkViewHandle,
  compareView: CompareViewHandle,
): void {
  chrome.setOpenSplit(null);
  workView.setGridOnlySheetId(null);
  compareView.standDown();
}

export function selectSheetTabTransition(
  workView: WorkViewHandle,
  compareView: CompareViewHandle,
): void {
  workView.setActivePromotedKey(null);
  workView.setGridOnlySheetId(null);
  compareView.standDown();
}

export function closePromotedTabTransition(
  chrome: Pick<PersistedChromeWrites, 'setPromotedViews'>,
  workView: WorkViewHandle,
  promotedViews: PromotedView[],
  view: PromotedView,
  activePromotedKey: string | null,
): boolean {
  chrome.setPromotedViews(promotedViews.filter((candidate) => candidate.key !== view.key));
  if (activePromotedKey === view.key) {
    workView.setActivePromotedKey(null);
    return true;
  }
  return false;
}

/** Only the route-matching launch may hydrate class-3 state. */
export function actionLaunchForRoute(
  launch: ActionLaunch | null,
  routeActionKind: string | null | undefined,
): ActionLaunch | null {
  return launch && launch.kind === routeActionKind ? launch : null;
}

/** Always write a launch record so repeated plain-tile clicks get distinct identities. */
export function runActionFromSurfaceTransition(
  stores: Pick<WorkspaceStores, 'actSurface' | 'chrome' | 'route'>,
  actionKind: string,
  sourceColumn: string | undefined,
  nextRoute: RouteState,
  initial?: ActionLaunchPrefill,
): void {
  stores.actSurface.setActionLaunch(
    {
      kind: actionKind,
      ...((sourceColumn || initial) ? {
        initial: { ...initial, ...(sourceColumn ? { sourceColumn } : {}) },
      } : {}),
    },
  );
  stores.chrome.openActionPanel();
  stores.route.navigate(nextRoute);
}

export function insertColumnBesideTransition(
  stores: Pick<WorkspaceStores, 'detail' | 'actSurface'>,
  prompt: AddColumnPrompt,
): void {
  stores.actSurface.setAddColumnPrompt(prompt);
  stores.detail.closeHeaderMenu();
}

export function completeAddColumnTransition(
  stores: Pick<WorkspaceStores, 'gridView' | 'actSurface'>,
  sheetId: string,
  columnOrder: string[],
): void {
  stores.gridView.setColumnOrder(sheetId, columnOrder);
  stores.actSurface.closeAddColumnPrompt();
}


export function openCompareTabTransition(
  chrome: Pick<PersistedChromeWrites, 'setOpenSplit'>,
  workView: WorkViewHandle,
  compareView: CompareViewHandle,
  kind: CompareKind,
): void {
  compareView.open(kind);
  workView.setActivePromotedKey(null);
  workView.setGridOnlySheetId(null);
  chrome.setOpenSplit(null);
}

/** Focusing an existing compare tab preserves grid-only and split state. */
export function focusCompareTabTransition(
  workView: WorkViewHandle,
  compareView: CompareViewHandle,
  kind: CompareKind,
): void {
  compareView.focus(kind);
  workView.setActivePromotedKey(null);
}

export function toggleProvenanceWithDrawersTransition(
  stores: Pick<WorkspaceStores, 'detail' | 'chrome'>,
): void {
  stores.detail.closeDrawers();
  stores.chrome.toggleProvenanceOpen();
}


/** Commit the row route before hydrating drawer state. */
export function openRowPanelTransition(
  stores: Pick<WorkspaceStores, 'detail' | 'selection'>,
  writeRoute: (next: RouteState) => void,
  base: RouteState,
  row: Row,
  col?: ColumnDef | null,
  preview?: PreviewCellDetail | null,
): void {
  writeRoute({ ...base, panel: { kind: 'row', rowId: row.id, columnId: col?.id } });
  stores.detail.openRow(row, preview);
  stores.selection.setSelectedColumnId(col?.id ?? null);
}

/** Commit the column route before hydrating drawer state. */
export function openColumnPanelTransition(
  stores: Pick<WorkspaceStores, 'detail' | 'selection'>,
  writeRoute: (next: RouteState) => void,
  base: RouteState,
  col: ColumnDef,
): void {
  writeRoute({ ...base, panel: { kind: 'column', columnId: col.id } });
  stores.detail.openColumn(col);
  stores.selection.setSelectedColumnId(null);
}

/** Cross-sheet row links must commit the target route before hydrating selection. */
export function openRowRefTransition(
  stores: Pick<WorkspaceStores, 'selection'>,
  writeRoute: (next: RouteState) => void,
  projectId: string,
  sheetId: number | string,
  rowId: number | string,
): void {
  writeRoute({
    projectId,
    sheetId: String(sheetId),
    actionKind: null,
    review: false,
    panel: { kind: 'row', rowId: String(rowId) },
  });
  stores.selection.setSelectedRows({ sheetId: String(sheetId), rowIds: [String(rowId)], rowIndexes: [] });
}

export function closeRoutePanelTransition(
  stores: Pick<WorkspaceStores, 'detail' | 'selection'>,
  writeRoute: (next: RouteState) => void,
  base: RouteState,
): void {
  stores.detail.closeAll();
  stores.selection.setSelectedColumnId(null);
  writeRoute(base);
}

export function confirmDeleteRowsTransition(
  stores: Pick<WorkspaceStores, 'selection' | 'detail'>,
  sheetId: string,
): void {
  stores.selection.clearRowSelection(sheetId);
  stores.detail.closeDrawers({ clearChildFilter: true });
}

/** Callers must bump dataVersion immediately after this transition. */
export function applyLensTransition(
  stores: Pick<WorkspaceStores, 'gridView' | 'detail' | 'lensView'>,
  lensView: LensGridView,
): void {
  stores.gridView.resetForLensEntry();
  stores.detail.closeHeaderMenu();
  stores.lensView.setLensOpenError(null);
  stores.lensView.setLensView(lensView);
}
