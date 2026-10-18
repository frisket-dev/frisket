// Does NOT own run lifecycle (run/actionJobs/costGate) — jobStore already owns
// that; this store owns only the launch-surface state (the Act navigation/palette
// configure-first seam and its modals). `actRibbonTabs` is a DERIVED
// navigation model computed at the bind layer via resolveRibbonTabs
// (workbench/actSurface.ts) — it is not stored state. ActionCatalogResource
// owns the catalog lifecycle; this store keeps only the independently-consumed
// export-target projection.
//
// createActSurfaceStore() is per-project, assembled by
// state/createWorkspaceStores.ts — never a module-level singleton.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import type { ActionLaunchPrefill } from '../actions/actionFormInitial';
import type { ExportTarget } from '../exportTargets';
import type { ExportModalKind } from '../components/TopNav';
import type { ImportMode } from '../components/importWorkspace/model';

export interface ActionLaunch {
  kind: string;
  /** Browser-only form prefill, distinct from the API request envelope. */
  initial?: ActionLaunchPrefill;
}

export interface AddColumnPrompt {
  /** 1-based schema ordinal to insert BEFORE (null = append at the end). */
  position: number | null;
  anchor: { x: number; y: number };
  /** Insert-beside only: the caret-menu column the user anchored on. The
   * display columnOrder pins names, and a column it doesn't name renders at
   * the far right regardless of schema position — submit splices the new
   * name beside this anchor so the column lands where the user aimed. */
  anchorName?: string;
  side?: 'left' | 'right';
}

export interface ActSurfaceState {
  actExportTargets: ExportTarget[];
  /** Export modal opened from an Act-surface export command — same modals the
   *  project ▾ menu owns. */
  actExportModal: ExportModalKind | null;
  /** The action and optional form prefill an Act-surface launch carried, forwarded to the
   *  ActionPanel as configure-first pre-binding (never a run). The bind layer
   *  only exposes it when it matches the current route's action kind (a stale
   *  launch can never pre-bind a different action opened later). */
  actionLaunch: ActionLaunch | null;
  /** Monotonic explicit-launch identity. Repeated clicks of the same visible
   * action are still new launches and must reset their draft atomically. */
  actionLaunchId: number;
  importDialogOpen: boolean;
  /** The entry-point mode an `openImportDialog(mode)` caller requested — e.g.
   *  the Sources panel's "Add source" forcing the dialog onto its Feed step
   *  (sources are for feeds). `null` for the
   *  generic Import entry points (ribbon, ⌘K, Ask handoff, AddSheetButton),
   *  which must keep preserving whatever mode the user last had selected
   *  inside the dialog rather than forcing one. Cleared on close so it never
   *  leaks into a later generic open. */
  importDialogEntryMode: ImportMode | null;
  /** A table file dropped on the empty-project surface. The dialog stages it
   *  through the same server reader used by the import. */
  importDialogTableFile: File | null;
  /** Retained committed workbook import ref reopened to import another sheet. */
  importDialogTableRerunDraftId: string | null;
  /** Files dropped on the empty-project surface that need the dialog's bulk
   *  planner. The dialog consumes this handoff once per open session. */
  importDialogFiles: File[] | null;
  /** Search-preparation batch reopened in the existing import workspace. */
  importPreparationBatchId: string | null;
  /** Session-only home for managing recurring sources/connections. */
  sourcesConnectionsOpen: boolean;
  addColumnPrompt: AddColumnPrompt | null;
}

export function createActSurfaceState(): ActSurfaceState {
  return {
    actExportTargets: [],
    actExportModal: null,
    actionLaunch: null,
    actionLaunchId: 0,
    importDialogOpen: false,
    importDialogEntryMode: null,
    importDialogTableFile: null,
    importDialogTableRerunDraftId: null,
    importDialogFiles: null,
    importPreparationBatchId: null,
    sourcesConnectionsOpen: false,
    addColumnPrompt: null,
  };
}

export function createActSurfaceStore(): {
  store: Store<ActSurfaceState>;

  setActExportTargets(targets: ExportTarget[]): void;

  setActExportModal(kind: ExportModalKind | null): void;
  closeActExportModal(): void;

  setActionLaunch(launch: ActionLaunch | null): void;

  setImportDialogOpen(open: boolean): void;
  /** `entryMode` forces the dialog onto that step for this open only (e.g.
   *  Sources' "Add source" -> 'feed'); omitted, the dialog keeps whatever
   *  mode the user last had selected (the generic-entry-point behavior). */
  openImportDialog(entryMode?: ImportMode): void;
  openTableImport(file: File): void;
  openTableImportRerun(importRef: string): void;
  openBulkImport(files: File[]): void;
  openImportPreparation(batchId: string): void;
  closeImportDialog(): void;

  openSourcesConnections(): void;
  closeSourcesConnections(): void;

  setAddColumnPrompt(prompt: AddColumnPrompt | null): void;
  openAddColumnPromptAt(anchor: { x: number; y: number }): void;
  closeAddColumnPrompt(): void;
} {
  const store = createStore<ActSurfaceState>(createActSurfaceState());
  let nextLaunchId = 0;

  return {
    store,
    setActExportTargets(targets) {
      store.set((s) => ({ ...s, actExportTargets: targets }));
    },

    setActExportModal(kind) {
      store.set((s) => (s.actExportModal === kind ? s : { ...s, actExportModal: kind }));
    },
    closeActExportModal() {
      store.set((s) => (s.actExportModal === null ? s : { ...s, actExportModal: null }));
    },

    setActionLaunch(launch) {
      if (launch !== null) nextLaunchId += 1;
      store.set((s) => ({
        ...s,
        actionLaunch: launch,
        actionLaunchId: launch === null ? s.actionLaunchId : nextLaunchId,
      }));
    },

    setImportDialogOpen(open) {
      store.set((s) => open
        ? (s.importDialogOpen && s.importPreparationBatchId === null
          ? s
          : { ...s, importDialogOpen: true, importPreparationBatchId: null })
        : {
          ...s,
          importDialogOpen: false,
          importDialogEntryMode: null,
          importDialogTableFile: null,
          importDialogTableRerunDraftId: null,
          importDialogFiles: null,
          importPreparationBatchId: null,
        });
    },
    openImportDialog(entryMode) {
      const nextEntryMode = entryMode ?? null;
      store.set((s) =>
        s.importDialogOpen
        && s.importDialogEntryMode === nextEntryMode
        && s.importDialogTableFile === null
        && s.importDialogTableRerunDraftId === null
        && s.importDialogFiles === null
        && s.importPreparationBatchId === null
          ? s
          : {
            ...s,
            importDialogOpen: true,
            importDialogEntryMode: nextEntryMode,
            importDialogTableFile: null,
            importDialogTableRerunDraftId: null,
            importDialogFiles: null,
            importPreparationBatchId: null,
          },
      );
    },
    openTableImport(file) {
      store.set((s) => ({
        ...s,
        importDialogOpen: true,
        importDialogEntryMode: 'csv',
        importDialogTableFile: file,
        importDialogTableRerunDraftId: null,
        importDialogFiles: null,
        importPreparationBatchId: null,
      }));
    },
    openTableImportRerun(importRef) {
      store.set((s) => ({
        ...s,
        importDialogOpen: true,
        importDialogEntryMode: 'csv',
        importDialogTableFile: null,
        importDialogTableRerunDraftId: importRef,
        importDialogFiles: null,
        importPreparationBatchId: null,
      }));
    },
    openBulkImport(files) {
      store.set((s) => ({
        ...s,
        importDialogOpen: true,
        importDialogEntryMode: 'files',
        importDialogTableFile: null,
        importDialogTableRerunDraftId: null,
        importDialogFiles: files,
        importPreparationBatchId: null,
      }));
    },
    openImportPreparation(batchId) {
      store.set((s) => ({
        ...s,
        importDialogOpen: true,
        importDialogEntryMode: null,
        importDialogTableFile: null,
        importDialogTableRerunDraftId: null,
        importDialogFiles: null,
        importPreparationBatchId: batchId,
      }));
    },
    closeImportDialog() {
      store.set((s) =>
        s.importDialogOpen
        || s.importDialogTableFile !== null
        || s.importDialogTableRerunDraftId !== null
        || s.importDialogFiles !== null
        || s.importPreparationBatchId !== null
          ? {
            ...s,
            importDialogOpen: false,
            importDialogEntryMode: null,
            importDialogTableFile: null,
            importDialogTableRerunDraftId: null,
            importDialogFiles: null,
            importPreparationBatchId: null,
          }
          : s,
      );
    },

    openSourcesConnections() {
      store.set((s) =>
        s.sourcesConnectionsOpen ? s : { ...s, sourcesConnectionsOpen: true },
      );
    },
    closeSourcesConnections() {
      store.set((s) =>
        s.sourcesConnectionsOpen ? { ...s, sourcesConnectionsOpen: false } : s,
      );
    },

    setAddColumnPrompt(prompt) {
      store.set((s) => ({ ...s, addColumnPrompt: prompt }));
    },
    openAddColumnPromptAt(anchor) {
      store.set((s) => ({ ...s, addColumnPrompt: { position: null, anchor } }));
    },
    closeAddColumnPrompt() {
      store.set((s) => (s.addColumnPrompt === null ? s : { ...s, addColumnPrompt: null }));
    },
  };
}

export type ActSurfaceStoreHandle = ReturnType<typeof createActSurfaceStore>;
