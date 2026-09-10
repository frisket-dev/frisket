// The chrome hook's React-facing seam. workspace/useWorkspaceChromeState.ts
// re-exports this file's `useWorkspaceChromeState` (plus every chrome type)
// unchanged — other files still import chrome types from that path, so its
// public API (hook signature/return shape and every exported type/const) must
// stay byte-identical to what this file defines.
//
// PERSISTENCE SPLIT (chromeStore.ts's header explains the design in full): the
// store's constructor already does the project-keyed localStorage READS
// synchronously (so first render never flashes default state); this file owns
// the WRITE side — every setter below that used to call
// localStorage.setItem/removeItem alongside its dispatch does the exact same
// write here, now calling the store method instead of dispatch. showError's 8s
// auto-clear timer (a React-lifecycle-tied ref, not persistence) also stays
// here — it cannot live in a plain state/ object.
//
// clusterPanelOpen/provenanceOpen/overflowMenuOpen are NOT part of this hook's
// return shape (kept byte-identical) — workspace/useWorkspaceModel.tsx
// reads/writes them directly off useChromeHandle() instead (the same "a second
// direct entry point into the same per-project store" pattern gridView already
// uses — useGridViewHandle() coexists with direct useSelector(gridView.store, …)
// reads in that same file).

import { useCallback, useEffect, useRef } from 'react';
import { ApiError } from '../api/open';
import { useChromeHandle } from './useChromeHandle';
import { useWorkspaceStores } from './useWorkspaceStores';
import { useSelector } from './useSelector';
import {
  type DeleteRowsConfirmState,
  type DocumentViewState,
  type EvidenceViewerHost,
  type PromotedView,
  type OpenSplitState,
  type RibbonMode,
  type WorkspaceToastError,
} from '../state/chromeStore';
import type { WorkbenchProjectionStatus } from '../workbench/WorkbenchBottomDock';

export function useWorkspaceChromeState(projectId: string) {
  const chrome = useChromeHandle();
  const { chromePreferences } = useWorkspaceStores();
  if (chromePreferences.projectId !== projectId) {
    throw new Error('workspace chrome preference scope does not match the mounted project');
  }
  const projectPreferences = chromePreferences.project;
  const sheetPreferences = chromePreferences.sheet;
  const errorTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (errorTimer.current) clearTimeout(errorTimer.current);
  }, []);

  const actionPanelOpen = useSelector(chrome.store, (s) => s.actionPanelOpen);
  const activeBottomDockTab = useSelector(chrome.store, (s) => s.activeBottomDockTab);
  const projectionStatus = useSelector(chrome.store, (s) => s.projectionStatus);
  const commandPaletteOpen = useSelector(chrome.store, (s) => s.commandPaletteOpen);
  const copilotPopoverOpen = useSelector(chrome.store, (s) => s.copilotPopoverOpen);
  const lastCommandAction = useSelector(chrome.store, (s) => s.lastCommandAction);
  const deleteRowsConfirm = useSelector(chrome.store, (s) => s.deleteRowsConfirm);
  const error = useSelector(chrome.store, (s) => s.error);
  const evidenceViewerState = useSelector(chrome.store, (s) => s.evidenceViewerState);
  const ribbonMode = useSelector(chrome.store, (s) => s.ribbonMode);
  const activeRibbonTab = useSelector(chrome.store, (s) => s.activeRibbonTab);
  const discoverOpen = useSelector(chrome.store, (s) => s.discoverOpen);
  const discoverTab = useSelector(chrome.store, (s) => s.discoverTab);
  const promotedViews = useSelector(chrome.store, (s) => s.promotedViews);
  const openSplit = useSelector(chrome.store, (s) => s.openSplit);
  const documentView = useSelector(chrome.store, (s) => s.documentView);
  const documentAnnotationPreferences = useSelector(
    chrome.store,
    (s) => s.documentAnnotationPreferences,
  );

  const openActionPanel = useCallback(() => chrome.openActionPanel(), [chrome]);
  const hideActionPanel = useCallback(() => chrome.hideActionPanel(), [chrome]);
  const toggleActionPanelOpen = useCallback(() => chrome.toggleActionPanelOpen(), [chrome]);

  const setActiveBottomDockTab = useCallback(
    (tab: string) => chrome.setActiveBottomDockTab(tab),
    [chrome],
  );
  const setProjectionStatus = useCallback(
    (status: WorkbenchProjectionStatus | null) => chrome.setProjectionStatus(status),
    [chrome],
  );

  const openCommandPalette = useCallback(() => chrome.openCommandPalette(), [chrome]);
  const closeCommandPalette = useCallback(() => chrome.closeCommandPalette(), [chrome]);

  const openCopilotPopover = useCallback(() => chrome.openCopilotPopover(), [chrome]);
  const closeCopilotPopover = useCallback(() => chrome.closeCopilotPopover(), [chrome]);
  const toggleCopilotPopover = useCallback(() => chrome.toggleCopilotPopover(), [chrome]);

  const recordCommandAction = useCallback(
    (label: string) => chrome.recordCommandAction(label),
    [chrome],
  );

  const setDeleteRowsConfirm = useCallback(
    (confirm: DeleteRowsConfirmState | null) => chrome.setDeleteRowsConfirm(confirm),
    [chrome],
  );
  const clearDeleteRowsConfirm = useCallback(() => chrome.clearDeleteRowsConfirm(), [chrome]);

  const showError = useCallback((input: string | Error) => {
    // The api layer (v1ErrorMessage / firstNonEmptyString) guarantees every
    // surfaced error carries a non-blank message, so this sink does not need
    // to re-implement blank-message compensation.
    const message = typeof input === 'string' ? input : input.message;
    const error: WorkspaceToastError =
      typeof input === 'string'
        ? { message }
        : {
            message,
            code: input instanceof ApiError ? input.code : undefined,
            details: input instanceof ApiError ? input.details : undefined,
          };
    chrome.setError(error);
    if (errorTimer.current) clearTimeout(errorTimer.current);
    errorTimer.current = setTimeout(() => {
      chrome.setError(null);
      errorTimer.current = null;
    }, 8000);
  }, [chrome]);

  const openEvidenceViewer = useCallback((
    linkId: string | number,
    host: EvidenceViewerHost = 'modalOrPeek',
  ) => {
    chrome.openEvidenceViewer(linkId, host);
  }, [chrome]);

  const closeEvidenceViewer = useCallback(() => chrome.closeEvidenceViewer(), [chrome]);

  const setRibbonMode = useCallback(
    (mode: RibbonMode) => projectPreferences.setRibbonMode(mode),
    [projectPreferences],
  );
  const setActiveRibbonTab = useCallback(
    (tab: string) => projectPreferences.setActiveRibbonTab(tab),
    [projectPreferences],
  );
  const setDiscoverOpen = useCallback(
    (open: boolean) => projectPreferences.setDiscoverOpen(open),
    [projectPreferences],
  );
  const setDiscoverTab = useCallback(
    (tab: string) => projectPreferences.setDiscoverTab(tab),
    [projectPreferences],
  );
  const setPromotedViews = useCallback(
    (views: PromotedView[]) => projectPreferences.setPromotedViews(views),
    [projectPreferences],
  );
  const setOpenSplit = useCallback(
    (split: OpenSplitState | null) => sheetPreferences.setOpenSplit(split),
    [sheetPreferences],
  );
  const setDocumentView = useCallback(
    (documentView: DocumentViewState | null) => sheetPreferences.setDocumentView(documentView),
    [sheetPreferences],
  );
  const setSheetAnnotationToggles = useCallback(
    (sheetId: string, disabledToggleKeys: readonly string[]) =>
      sheetPreferences.setSheetAnnotationToggles(sheetId, disabledToggleKeys),
    [sheetPreferences],
  );

  const toggleDiscover = useCallback(() => {
    setDiscoverOpen(!discoverOpen);
  }, [setDiscoverOpen, discoverOpen]);

  /** Open the Discover panel to a specific tab (used by the rail icons and the
   *  Sources/Facets action entry points). */
  const openDiscover = useCallback((tab: string) => {
    setDiscoverTab(tab);
    setDiscoverOpen(true);
  }, [setDiscoverTab, setDiscoverOpen]);

  return {
    actionPanelOpen,
    activeBottomDockTab,
    projectionStatus,
    commandPaletteOpen,
    copilotPopoverOpen,
    lastCommandAction,
    deleteRowsConfirm,
    error,
    evidenceViewerState,
    ribbonMode,
    activeRibbonTab,
    discoverOpen,
    discoverTab,
    promotedViews,
    openSplit,
    documentView,
    documentAnnotationPreferences,
    setSheetAnnotationToggles,
    setRibbonMode,
    setActiveRibbonTab,
    setDiscoverOpen,
    setDiscoverTab,
    setPromotedViews,
    setOpenSplit,
    setDocumentView,
    toggleDiscover,
    openDiscover,
    clearDeleteRowsConfirm,
    closeCommandPalette,
    openCopilotPopover,
    closeCopilotPopover,
    toggleCopilotPopover,
    closeEvidenceViewer,
    hideActionPanel,
    openActionPanel,
    openCommandPalette,
    openEvidenceViewer,
    recordCommandAction,
    setActiveBottomDockTab,
    setDeleteRowsConfirm,
    setProjectionStatus,
    showError,
    toggleActionPanelOpen,
  };
}

/** Narrow feature-facing command hook: no raw chrome mutation surface. */
export function useWorkspaceChromeCommands() {
  const { chromePreferences } = useWorkspaceStores();
  const projectPreferences = chromePreferences.project;
  const openDiscover = useCallback(
    (tab: string) => {
      projectPreferences.setDiscoverTab(tab);
      projectPreferences.setDiscoverOpen(true);
    },
    [projectPreferences],
  );
  return { openDiscover };
}
