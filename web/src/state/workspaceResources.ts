import type { RouteState } from '../core/route/RouteState';
import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';
import type { ProjectApiPort } from '../api/ports';
import { onActionCatalogInvalidated } from '../api/catalogEvents';
import type {
  ActionCatalogPayload,
  ActionTemplate,
  ColumnDef,
  HistoryState,
  SheetMeta,
} from '../api/open';
import { actionTemplatesFromCatalog } from '../actions/model';
import { exportTargetsFromCatalog } from '../exportTargets';
import { injectTestOnlyUnassignedAction } from '../workbench/actSurface';
import type { ActSurfaceStoreHandle } from './actSurfaceStore';
import type {
  DocumentAnnotationPreferences,
  DocumentViewFit,
  DocumentViewLayout,
  DocumentViewState,
  DocumentViewVideoFit,
  OpenSplitState,
  PromotedView,
  RibbonMode,
} from './chromeStore';

export type PreferenceDiagnosticKind =
  | 'missing'
  | 'malformed'
  | 'unsupported'
  | 'write_failed';

export interface PreferenceDiagnostic {
  scope: 'project' | 'sheet';
  field: string;
  key: string;
  version: 1;
  kind: PreferenceDiagnosticKind;
  error?: unknown;
}

export interface PreferenceStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface ScopedChromePreferenceOptions {
  storage?: PreferenceStorage;
  diagnostic?: (diagnostic: PreferenceDiagnostic) => void;
}

function ribbonModeStorageKey(projectId: string): string {
  return `frisket:ribbon-mode:${projectId}`;
}

function activeRibbonTabStorageKey(projectId: string): string {
  return `frisket:ribbon-tab:${projectId}`;
}

function discoverOpenStorageKey(projectId: string): string {
  return `frisket:discover-open:${projectId}`;
}

function discoverTabStorageKey(projectId: string): string {
  return `frisket:discover-tab:${projectId}`;
}

function promotedViewsStorageKey(projectId: string): string {
  return `frisket:promoted-views:${projectId}`;
}

function openSplitStorageKey(projectId: string): string {
  return `frisket:open-split:${projectId}`;
}

function documentViewStorageKey(projectId: string): string {
  return `frisket:document-view:${projectId}`;
}

function documentAnnotationPrefsStorageKey(projectId: string): string {
  return `frisket:document-annotations:${projectId}`;
}

/** Stable compatibility surface: WEB-02 preserves every key and encoding. */
export const chromeStorageKeys = {
  ribbonMode: ribbonModeStorageKey,
  activeRibbonTab: activeRibbonTabStorageKey,
  discoverOpen: discoverOpenStorageKey,
  discoverTab: discoverTabStorageKey,
  promotedViews: promotedViewsStorageKey,
  openSplit: openSplitStorageKey,
  documentView: documentViewStorageKey,
  documentAnnotationPrefs: documentAnnotationPrefsStorageKey,
};

export interface HydratedChromePreferences {
  ribbonMode: RibbonMode;
  activeRibbonTab: string;
  discoverOpen: boolean;
  discoverTab: string;
  promotedViews: PromotedView[];
  openSplit: OpenSplitState | null;
  documentView: DocumentViewState | null;
  documentAnnotationPreferences: DocumentAnnotationPreferences;
}

export interface ChromePreferenceMutationTarget {
  getDocumentAnnotationPreferences(): DocumentAnnotationPreferences;
  setRibbonMode(mode: RibbonMode): void;
  setActiveRibbonTab(tab: string): void;
  setDiscoverOpen(open: boolean): void;
  setDiscoverTab(tab: string): void;
  setPromotedViews(views: PromotedView[]): void;
  setOpenSplit(split: OpenSplitState | null): void;
  setDocumentView(documentView: DocumentViewState | null): void;
  setDocumentAnnotationPreferences(prefs: DocumentAnnotationPreferences): void;
}

export interface ProjectChromePreferenceCommands {
  setRibbonMode(mode: RibbonMode): void;
  setActiveRibbonTab(tab: string): void;
  setDiscoverOpen(open: boolean): void;
  setDiscoverTab(tab: string): void;
  setPromotedViews(views: PromotedView[]): void;
}

export interface SheetChromePreferenceCommands {
  setOpenSplit(split: OpenSplitState | null): void;
  setDocumentView(documentView: DocumentViewState | null): void;
  setSheetAnnotationToggles(sheetId: string, disabledToggleKeys: readonly string[]): void;
}

export interface ScopedChromePreferenceOwner {
  readonly projectId: string;
  hydrate(): HydratedChromePreferences;
  bindCommands(target: ChromePreferenceMutationTarget): {
    project: ProjectChromePreferenceCommands;
    sheet: SheetChromePreferenceCommands;
  };
}

function emitDefaultDiagnostic(diagnostic: PreferenceDiagnostic): void {
  console.warn('[frisket:workspace-preference]', diagnostic);
}

function readPreference<T>(
  storage: PreferenceStorage,
  diagnostic: (diagnostic: PreferenceDiagnostic) => void,
  scope: PreferenceDiagnostic['scope'],
  field: string,
  key: string,
  fallback: T,
  decode: (raw: string) => T | undefined,
): T {
  let raw: string | null;
  try {
    raw = storage.getItem(key);
  } catch (error) {
    diagnostic({ scope, field, key, version: 1, kind: 'malformed', error });
    return fallback;
  }
  if (raw === null) {
    diagnostic({ scope, field, key, version: 1, kind: 'missing' });
    return fallback;
  }
  try {
    const value = decode(raw);
    if (value !== undefined) return value;
    diagnostic({ scope, field, key, version: 1, kind: 'unsupported' });
  } catch (error) {
    diagnostic({ scope, field, key, version: 1, kind: 'malformed', error });
  }
  return fallback;
}

function parseJson(raw: string): unknown {
  return JSON.parse(raw) as unknown;
}

function decodePromotedViews(raw: string): PromotedView[] | undefined {
  const parsed = parseJson(raw);
  if (!Array.isArray(parsed)) return undefined;
  return parsed.filter(
    (value): value is PromotedView =>
      typeof value === 'object' &&
      value !== null &&
      typeof (value as PromotedView).key === 'string' &&
      typeof (value as PromotedView).sheetId === 'string' &&
      typeof (value as PromotedView).label === 'string' &&
      ((value as PromotedView).kind === 'map' ||
        (value as PromotedView).kind === 'gallery' ||
        (value as PromotedView).kind === 'graph'),
  );
}

function decodeOpenSplit(raw: string): OpenSplitState | null | undefined {
  const parsed = parseJson(raw);
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    ((parsed as OpenSplitState).kind !== 'map' && (parsed as OpenSplitState).kind !== 'graph') ||
    typeof (parsed as OpenSplitState).sheetId !== 'string'
  ) {
    return undefined;
  }
  return parsed as OpenSplitState;
}

const DOCUMENT_VIEW_LAYOUTS: readonly DocumentViewLayout[] = ['continuous', 'single', 'two-up'];
const DOCUMENT_VIEW_FITS: readonly DocumentViewFit[] = ['width', 'page'];
const DOCUMENT_VIEW_VIDEO_FITS: readonly DocumentViewVideoFit[] = ['full', 'fit-height'];

function decodeDocumentView(raw: string): DocumentViewState | null | undefined {
  const parsed = parseJson(raw) as Partial<DocumentViewState> | null;
  if (typeof parsed !== 'object' || parsed === null || typeof parsed.sheetId !== 'string') {
    return undefined;
  }
  return {
    sheetId: parsed.sheetId,
    sourceColumnId: typeof parsed.sourceColumnId === 'string' ? parsed.sourceColumnId : null,
    titleColumnId: typeof parsed.titleColumnId === 'string' ? parsed.titleColumnId : null,
    layout: DOCUMENT_VIEW_LAYOUTS.includes(parsed.layout as DocumentViewLayout)
      ? (parsed.layout as DocumentViewLayout)
      : 'continuous',
    fit: DOCUMENT_VIEW_FITS.includes(parsed.fit as DocumentViewFit)
      ? (parsed.fit as DocumentViewFit)
      : 'width',
    videoFit: DOCUMENT_VIEW_VIDEO_FITS.includes(parsed.videoFit as DocumentViewVideoFit)
      ? (parsed.videoFit as DocumentViewVideoFit)
      : 'full',
    textLayer: parsed.textLayer !== false,
    // Document focus is local browsing state, not an action row selection.
    // Always migrate the retired selection-sync preference off.
    sync: false,
    activeRowId: typeof parsed.activeRowId === 'string' ? parsed.activeRowId : null,
  };
}

const MAX_ANNOTATION_PREF_SHEETS = 200;
const MAX_DISABLED_TOGGLE_KEYS_PER_SHEET = 100;
const TOGGLE_KEY_SHAPE = /^\d+:\d+:[a-z][a-z0-9_.-]{0,63}$/;

function decodeDocumentAnnotationPreferences(
  raw: string,
): DocumentAnnotationPreferences | undefined {
  const parsed = parseJson(raw);
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return undefined;
  const preferences: DocumentAnnotationPreferences = {};
  for (const [sheetId, keys] of Object.entries(parsed).slice(0, MAX_ANNOTATION_PREF_SHEETS)) {
    if (!Array.isArray(keys)) continue;
    const valid = keys
      .filter((key): key is string => typeof key === 'string' && TOGGLE_KEY_SHAPE.test(key))
      .slice(0, MAX_DISABLED_TOGGLE_KEYS_PER_SHEET);
    if (valid.length > 0) preferences[sheetId] = valid;
  }
  return preferences;
}

function responsiveDiscoverDefault(): boolean {
  return typeof window !== 'undefined' ? window.innerWidth >= 1024 : true;
}

export function createScopedChromePreferenceOwner(
  projectId: string,
  options: ScopedChromePreferenceOptions = {},
): ScopedChromePreferenceOwner {
  const storage = options.storage ?? localStorage;
  const diagnostic = options.diagnostic ?? emitDefaultDiagnostic;

  function reportWriteFailure(
    scope: PreferenceDiagnostic['scope'],
    field: string,
    key: string,
    error: unknown,
  ): void {
    diagnostic({ scope, field, key, version: 1, kind: 'write_failed', error });
  }

  function setValue(
    scope: PreferenceDiagnostic['scope'],
    field: string,
    key: string,
    value: string,
  ): void {
    try {
      storage.setItem(key, value);
    } catch (error) {
      reportWriteFailure(scope, field, key, error);
    }
  }

  function removeValue(
    scope: PreferenceDiagnostic['scope'],
    field: string,
    key: string,
  ): void {
    try {
      storage.removeItem(key);
    } catch (error) {
      reportWriteFailure(scope, field, key, error);
    }
  }

  return {
    projectId,
    hydrate() {
      return {
        ribbonMode: readPreference(
          storage,
          diagnostic,
          'project',
          'ribbonMode',
          chromeStorageKeys.ribbonMode(projectId),
          'ribbon',
          (raw) => (raw === 'ribbon' || raw === 'menu' ? raw : undefined),
        ),
        activeRibbonTab: readPreference(
          storage,
          diagnostic,
          'project',
          'activeRibbonTab',
          chromeStorageKeys.activeRibbonTab(projectId),
          'analyze',
          (raw) => raw || undefined,
        ),
        discoverOpen: readPreference(
          storage,
          diagnostic,
          'project',
          'discoverOpen',
          chromeStorageKeys.discoverOpen(projectId),
          responsiveDiscoverDefault(),
          (raw) => (raw === '1' ? true : raw === '0' ? false : undefined),
        ),
        discoverTab: readPreference(
          storage,
          diagnostic,
          'project',
          'discoverTab',
          chromeStorageKeys.discoverTab(projectId),
          'Facets',
          (raw) => raw || undefined,
        ),
        promotedViews: readPreference(
          storage,
          diagnostic,
          'project',
          'promotedViews',
          chromeStorageKeys.promotedViews(projectId),
          [],
          decodePromotedViews,
        ),
        openSplit: readPreference(
          storage,
          diagnostic,
          'sheet',
          'openSplit',
          chromeStorageKeys.openSplit(projectId),
          null,
          decodeOpenSplit,
        ),
        documentView: readPreference(
          storage,
          diagnostic,
          'sheet',
          'documentView',
          chromeStorageKeys.documentView(projectId),
          null,
          decodeDocumentView,
        ),
        documentAnnotationPreferences: readPreference(
          storage,
          diagnostic,
          'sheet',
          'documentAnnotationPreferences',
          chromeStorageKeys.documentAnnotationPrefs(projectId),
          {},
          decodeDocumentAnnotationPreferences,
        ),
      };
    },
    bindCommands(target) {
      return {
        project: {
          setRibbonMode(mode) {
            setValue('project', 'ribbonMode', chromeStorageKeys.ribbonMode(projectId), mode);
            target.setRibbonMode(mode);
          },
          setActiveRibbonTab(tab) {
            setValue('project', 'activeRibbonTab', chromeStorageKeys.activeRibbonTab(projectId), tab);
            target.setActiveRibbonTab(tab);
          },
          setDiscoverOpen(open) {
            setValue('project', 'discoverOpen', chromeStorageKeys.discoverOpen(projectId), open ? '1' : '0');
            target.setDiscoverOpen(open);
          },
          setDiscoverTab(tab) {
            setValue('project', 'discoverTab', chromeStorageKeys.discoverTab(projectId), tab);
            target.setDiscoverTab(tab);
          },
          setPromotedViews(views) {
            setValue('project', 'promotedViews', chromeStorageKeys.promotedViews(projectId), JSON.stringify(views));
            target.setPromotedViews(views);
          },
        },
        sheet: {
          setOpenSplit(split) {
            const key = chromeStorageKeys.openSplit(projectId);
            if (split) setValue('sheet', 'openSplit', key, JSON.stringify(split));
            else removeValue('sheet', 'openSplit', key);
            target.setOpenSplit(split);
          },
          setDocumentView(documentView) {
            const key = chromeStorageKeys.documentView(projectId);
            if (documentView) setValue('sheet', 'documentView', key, JSON.stringify(documentView));
            else removeValue('sheet', 'documentView', key);
            target.setDocumentView(documentView);
          },
          setSheetAnnotationToggles(sheetId, disabledToggleKeys) {
            const next = { ...target.getDocumentAnnotationPreferences() };
            if (disabledToggleKeys.length > 0) next[sheetId] = [...disabledToggleKeys];
            else delete next[sheetId];
            setValue(
              'sheet',
              'documentAnnotationPreferences',
              chromeStorageKeys.documentAnnotationPrefs(projectId),
              JSON.stringify(next),
            );
            target.setDocumentAnnotationPreferences(next);
          },
        },
      };
    },
  };
}

interface ProjectDataState {
  sheets: SheetMeta[];
  sheetsLoaded: boolean;
  dataVersion: number;
  history: HistoryState | null;
  reviewCount: number;
}

type ProjectDataApiPort = Pick<
  ProjectApiPort,
  'listSheets' | 'getHistory' | 'getReviewCount'
>;

interface HistoryPage {
  offset: number;
  limit: number;
}

interface SheetRefreshOptions {
  markInventoryStale?: boolean;
}

interface ProjectDataStart {
  sheets: Promise<SheetMeta[]>;
  history: Promise<void>;
  reviewCount: Promise<void>;
}

/**
 * The one session-scoped owner for project data and the route-normalization
 * inventory derived from it. Refresh promises deliberately reject unchanged:
 * model-owned callers surface failures, while the plugin host adapter keeps its
 * historically silent failure path.
 */
export interface ProjectDataResource {
  readonly store: Store<ProjectDataState>;
  readonly isCurrent: boolean;
  start(): ProjectDataStart;
  refresh(target: 'sheets', options?: SheetRefreshOptions): Promise<SheetMeta[]>;
  refresh(target: 'history', page?: HistoryPage): Promise<void>;
  refresh(target: 'reviewCount'): Promise<void>;
  invalidate(): void;
  commitHistoryChange(history: HistoryState): void;
  mergeColumnUpdate(updated: ColumnDef): void;
  commitReviewDecision(remaining: number): void;
  has(sheetId: string): boolean;
  normalize(route: RouteState): RouteState | null;
  dispose(): void;
}

export function createProjectDataResource(api: ProjectDataApiPort): ProjectDataResource {
  const store = createStore<ProjectDataState>({
    sheets: [],
    sheetsLoaded: false,
    dataVersion: 0,
    history: null,
    reviewCount: 0,
  });
  let sheetIds: readonly string[] | null = null;
  let generation = 0;
  let disposed = false;

  function mayCommit(requestGeneration: number): boolean {
    return !disposed && requestGeneration === generation;
  }

  function refreshSheets({ markInventoryStale = true }: SheetRefreshOptions = {}): Promise<SheetMeta[]> {
    const requestGeneration = generation;
    if (markInventoryStale) sheetIds = null;
    return api.listSheets().then((sheets) => {
      if (mayCommit(requestGeneration)) {
        sheetIds = sheets.map((sheet) => sheet.id);
        store.set((state) => ({ ...state, sheets, sheetsLoaded: true }));
      }
      return sheets;
    });
  }

  function refreshHistory(page?: HistoryPage): Promise<void> {
    const requestGeneration = generation;
    const request = page === undefined
      ? api.getHistory()
      : api.getHistory(page.offset, page.limit);
    return request.then((history) => {
      if (mayCommit(requestGeneration)) {
        store.set((state) => ({ ...state, history }));
      }
    });
  }

  function refreshReviewCount(): Promise<void> {
    const requestGeneration = generation;
    return api.getReviewCount().then((reviewCount) => {
      if (mayCommit(requestGeneration)) {
        store.set((state) => ({ ...state, reviewCount }));
      }
    });
  }

  function refresh(
    target: 'sheets' | 'history' | 'reviewCount',
    options?: HistoryPage | SheetRefreshOptions,
  ): Promise<SheetMeta[] | void> {
    switch (target) {
      case 'sheets':
        return refreshSheets(options as SheetRefreshOptions | undefined);
      case 'history':
        return refreshHistory(options as HistoryPage | undefined);
      case 'reviewCount':
        return refreshReviewCount();
    }
  }

  return {
    store,
    get isCurrent() {
      return sheetIds !== null;
    },
    start() {
      generation += 1;
      disposed = false;
      return {
        sheets: refreshSheets(),
        history: refreshHistory(),
        reviewCount: refreshReviewCount(),
      };
    },
    refresh: refresh as ProjectDataResource['refresh'],
    invalidate() {
      store.set((state) => ({ ...state, dataVersion: state.dataVersion + 1 }));
    },
    commitHistoryChange(history) {
      store.set((state) => ({
        ...state,
        history,
        dataVersion: state.dataVersion + 1,
      }));
    },
    mergeColumnUpdate(updated) {
      const mergeColumn = (column: ColumnDef) =>
        column.id === updated.id ? { ...column, ...updated, ai: column.ai } : column;
      store.set((state) => ({
        ...state,
        sheets: state.sheets.map((sheet) => ({
          ...sheet,
          columns: sheet.columns.map(mergeColumn),
        })),
        dataVersion: state.dataVersion + 1,
      }));
    },
    commitReviewDecision(remaining) {
      store.set((state) => ({
        ...state,
        reviewCount: remaining,
        dataVersion: state.dataVersion + 1,
      }));
    },
    has(sheetId) {
      return sheetIds?.includes(sheetId) ?? false;
    },
    normalize(route) {
      if (sheetIds === null) return null;
      const normalizedSheetId =
        route.sheetId !== null && sheetIds.includes(route.sheetId)
          ? route.sheetId
          : sheetIds[0] ?? null;
      return normalizedSheetId === route.sheetId
        ? null
        : { ...route, sheetId: normalizedSheetId };
    },
    dispose() {
      disposed = true;
      generation += 1;
    },
  };
}

interface ActionCatalogState {
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  catalog: ActionCatalogPayload | null;
  resolvedTemplates: ActionTemplate[];
  version: number;
}

type ActionCatalogApiPort = Pick<ProjectApiPort, 'listActionCatalog'>;
type ActionCatalogExportTargetOwner = Pick<ActSurfaceStoreHandle, 'setActExportTargets'>;

/**
 * The single session-scoped owner for the accepted action catalog. The
 * independently-consumed export-target projection remains on actSurface and
 * is updated as the catalog lifecycle reaches the same terminal states.
 */
export interface ActionCatalogResource {
  readonly store: Store<ActionCatalogState>;
  start(): Promise<void>;
  refresh(): Promise<void>;
  invalidate(): void;
  dispose(): void;
}

export function createActionCatalogResource(
  projectId: string,
  api: ActionCatalogApiPort,
  actSurface: ActionCatalogExportTargetOwner,
): ActionCatalogResource {
  const store = createStore<ActionCatalogState>({
    status: 'idle',
    error: null,
    catalog: null,
    resolvedTemplates: [],
    version: 0,
  });
  let generation = 0;
  let disposed = false;
  let unsubscribeInvalidation: (() => void) | null = null;

  function mayCommit(requestGeneration: number): boolean {
    return !disposed && requestGeneration === generation;
  }

  function refresh(): Promise<void> {
    const requestGeneration = generation;
    if (mayCommit(requestGeneration)) {
      store.set((state) => ({
        ...state,
        status: 'loading',
        error: null,
        catalog: null,
        resolvedTemplates: [],
      }));
      actSurface.setActExportTargets([]);
    }
    return api
      .listActionCatalog(projectId)
      .then((catalog) => {
        if (!mayCommit(requestGeneration)) return;
        const resolvedTemplates = injectTestOnlyUnassignedAction(
          actionTemplatesFromCatalog(catalog),
        );
        store.set((state) => ({
          status: 'ready',
          error: null,
          catalog,
          resolvedTemplates,
          version: state.version + 1,
        }));
        actSurface.setActExportTargets(exportTargetsFromCatalog(catalog));
      })
      .catch((error: unknown) => {
        if (!mayCommit(requestGeneration)) return;
        store.set((state) => ({
          ...state,
          status: 'error',
          error:
            error instanceof Error && error.message
              ? `Action catalog failed: ${error.message}`
              : 'Action catalog unavailable',
          catalog: null,
          resolvedTemplates: [],
        }));
        actSurface.setActExportTargets([]);
      });
  }

  function invalidate(): void {
    generation += 1;
    if (disposed) return;
    void refresh();
  }

  return {
    store,
    start() {
      generation += 1;
      disposed = false;
      unsubscribeInvalidation ??= onActionCatalogInvalidated(invalidate);
      return refresh();
    },
    refresh,
    invalidate,
    dispose() {
      disposed = true;
      generation += 1;
      unsubscribeInvalidation?.();
      unsubscribeInvalidation = null;
    },
  };
}
