import { describe, expect, it, vi } from 'vitest';
import type { ActionCatalogPayload, ColumnDef, HistoryState, SheetMeta } from '../api/open';
import { emitActionCatalogInvalidated } from '../api/catalogEvents';
import { actionTemplatesFromCatalog } from '../actions/model';
import type {
  DocumentAnnotationPreferences,
  DocumentViewState,
  OpenSplitState,
  PromotedView,
  RibbonMode,
} from './chromeStore';
import {
  chromeStorageKeys,
  createActionCatalogResource,
  createProjectDataResource,
  createScopedChromePreferenceOwner,
  type ChromePreferenceMutationTarget,
  type PreferenceStorage,
} from './workspaceResources';

class MemoryStorage implements PreferenceStorage {
  readonly values = new Map<string, string>();
  failWrites = false;

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    if (this.failWrites) throw new Error('quota');
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    if (this.failWrites) throw new Error('quota');
    this.values.delete(key);
  }
}

function createMutationTarget() {
  let annotationPreferences: DocumentAnnotationPreferences = {};
  const calls = {
    ribbonMode: [] as RibbonMode[],
    activeRibbonTab: [] as string[],
    discoverOpen: [] as boolean[],
    discoverTab: [] as string[],
    promotedViews: [] as PromotedView[][],
    openSplit: [] as Array<OpenSplitState | null>,
    documentView: [] as Array<DocumentViewState | null>,
    documentAnnotationPreferences: [] as DocumentAnnotationPreferences[],
  };
  const target: ChromePreferenceMutationTarget = {
    getDocumentAnnotationPreferences: () => annotationPreferences,
    setRibbonMode: (value) => calls.ribbonMode.push(value),
    setActiveRibbonTab: (value) => calls.activeRibbonTab.push(value),
    setDiscoverOpen: (value) => calls.discoverOpen.push(value),
    setDiscoverTab: (value) => calls.discoverTab.push(value),
    setPromotedViews: (value) => calls.promotedViews.push(value),
    setOpenSplit: (value) => calls.openSplit.push(value),
    setDocumentView: (value) => calls.documentView.push(value),
    setDocumentAnnotationPreferences: (value) => {
      annotationPreferences = value;
      calls.documentAnnotationPreferences.push(value);
    },
  };
  return { calls, target };
}

const projectId = 'p1';

describe('scoped chrome preferences', () => {
  it('preserves the eight existing chromeStorageKeys families exactly', () => {
    expect(chromeStorageKeys.ribbonMode(projectId)).toBe('frisket:ribbon-mode:p1');
    expect(chromeStorageKeys.activeRibbonTab(projectId)).toBe('frisket:ribbon-tab:p1');
    expect(chromeStorageKeys.discoverOpen(projectId)).toBe('frisket:discover-open:p1');
    expect(chromeStorageKeys.discoverTab(projectId)).toBe('frisket:discover-tab:p1');
    expect(chromeStorageKeys.promotedViews(projectId)).toBe('frisket:promoted-views:p1');
    expect(chromeStorageKeys.openSplit(projectId)).toBe('frisket:open-split:p1');
    expect(chromeStorageKeys.documentView(projectId)).toBe('frisket:document-view:p1');
    expect(chromeStorageKeys.documentAnnotationPrefs(projectId)).toBe(
      'frisket:document-annotations:p1',
    );
  });

  it('hydrates every absent field to its safe default and emits a diagnostic', () => {
    const diagnostic = vi.fn();
    const preferences = createScopedChromePreferenceOwner(projectId, {
      storage: new MemoryStorage(),
      diagnostic,
    }).hydrate();

    expect(preferences).toMatchObject({
      ribbonMode: 'ribbon',
      activeRibbonTab: 'analyze',
      discoverTab: 'Facets',
      promotedViews: [],
      openSplit: null,
      documentView: null,
      documentAnnotationPreferences: {},
    });
    expect(diagnostic).toHaveBeenCalledTimes(8);
    expect(diagnostic.mock.calls.every(([entry]) => entry.kind === 'missing')).toBe(true);
  });

  it('hydrates malformed or unsupported values to safe defaults with diagnostics', () => {
    const storage = new MemoryStorage();
    storage.values.set(chromeStorageKeys.ribbonMode(projectId), 'future');
    storage.values.set(chromeStorageKeys.activeRibbonTab(projectId), '');
    storage.values.set(chromeStorageKeys.discoverOpen(projectId), 'yes');
    storage.values.set(chromeStorageKeys.discoverTab(projectId), '');
    storage.values.set(chromeStorageKeys.promotedViews(projectId), '{');
    storage.values.set(chromeStorageKeys.openSplit(projectId), '{}');
    storage.values.set(chromeStorageKeys.documentView(projectId), '[]');
    storage.values.set(chromeStorageKeys.documentAnnotationPrefs(projectId), '[]');
    const diagnostic = vi.fn();

    const preferences = createScopedChromePreferenceOwner(projectId, {
      storage,
      diagnostic,
    }).hydrate();

    expect(preferences.ribbonMode).toBe('ribbon');
    expect(preferences.activeRibbonTab).toBe('analyze');
    expect(preferences.discoverTab).toBe('Facets');
    expect(preferences.promotedViews).toEqual([]);
    expect(preferences.openSplit).toBeNull();
    expect(preferences.documentView).toBeNull();
    expect(preferences.documentAnnotationPreferences).toEqual({});
    expect(diagnostic).toHaveBeenCalledTimes(8);
    expect(
      diagnostic.mock.calls.every(([entry]) =>
        entry.kind === 'malformed' || entry.kind === 'unsupported',
      ),
    ).toBe(true);
  });

  it('keeps the old encodings while each owning command performs the only durable write', () => {
    const storage = new MemoryStorage();
    const diagnostic = vi.fn();
    const owner = createScopedChromePreferenceOwner(projectId, { storage, diagnostic });
    const { calls, target } = createMutationTarget();
    const commands = owner.bindCommands(target);
    const split: OpenSplitState = { kind: 'graph', sheetId: 's1' };

    commands.project.setRibbonMode('menu');
    commands.project.setActiveRibbonTab('act');
    commands.project.setDiscoverOpen(false);
    commands.project.setDiscoverTab('Watches');
    commands.project.setPromotedViews([]);
    commands.sheet.setOpenSplit(split);
    commands.sheet.setDocumentView(null);
    commands.sheet.setSheetAnnotationToggles('1', ['1:2:ner']);

    expect(storage.values.get(chromeStorageKeys.ribbonMode(projectId))).toBe('menu');
    expect(storage.values.get(chromeStorageKeys.activeRibbonTab(projectId))).toBe('act');
    expect(storage.values.get(chromeStorageKeys.discoverOpen(projectId))).toBe('0');
    expect(storage.values.get(chromeStorageKeys.discoverTab(projectId))).toBe('Watches');
    expect(storage.values.get(chromeStorageKeys.promotedViews(projectId))).toBe('[]');
    expect(storage.values.get(chromeStorageKeys.openSplit(projectId))).toBe(
      JSON.stringify(split),
    );
    expect(storage.values.has(chromeStorageKeys.documentView(projectId))).toBe(false);
    expect(storage.values.get(chromeStorageKeys.documentAnnotationPrefs(projectId))).toBe(
      JSON.stringify({ '1': ['1:2:ner'] }),
    );
    expect(calls.ribbonMode).toEqual(['menu']);
    expect(calls.discoverOpen).toEqual([false]);
    expect(calls.openSplit).toEqual([split]);
    expect(calls.documentAnnotationPreferences).toEqual([{ '1': ['1:2:ner'] }]);
    expect(diagnostic).not.toHaveBeenCalled();
  });

  it('treats quota/write failure as best effort: diagnostic plus in-memory commit, no rollback', () => {
    const storage = new MemoryStorage();
    storage.failWrites = true;
    const diagnostic = vi.fn();
    const owner = createScopedChromePreferenceOwner(projectId, { storage, diagnostic });
    const { calls, target } = createMutationTarget();
    const commands = owner.bindCommands(target);

    expect(() => commands.project.setRibbonMode('menu')).not.toThrow();
    expect(() => commands.sheet.setOpenSplit({ kind: 'graph', sheetId: 's2' })).not.toThrow();

    expect(calls.ribbonMode).toEqual(['menu']);
    expect(calls.openSplit).toEqual([{ kind: 'graph', sheetId: 's2' }]);
    expect(diagnostic).toHaveBeenCalledTimes(2);
    expect(diagnostic.mock.calls.every(([entry]) => entry.kind === 'write_failed')).toBe(true);
  });
});

function sheet(id: string, columns: ColumnDef[] = []): SheetMeta {
  return { id, name: `Sheet ${id}`, columns, rowCount: 0 } as SheetMeta;
}

function history(total = 0): HistoryState {
  return { ops: [], total } as unknown as HistoryState;
}

function createProjectDataApi() {
  return {
    listSheets: vi.fn(async (): Promise<SheetMeta[]> => []),
    getHistory: vi.fn(async (): Promise<HistoryState> => history()),
    getReviewCount: vi.fn(async (): Promise<number> => 0),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}

describe('project data resource', () => {
  const route = {
    projectId: 'p1',
    sheetId: 's2',
    actionKind: null,
    review: false,
    panel: null,
  } as const;

  it('does not normalize a delayed or known-stale inventory', () => {
    const api = createProjectDataApi();
    const resource = createProjectDataResource(api);
    expect(resource.isCurrent).toBe(false);
    expect(resource.normalize(route)).toBeNull();

    const pending = deferred<SheetMeta[]>();
    api.listSheets.mockReturnValueOnce(pending.promise);
    void resource.refresh('sheets');
    expect(resource.normalize(route)).toBeNull();
  });

  it('normalizes invalid and empty-project routes deterministically', async () => {
    const api = createProjectDataApi();
    const resource = createProjectDataResource(api);
    api.listSheets.mockResolvedValueOnce([sheet('s1'), sheet('s3')]);
    await resource.refresh('sheets');
    expect(resource.normalize(route)).toEqual({ ...route, sheetId: 's1' });

    api.listSheets.mockResolvedValueOnce([]);
    await resource.refresh('sheets');
    expect(resource.normalize(route)).toEqual({ ...route, sheetId: null });
    expect(resource.normalize({ ...route, sheetId: null })).toBeNull();
  });

  it('models import sequencing: stale first, authoritative refresh, then target navigation', async () => {
    const api = createProjectDataApi();
    const resource = createProjectDataResource(api);
    api.listSheets.mockResolvedValueOnce([sheet('s1')]);
    await resource.refresh('sheets');

    const pending = deferred<SheetMeta[]>();
    api.listSheets.mockReturnValueOnce(pending.promise);
    const refreshed = resource.refresh('sheets');
    expect(resource.normalize({ ...route, sheetId: 's1' })).toBeNull();
    pending.resolve([sheet('s1'), sheet('s9')]);
    await refreshed;
    expect(resource.has('s9')).toBe(true);
    expect(resource.normalize({ ...route, sheetId: 's9' })).toBeNull();
  });

  it('models active-sheet deletion sequencing: refetch first, then normalize remaining sheet', async () => {
    const api = createProjectDataApi();
    const resource = createProjectDataResource(api);
    api.listSheets.mockResolvedValueOnce([sheet('s1'), sheet('s2')]);
    await resource.refresh('sheets');

    const pending = deferred<SheetMeta[]>();
    api.listSheets.mockReturnValueOnce(pending.promise);
    const refreshed = resource.refresh('sheets');
    expect(resource.normalize(route)).toBeNull();
    pending.resolve([sheet('s1')]);
    await refreshed;
    expect(resource.normalize(route)).toEqual({ ...route, sheetId: 's1' });
  });

  it('keeps dataVersion increments byte-identical across every named mutation', async () => {
    const api = createProjectDataApi();
    const originalColumn = {
      id: 'c1',
      name: 'Original',
      type: 'text',
      ai: { generated: true },
    } as unknown as ColumnDef;
    api.listSheets.mockResolvedValueOnce([sheet('s1', [originalColumn])]);
    const resource = createProjectDataResource(api);
    await resource.refresh('sheets');

    resource.invalidate();
    expect(resource.store.get().dataVersion).toBe(1);
    api.getHistory.mockResolvedValueOnce(history(1));
    await resource.refresh('history');
    expect(resource.store.get().dataVersion).toBe(1);
    resource.commitHistoryChange(history(2));
    expect(resource.store.get().dataVersion).toBe(2);
    resource.commitReviewDecision(4);
    expect(resource.store.get().dataVersion).toBe(3);
    resource.mergeColumnUpdate({ ...originalColumn, name: 'Updated', ai: undefined });
    expect(resource.store.get().dataVersion).toBe(4);
    expect(resource.store.get().sheets[0].columns[0]).toMatchObject({
      name: 'Updated',
      ai: originalColumn.ai,
    });
  });

  it('drops all three refresh legs when they resolve after disposal', async () => {
    const api = createProjectDataApi();
    const sheets = deferred<SheetMeta[]>();
    const historyResult = deferred<HistoryState>();
    const reviewCount = deferred<number>();
    api.listSheets.mockReturnValueOnce(sheets.promise);
    api.getHistory.mockReturnValueOnce(historyResult.promise);
    api.getReviewCount.mockReturnValueOnce(reviewCount.promise);
    const resource = createProjectDataResource(api);

    const initial = resource.start();
    resource.dispose();
    sheets.resolve([sheet('stale')]);
    historyResult.resolve(history(7));
    reviewCount.resolve(9);
    await Promise.all([initial.sheets, initial.history, initial.reviewCount]);

    expect(resource.store.get()).toEqual({
      sheets: [],
      sheetsLoaded: false,
      dataVersion: 0,
      history: null,
      reviewCount: 0,
    });
    expect(resource.isCurrent).toBe(false);
  });

  it('starts a fresh generation after disposal without accepting the prior generation', async () => {
    const api = createProjectDataApi();
    const staleSheets = deferred<SheetMeta[]>();
    api.listSheets
      .mockReturnValueOnce(staleSheets.promise)
      .mockResolvedValueOnce([sheet('fresh')]);
    const resource = createProjectDataResource(api);

    const stale = resource.start();
    resource.dispose();
    const fresh = resource.start();
    await fresh.sheets;
    staleSheets.resolve([sheet('stale')]);
    await stale.sheets;

    expect(resource.store.get().sheets.map((item) => item.id)).toEqual(['fresh']);
  });

  it('propagates raw failures and preserves last-known-good state', async () => {
    const api = createProjectDataApi();
    api.listSheets.mockResolvedValueOnce([sheet('s1')]);
    api.getHistory.mockResolvedValueOnce(history(3));
    api.getReviewCount.mockResolvedValueOnce(5);
    const resource = createProjectDataResource(api);
    await Promise.all([
      resource.refresh('sheets'),
      resource.refresh('history'),
      resource.refresh('reviewCount'),
    ]);
    const lastKnownGood = resource.store.get();

    api.listSheets.mockRejectedValueOnce(new Error('sheets failed'));
    api.getHistory.mockRejectedValueOnce(new Error('history failed'));
    api.getReviewCount.mockRejectedValueOnce(new Error('review failed'));
    await expect(
      resource.refresh('sheets', { markInventoryStale: false }),
    ).rejects.toThrow('sheets failed');
    await expect(resource.refresh('history')).rejects.toThrow('history failed');
    await expect(resource.refresh('reviewCount')).rejects.toThrow('review failed');
    expect(resource.store.get()).toBe(lastKnownGood);
    expect(resource.isCurrent).toBe(true);
  });

  it('does not add single-flight behavior to concurrent refreshes', async () => {
    const api = createProjectDataApi();
    const first = deferred<SheetMeta[]>();
    const second = deferred<SheetMeta[]>();
    api.listSheets.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const resource = createProjectDataResource(api);

    const refreshA = resource.refresh('sheets');
    const refreshB = resource.refresh('sheets');
    expect(api.listSheets).toHaveBeenCalledTimes(2);
    first.resolve([sheet('s1')]);
    second.resolve([sheet('s2')]);
    await Promise.all([refreshA, refreshB]);
  });
});

function actionCatalog(): ActionCatalogPayload {
  return {
    schema_version: 'frisket.action_catalog.v2',
    actions: [],
    action_schema: {},
    error_schema: {},
    result_schema: {},
    receipt_schema: {},
    validation_result_schema: {},
  };
}

function createActionCatalogApi() {
  return {
    listActionCatalog: vi.fn(async (): Promise<ActionCatalogPayload> => actionCatalog()),
  };
}

function createExportTargetOwner() {
  return { setActExportTargets: vi.fn() };
}

describe('action catalog resource', () => {
  it('starts with loading, fetches through the project port, and publishes one snapshot', async () => {
    const api = createActionCatalogApi();
    const pending = deferred<ActionCatalogPayload>();
    api.listActionCatalog.mockReturnValueOnce(pending.promise);
    const actSurface = createExportTargetOwner();
    const resource = createActionCatalogResource('p1', api, actSurface);

    const initial = resource.start();
    expect(api.listActionCatalog).toHaveBeenCalledWith('p1');
    expect(resource.store.get()).toEqual({
      status: 'loading',
      error: null,
      catalog: null,
      resolvedTemplates: [],
      version: 0,
    });
    expect(actSurface.setActExportTargets).toHaveBeenLastCalledWith([]);

    const catalog = actionCatalog();
    pending.resolve(catalog);
    await initial;
    expect(resource.store.get()).toMatchObject({
      status: 'ready',
      error: null,
      catalog,
      version: 1,
    });
    expect(resource.store.get().resolvedTemplates.map((template) => template.kind)).toEqual(
      actionTemplatesFromCatalog(catalog).map((template) => template.kind),
    );
    resource.dispose();
  });

  it('preserves explicit clear-on-loading and clear-on-error semantics', async () => {
    const api = createActionCatalogApi();
    const actSurface = createExportTargetOwner();
    const resource = createActionCatalogResource('p1', api, actSurface);
    await resource.start();

    api.listActionCatalog.mockRejectedValueOnce(new Error('network down'));
    const refresh = resource.refresh();
    expect(resource.store.get()).toMatchObject({
      status: 'loading',
      catalog: null,
      resolvedTemplates: [],
      version: 1,
    });
    await refresh;
    expect(resource.store.get()).toEqual({
      status: 'error',
      error: 'Action catalog failed: network down',
      catalog: null,
      resolvedTemplates: [],
      version: 1,
    });
    expect(actSurface.setActExportTargets).toHaveBeenLastCalledWith([]);
    resource.dispose();
  });

  it('invalidates by refetching immediately and rejects the displaced generation', async () => {
    const api = createActionCatalogApi();
    const stale = deferred<ActionCatalogPayload>();
    const fresh = deferred<ActionCatalogPayload>();
    api.listActionCatalog
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(fresh.promise);
    const resource = createActionCatalogResource('p1', api, createExportTargetOwner());

    const initial = resource.start();
    emitActionCatalogInvalidated();
    expect(api.listActionCatalog).toHaveBeenCalledTimes(2);
    expect(resource.store.get().status).toBe('loading');

    const freshCatalog = actionCatalog();
    fresh.resolve(freshCatalog);
    await fresh.promise;
    await Promise.resolve();
    const accepted = resource.store.get();
    expect(accepted).toMatchObject({ status: 'ready', catalog: freshCatalog, version: 1 });

    stale.resolve(actionCatalog());
    await initial;
    expect(resource.store.get()).toBe(accepted);
    resource.dispose();
  });

  it('drops late results and removes its invalidation subscription on dispose', async () => {
    const api = createActionCatalogApi();
    const pending = deferred<ActionCatalogPayload>();
    api.listActionCatalog.mockReturnValueOnce(pending.promise);
    const actSurface = createExportTargetOwner();
    const resource = createActionCatalogResource('p1', api, actSurface);

    const initial = resource.start();
    resource.dispose();
    pending.resolve(actionCatalog());
    await initial;
    expect(resource.store.get()).toMatchObject({ status: 'loading', version: 0 });
    expect(actSurface.setActExportTargets).toHaveBeenCalledTimes(1);

    emitActionCatalogInvalidated();
    expect(api.listActionCatalog).toHaveBeenCalledTimes(1);
  });

  it('does not add single-flight behavior to concurrent refreshes', async () => {
    const api = createActionCatalogApi();
    const first = deferred<ActionCatalogPayload>();
    const second = deferred<ActionCatalogPayload>();
    api.listActionCatalog
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const resource = createActionCatalogResource('p1', api, createExportTargetOwner());

    const refreshA = resource.refresh();
    const refreshB = resource.refresh();
    expect(api.listActionCatalog).toHaveBeenCalledTimes(2);
    first.resolve(actionCatalog());
    second.resolve(actionCatalog());
    await Promise.all([refreshA, refreshB]);
    resource.dispose();
  });
});
