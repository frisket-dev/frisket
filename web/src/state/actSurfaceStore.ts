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
import type { ImportCsvPreview } from '../api/onboardingImports';

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
   *  generic Import entry points (ribbon, ⌘K, Copilot handoff, AddSheetButton),
   *  which must keep preserving whatever mode the user last had selected
   *  inside the dialog rather than forcing one. Cleared on close so it never
   *  leaks into a later generic open. */
  importDialogEntryMode: ImportMode | null;
  /** A CSV dropped on the empty-project surface. The drop surface performs
   *  the non-mutating read, then hands its File and preview to the global
   *  dialog, which remains the sole owner of confirmation. */
  importDialogCsv: { file: File; preview: ImportCsvPreview } | null;
  /** Files dropped on the empty-project surface that need the dialog's bulk
   *  planner. The dialog consumes this handoff once per open session. */
  importDialogFiles: File[] | null;
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
    importDialogCsv: null,
    importDialogFiles: null,
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
  openCsvImport(file: File, preview: ImportCsvPreview): void;
  openBulkImport(files: File[]): void;
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
        ? (s.importDialogOpen ? s : { ...s, importDialogOpen: true })
        : {
          ...s,
          importDialogOpen: false,
          importDialogEntryMode: null,
          importDialogCsv: null,
          importDialogFiles: null,
        });
    },
    openImportDialog(entryMode) {
      const nextEntryMode = entryMode ?? null;
      store.set((s) =>
        s.importDialogOpen
        && s.importDialogEntryMode === nextEntryMode
        && s.importDialogCsv === null
        && s.importDialogFiles === null
          ? s
          : {
            ...s,
            importDialogOpen: true,
            importDialogEntryMode: nextEntryMode,
            importDialogCsv: null,
            importDialogFiles: null,
          },
      );
    },
    openCsvImport(file, preview) {
      store.set((s) => ({
        ...s,
        importDialogOpen: true,
        importDialogEntryMode: 'csv',
        importDialogCsv: { file, preview },
        importDialogFiles: null,
      }));
    },
    openBulkImport(files) {
      store.set((s) => ({
        ...s,
        importDialogOpen: true,
        importDialogEntryMode: 'files',
        importDialogCsv: null,
        importDialogFiles: [...files],
      }));
    },
    closeImportDialog() {
      store.set((s) =>
        s.importDialogOpen || s.importDialogCsv !== null || s.importDialogFiles !== null
          ? {
            ...s,
            importDialogOpen: false,
            importDialogEntryMode: null,
            importDialogCsv: null,
            importDialogFiles: null,
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
