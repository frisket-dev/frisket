// Per-project store factory. NEVER a module-level singleton: switching projects
// must not leak project A's store state into project B. bind/WorkspaceStoresProvider
// calls this exactly once per project mount (a memo hook keyed on projectId);
// its effect then calls activate()/deactivate() on the returned lease instead
// of constructing/disposing directly — see WorkspaceSessionLease below.

import { createGridViewStore, type GridViewStoreHandle } from './gridViewStore';
import { createJobStore, type JobStoreHandle } from './jobStore';
import { createRouteStore, type RouteStoreHandle } from './routeStore';
import { resetForRouteSheetChange } from './workspaceTransitions';
import { createRowCacheStore, type RowCacheStoreHandle } from './rowCacheStore';
import { createSelectionStore, type SelectionStoreHandle } from './selectionStore';
import { createDetailStore, type DetailStoreHandle } from './detailStore';
import { createSavedViewsStore, type SavedViewsStoreHandle } from './savedViewsStore';
import { createWorkViewStore, type WorkViewStoreHandle } from './workViewStore';
import {
  createCompareViewStore,
  type CompareViewStoreHandle,
} from './compareViewStore';
import { createLensViewStore, type LensViewStoreHandle } from './lensViewStore';
import { createPreviewViewStore, type PreviewViewStoreHandle } from './previewViewStore';
import {
  createWatchRunLinkStore,
  type WatchRunLinkStoreHandle,
} from './watchRunLinkStore';
import { createChromeStore, type ChromeStoreHandle } from './chromeStore';
import { createActSurfaceStore, type ActSurfaceStoreHandle } from './actSurfaceStore';
import {
  createPluginLayoutStore,
  type PluginAvailability,
  type PluginLayoutStoreHandle,
} from './pluginLayoutStore';
import { createAudioPlaybackStore, type AudioPlaybackStoreHandle } from './audioPlaybackStore';
import {
  createWorkspaceSessionLease,
  type WorkspaceSessionGeneration,
  type WorkspaceSessionLease,
} from './workspaceSessionLease';
import { createProjectApi } from '../api/real';
import type { ProjectApiPort, WorkbenchApiPort } from '../api/ports';
import {
  createActionCatalogResource,
  createProjectDataResource,
  createScopedChromePreferenceOwner,
  type ActionCatalogResource,
  type ProjectDataResource,
  type ProjectChromePreferenceCommands,
  type SheetChromePreferenceCommands,
} from './workspaceResources';

export interface WorkspaceStores {
  gridView: GridViewStoreHandle;
  /** Route projection. A projection, not a URL owner: fed by a
   *  bind/ effect from App's route props; useRoute() stays the sole URL reader.
   *  The RouteSyncController (bind/useRouteSyncController) subscribes to THIS
   *  store as the single workspace-route→history writer:
   *  every command write site mutates this store; the controller commits it.
   *  The store owns no long-lived subscription of its own; the controller's
   *  subscription is disposed by its own bind effect on unmount, so this store
   *  still needs no work in dispose() below. check-substrate-boundaries.mjs
   *  enforces that the controller MUST be mounted
   *  and no direct navigate()/replaceRoute() workspace-route write may survive. */
  route: RouteStoreHandle;
  /** Project job resource. Publishes run/actionJobs/costGate/collision while
   *  privately owning the run, queued, and active-dock lanes. The thin bind
   *  composer plus direct region handles consume it; no in-flight launch,
   *  request, or lane survives a project switch. */
  job: JobStoreHandle;
  /** The paged row-cache registry, keyed by sheetId. One slot per
   *  sheet, reused across a SheetGrid remount instead of restarting; the
   *  toolbar reads the same slot directly (bind/useRowCacheHandle) instead
   *  of SheetGrid pushing rowCount up via an effect. */
  rowCache: RowCacheStoreHandle;
  /** Grid row/column selection. */
  selection: SelectionStoreHandle;
  /** Row/column drawers, header menu, child filter, proposal inspect. */
  detail: DetailStoreHandle;
  /** Saved views list + name/editor. */
  savedViews: SavedViewsStoreHandle;
  /** Work-view-switcher session state. Structurally unpersistable/unroutable
   *  by design — see workViewStore.ts's header. */
  workView: WorkViewStoreHandle;
  /** The four closed first-party compare tabs and their in-memory sessions. */
  compareView: CompareViewStoreHandle;
  /** In-memory saved-lens grid view and its last open error. */
  lensView: LensViewStoreHandle;
  /** In-memory action preview: the small server-sampled preview a
   *  running/done/error banner tracks while open, overlaid onto the grid via
   *  the lens row-id seam. Same "fixed sheet-change reset class" as lensView
   *  (not survive-by-omission like gridView's inlineFilter) — its
   *  resetForSheetChange() is composed at the SAME resetForRouteSheetChange
   *  site as workView's, below. It also owns the poll/generation/teardown
   *  lifecycle; workspace/useWorkspaceModel.tsx supplies its call-time
   *  collaborators through bind/usePreviewViewHandle.ts. */
  previewView: PreviewViewStoreHandle;
  /** In-memory watch/notification handoff; deliberately survives sheet changes. */
  watchRunLink: WatchRunLinkStoreHandle;
  /** Chrome: action panel/command palette/copilot popover/discover/ribbon/
   *  promoted views/open split/document view + the former
   *  workspaceUiReducer remnants. Constructed WITH
   *  projectId — its initial snapshot is synchronously hydrated from
   *  project-keyed localStorage (chromeStore.ts's header explains why). */
  chrome: ChromeStoreHandle;
  /** Act-surface: export targets/modal, the configure-first launch seam,
   *  import dialog, actions-library modal, add-column prompt. Explicitly does
   *  not own the action catalog or run lifecycle. */
  actSurface: ActSurfaceStoreHandle;
  /** The last-fetched plugin runtime index plus its request, fixed poll,
   *  visibility catch-up, generation, and teardown lifecycle. Every per-host
   *  descriptor list stays a bind-layer derivation of its one published field. */
  pluginLayout: PluginLayoutStoreHandle;
  /** One app-level audio session shared by grid and row detail controls. */
  audioPlayback: AudioPlaybackStoreHandle;
  /** Immutable API scoped to this workspace project. */
  projectApi: ProjectApiPort;
  /** Scope-correct owning commands for the eight persisted chrome fields. */
  chromePreferences: {
    readonly projectId: string;
    readonly project: ProjectChromePreferenceCommands;
    readonly sheet: SheetChromePreferenceCommands;
  };
  /** Project data plus the current sheet ids used for route normalization. */
  projectData: ProjectDataResource;
  /** The single accepted action-catalog snapshot and its fetch lifecycle. */
  actionCatalog: ActionCatalogResource;
  /** Owns workspace activation generation and ordered disposal. */
  lease: WorkspaceSessionLease;
  // WEB-01 requirement 5 names, but does not build, three more immutable
  // project-scoped ports this substrate will host: scoped browser preference
  // adapters (WEB-02 builds the typed codec/version machinery; they attach
  // here, alongside chrome/gridView's existing persisted fields), same-project
  // navigation (WEB-02's projectExternal/navigate/normalize triad, attaching
  // beside `route`/RouteSyncController above), and a clock port (no
  // time-mocking machinery is added by this card — the seam is this object,
  // not a new field, since nothing here needs one yet). Characterization
  // only; none of the three is constructed by WEB-01.
  /** Disposes every controller/subscription this instance owns. gridView,
   *  rowCache, selection, detail, savedViews, workView, compareView, lensView,
   *  watchRunLink, chrome, and actSurface are plain state containers with no
   *  long-lived subscriptions of their own, so their share of this is a no-op;
   *  pluginLayout.dispose() clears its timer/visibility listener and invalidates
   *  its pending runtime-index generation;
   *  actionCatalog.dispose() unsubscribes invalidation and invalidates its
   *  pending request, projectData.dispose() invalidates pending refreshes,
   *  previewView.dispose() clears its timer and invalidates its generation,
   *  and job.dispose() aborts/fences its in-flight action launch before
   *  retiring all owned lanes and requests — no async result from project A's
   *  instance may still commit once project B's is constructed. Called by
   *  lease.deactivate(), not directly, once WorkspaceStoresProvider's effect
   *  is wired to the lease — kept as a named method (rather than folded away)
   *  because it remains the store-topology-owned disposal step the lease's
   *  deactivate() delegates to. */
  dispose(): void;
}

export function createWorkspaceStores(
  projectId: string,
  projectApi: ProjectApiPort = createProjectApi(projectId),
  pluginsAvailable?: PluginAvailability,
): WorkspaceStores {
  // Immutable domain ports shared by this project session. Construction
  // performs no I/O; each resource owns its own start/refresh lifecycle.
  const workbenchApi: WorkbenchApiPort = projectApi;
  // gridView is not project-scoped persistence (its localStorage keys
  // are written by the bind-layer caller, not the store — see
  // gridViewStore.ts), so projectId is unused here.
  const gridView = createGridViewStore();
  // job is not project-scoped persistence either (run/poll state is
  // intentionally NOT carried across a project switch — a new project starts
  // with no in-flight run, matching today's behavior where switching projects
  // remounts Workspace and discards the prior job resource/session state).
  const job = createJobStore(projectId, projectApi);
  // rowCache is NOT project-scoped persistence (it's an in-memory paging
  // cache, discarded on project switch same as job's run state) — it IS
  // project-SCOPED ownership though (a fresh Map per project), which is the
  // whole reason it lives here instead of a module-level singleton: sheet
  // ids repeat across projects (SheetGrid.tsx's widthsKey comment), so two
  // projects sharing a sheet id must never share a cache slot.
  const rowCache = createRowCacheStore();
  // selection/detail/savedViews/workView/compareView/lensView/previewView/watchRunLink are not project-scoped
  // persistence either — every field they own is either pure in-memory
  // session state (selection, detail's drawers, workView, compareView, lensView, previewView,
  // watchRunLink) or a
  // plain component-state list re-fetched per sheet (savedViews' `views`,
  // from api.listViews on the active-sheet transition), matching gridView/job's
  // precedent above: projectId is accepted but unused by any of these eight.
  const selection = createSelectionStore();
  const detail = createDetailStore();
  const savedViews = createSavedViewsStore();
  const workView = createWorkViewStore();
  const compareView = createCompareViewStore();
  const lensView = createLensViewStore();
  const previewView = createPreviewViewStore(projectApi);
  const watchRunLink = createWatchRunLinkStore();
  // The route owner is assembled after its reset participants so its private
  // applyRoute can invoke the fixed cross-store matrix synchronously.
  const route = createRouteStore(projectId, {
    resetForSheetChange: (sheetId) =>
      resetForRouteSheetChange(
        { gridView, selection, detail, workView, lensView, previewView },
        sheetId,
      ),
  });
  // chrome IS project-scoped persistence (ribbonMode/activeRibbonTab/
  // discoverOpen/discoverTab/promotedViews/openSplit/documentView all read
  // project-keyed localStorage at construction) — same pattern as routeStore
  // above, chromeStore.ts's header explains why the READ side stays here
  // while the WRITE side moves to bind/.
  const chromePreferenceOwner = createScopedChromePreferenceOwner(projectId);
  const chrome = createChromeStore(projectId, chromePreferenceOwner.hydrate());
  const chromePreferenceCommands = chromePreferenceOwner.bindCommands({
    getDocumentAnnotationPreferences: () => chrome.store.get().documentAnnotationPreferences,
    setRibbonMode: chrome.setRibbonMode,
    setActiveRibbonTab: chrome.setActiveRibbonTab,
    setDiscoverOpen: chrome.setDiscoverOpen,
    setDiscoverTab: chrome.setDiscoverTab,
    setPromotedViews: chrome.setPromotedViews,
    setOpenSplit: chrome.setOpenSplit,
    setDocumentView: chrome.setDocumentView,
    setDocumentAnnotationPreferences: chrome.setDocumentAnnotationPreferences,
  });
  const chromePreferences = {
    projectId,
    ...chromePreferenceCommands,
  };
  const projectData = createProjectDataResource(projectApi);
  // actSurface/pluginLayout are not project-scoped persistence — every field
  // they own is either re-fetched API data (the plugin runtime index) or pure
  // in-memory launch-seam state.
  const actSurface = createActSurfaceStore();
  const actionCatalog = createActionCatalogResource(projectId, projectApi, actSurface);
  const pluginLayout = createPluginLayoutStore(workbenchApi, pluginsAvailable);
  const audioPlayback = createAudioPlaybackStore();
  function dispose(): void {
    pluginLayout.dispose();
    actionCatalog.dispose();
    projectData.dispose();
    previewView.dispose();
    job.dispose();
  }
  // Lease construction closes over the local dispose/projectId functions
  // rather than over the returned `stores` object — no self-reference needed.
  const lease = createWorkspaceSessionLease({
    disposeJobLanes: dispose,
  });

  return {
    gridView,
    route,
    job,
    rowCache,
    selection,
    detail,
    savedViews,
    workView,
    compareView,
    lensView,
    previewView,
    watchRunLink,
    chrome,
    actSurface,
    pluginLayout,
    audioPlayback,
    projectApi,
    chromePreferences,
    projectData,
    actionCatalog,
    lease,
    dispose,
  };
}

export type { WorkspaceSessionGeneration, WorkspaceSessionLease };
