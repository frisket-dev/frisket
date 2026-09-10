// Hydrate synchronously from localStorage to avoid a one-render flash; bind-layer wrappers own writes.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import type { WorkbenchProjectionStatus } from '../workbench/WorkbenchBottomDock';
import type { ProjectInfo } from '../api/types';
import {
  createScopedChromePreferenceOwner,
  type HydratedChromePreferences,
} from './workspaceResources';
export { chromeStorageKeys } from './workspaceResources';
import { navigate } from '../routes';
import { writeSettingsProjectContext } from '../settings/settingsProjectContext';

const BOTTOM_DOCK_FALLBACK_TAB_PLACEMENT_ID = 'jobs';

export type EvidenceViewerHost = 'modalOrPeek' | 'mainView';

export type RibbonMode = 'ribbon' | 'menu';

export type DiscoverTab =
  | 'Facets'
  | 'Mentions'
  | 'Sources'
  | 'Views'
  | 'Watches'
  | 'Embeddings'
  | 'Notifications';

export const DISCOVER_TABS: readonly DiscoverTab[] = [
  'Facets',
  'Mentions',
  'Sources',
  'Views',
  'Watches',
  'Embeddings',
  'Notifications',
];

export type WorkViewKind = 'grid' | 'map' | 'gallery' | 'graph' | 'document' | 'answers';

export type DocumentViewLayout = 'continuous' | 'single' | 'two-up';
export type DocumentViewFit = 'width' | 'page';
export type DocumentViewVideoFit = 'full' | 'fit-height';

export interface DocumentViewState {
  sheetId: string;
  sourceColumnId: string | null;
  titleColumnId: string | null;
  layout: DocumentViewLayout;
  fit: DocumentViewFit;
  videoFit: DocumentViewVideoFit;
  textLayer: boolean;
  sync: boolean;
  activeRowId: string | null;
}

/** Store disabled layers so newly discovered layers remain visible by default. */
export type DocumentAnnotationPreferences = Record<string, string[]>;

export interface PromotedView {
  key: string;
  sheetId: string;
  kind: Exclude<WorkViewKind, 'grid' | 'answers'>;
  columnId?: string;
  label: string;
}

export interface OpenSplitState {
  kind: 'map' | 'graph';
  sheetId: string;
  columnId?: string;
}

export interface EvidenceViewerState {
  linkId: string | number;
  host: EvidenceViewerHost;
}

export interface DeleteRowsConfirmState {
  sheetId: string;
  rowIds: string[];
}

export interface WorkspaceToastError {
  message: string;
  code?: string;
  details?: Record<string, unknown>;
}

export interface ChromeState {
  // Session-only; /action/{kind} is the durable reopen path.
  actionPanelOpen: boolean;
  activeBottomDockTab: string;
  projectionStatus: WorkbenchProjectionStatus | null;
  commandPaletteOpen: boolean;
  commandPaletteQuery: string;
  copilotPopoverOpen: boolean;
  lastCommandAction: string;
  deleteRowsConfirm: DeleteRowsConfirmState | null;
  error: WorkspaceToastError | null;
  evidenceViewerState: EvidenceViewerState | null;
  ribbonMode: RibbonMode;
  activeRibbonTab: string;
  discoverOpen: boolean;
  // Plugin contributions make this an open identifier set.
  discoverTab: string;
  promotedViews: PromotedView[];
  openSplit: OpenSplitState | null;
  documentView: DocumentViewState | null;
  documentAnnotationPreferences: DocumentAnnotationPreferences;
  provenanceOpen: boolean;
  overflowMenuOpen: boolean;
}

export function createChromeState(
  projectId: string,
  preferences: HydratedChromePreferences = createScopedChromePreferenceOwner(projectId).hydrate(),
): ChromeState {
  return {
    // Never restore session-only action-panel state from storage.
    actionPanelOpen: false,
    activeBottomDockTab: BOTTOM_DOCK_FALLBACK_TAB_PLACEMENT_ID,
    projectionStatus: null,
    commandPaletteOpen: false,
    commandPaletteQuery: '',
    copilotPopoverOpen: false,
    lastCommandAction: '',
    deleteRowsConfirm: null,
    error: null,
    evidenceViewerState: null,
    ribbonMode: preferences.ribbonMode,
    activeRibbonTab: preferences.activeRibbonTab,
    discoverOpen: preferences.discoverOpen,
    discoverTab: preferences.discoverTab,
    promotedViews: preferences.promotedViews,
    openSplit: preferences.openSplit,
    documentView: preferences.documentView,
    documentAnnotationPreferences: preferences.documentAnnotationPreferences,
    provenanceOpen: false,
    overflowMenuOpen: false,
  };
}

export function createChromeStore(
  projectId: string,
  preferences?: HydratedChromePreferences,
): {
  store: Store<ChromeState>;

  setActionPanelOpen(open: boolean): void;
  openActionPanel(): void;
  hideActionPanel(): void;
  toggleActionPanelOpen(): void;

  setActiveBottomDockTab(tab: string): void;
  setProjectionStatus(status: WorkbenchProjectionStatus | null): void;

  openCommandPalette(): void;
  closeCommandPalette(): void;
  setCommandPaletteQuery(query: string): void;
  closeCommandPaletteAndReset(): void;

  openCopilotPopover(): void;
  closeCopilotPopover(): void;
  toggleCopilotPopover(): void;

  recordCommandAction(label: string): void;

  setDeleteRowsConfirm(confirm: DeleteRowsConfirmState | null): void;
  clearDeleteRowsConfirm(): void;

  setError(error: WorkspaceToastError | null): void;

  openDiagnosePanel(project?: ProjectInfo | null): void;

  openEvidenceViewer(linkId: string | number, host?: EvidenceViewerHost): void;
  closeEvidenceViewer(): void;

  setRibbonMode(mode: RibbonMode): void;
  setActiveRibbonTab(tab: string): void;

  setDiscoverOpen(open: boolean): void;
  setDiscoverTab(tab: string): void;
  toggleDiscoverOpen(): void;

  setPromotedViews(views: PromotedView[]): void;
  setOpenSplit(split: OpenSplitState | null): void;
  setDocumentView(documentView: DocumentViewState | null): void;
  setDocumentAnnotationPreferences(prefs: DocumentAnnotationPreferences): void;

  toggleProvenanceOpen(): void;
  closeProvenanceOpen(): void;
  setOverflowMenuOpen(open: boolean): void;
} {
  const store = createStore<ChromeState>(createChromeState(projectId, preferences));

  return {
    store,

    setActionPanelOpen(open) {
      store.set((s) => (s.actionPanelOpen === open ? s : { ...s, actionPanelOpen: open }));
    },
    openActionPanel() {
      store.set((s) => (s.actionPanelOpen ? s : { ...s, actionPanelOpen: true }));
    },
    hideActionPanel() {
      store.set((s) => (s.actionPanelOpen ? { ...s, actionPanelOpen: false } : s));
    },
    toggleActionPanelOpen() {
      store.set((s) => ({ ...s, actionPanelOpen: !s.actionPanelOpen }));
    },

    setActiveBottomDockTab(tab) {
      store.set((s) => (s.activeBottomDockTab === tab ? s : { ...s, activeBottomDockTab: tab }));
    },
    setProjectionStatus(status) {
      store.set((s) => ({ ...s, projectionStatus: status }));
    },

    openCommandPalette() {
      store.set((s) => (s.commandPaletteOpen ? s : { ...s, commandPaletteOpen: true }));
    },
    closeCommandPalette() {
      store.set((s) => (s.commandPaletteOpen ? { ...s, commandPaletteOpen: false } : s));
    },
    setCommandPaletteQuery(query) {
      store.set((s) => (s.commandPaletteQuery === query ? s : { ...s, commandPaletteQuery: query }));
    },
    closeCommandPaletteAndReset() {
      store.set((s) => ({ ...s, commandPaletteQuery: '', commandPaletteOpen: false }));
    },

    openCopilotPopover() {
      store.set((s) => (s.copilotPopoverOpen ? s : { ...s, copilotPopoverOpen: true }));
    },
    closeCopilotPopover() {
      store.set((s) => (s.copilotPopoverOpen ? { ...s, copilotPopoverOpen: false } : s));
    },
    toggleCopilotPopover() {
      store.set((s) => ({ ...s, copilotPopoverOpen: !s.copilotPopoverOpen }));
    },

    recordCommandAction(label) {
      store.set((s) => ({ ...s, lastCommandAction: label }));
    },

    setDeleteRowsConfirm(confirm) {
      store.set((s) => ({ ...s, deleteRowsConfirm: confirm }));
    },
    clearDeleteRowsConfirm() {
      store.set((s) => (s.deleteRowsConfirm === null ? s : { ...s, deleteRowsConfirm: null }));
    },

    setError(error) {
      store.set((s) => ({ ...s, error }));
    },

    openDiagnosePanel(project) {
      if (project) writeSettingsProjectContext(project);
      navigate({ kind: 'settings', scope: 'personal', section: 'diagnostics' });
    },

    openEvidenceViewer(linkId, host = 'modalOrPeek') {
      store.set((s) => ({ ...s, evidenceViewerState: { linkId, host } }));
    },
    closeEvidenceViewer() {
      store.set((s) => (s.evidenceViewerState === null ? s : { ...s, evidenceViewerState: null }));
    },

    setRibbonMode(mode) {
      store.set((s) => (s.ribbonMode === mode ? s : { ...s, ribbonMode: mode }));
    },
    setActiveRibbonTab(tab) {
      store.set((s) => (s.activeRibbonTab === tab ? s : { ...s, activeRibbonTab: tab }));
    },

    setDiscoverOpen(open) {
      store.set((s) => (s.discoverOpen === open ? s : { ...s, discoverOpen: open }));
    },
    setDiscoverTab(tab) {
      store.set((s) => (s.discoverTab === tab ? s : { ...s, discoverTab: tab }));
    },
    toggleDiscoverOpen() {
      store.set((s) => ({ ...s, discoverOpen: !s.discoverOpen }));
    },

    setPromotedViews(views) {
      store.set((s) => ({ ...s, promotedViews: views }));
    },
    setOpenSplit(split) {
      store.set((s) => ({ ...s, openSplit: split }));
    },
    setDocumentView(documentView) {
      store.set((s) => ({ ...s, documentView }));
    },
    setDocumentAnnotationPreferences(documentAnnotationPreferences) {
      store.set((s) => ({ ...s, documentAnnotationPreferences }));
    },

    toggleProvenanceOpen() {
      store.set((s) => ({ ...s, provenanceOpen: !s.provenanceOpen }));
    },
    closeProvenanceOpen() {
      store.set((s) => (s.provenanceOpen ? { ...s, provenanceOpen: false } : s));
    },
    setOverflowMenuOpen(open) {
      store.set((s) => (s.overflowMenuOpen === open ? s : { ...s, overflowMenuOpen: open }));
    },
  };
}

type InternalChromeStoreHandle = ReturnType<typeof createChromeStore>;

export type ChromeStoreHandle = Omit<
  InternalChromeStoreHandle,
  | 'setRibbonMode'
  | 'setActiveRibbonTab'
  | 'setDiscoverOpen'
  | 'setDiscoverTab'
  | 'toggleDiscoverOpen'
  | 'setPromotedViews'
  | 'setOpenSplit'
  | 'setDocumentView'
  | 'setDocumentAnnotationPreferences'
>;
