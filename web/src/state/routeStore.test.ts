import { describe, expect, it, vi } from 'vitest';
import { type ColumnDef, type Row } from '../api/open';
import { createProjectApi } from '../api/real';
import { createRouteSyncController } from '../core/route/RouteSyncController';
import type { RouteState } from '../core/route/RouteState';
import { createChromeStore } from './chromeStore';
import type { HydratedChromePreferences } from './workspaceResources';
import { createDetailStore } from './detailStore';
import { createGridViewStore } from './gridViewStore';
import { createJobStore, type JobState } from './jobStore';
import { createLensViewStore } from './lensViewStore';
import { createPreviewViewStore } from './previewViewStore';
import { createRouteStore } from './routeStore';
import { createRowCacheStore } from './rowCacheStore';
import { createWorkViewStore } from './workViewStore';
import { createSelectionStore } from './selectionStore';
import { resetForRouteSheetChange } from './workspaceTransitions';

const api = createProjectApi('p1');

const base: RouteState = {
  projectId: 'p1',
  sheetId: 's1',
  actionKind: null,
  review: false,
  panel: null,
};

const column = {
  id: 'c1',
  name: 'status',
  type: 'string',
} as ColumnDef;

function createMatrixHarness() {
  const gridView = createGridViewStore();
  const selection = createSelectionStore();
  const detail = createDetailStore();
  const workView = createWorkViewStore();
  const lensView = createLensViewStore();
  const previewView = createPreviewViewStore(api);
  const route = createRouteStore('p1', {
    resetForSheetChange: (sheetId) =>
      resetForRouteSheetChange(
        { gridView, selection, detail, workView, lensView, previewView },
        sheetId,
      ),
  });
  route.projectExternal(base);
  return { detail, gridView, lensView, previewView, route, workView, selection };
}

function seedClassOne(harness: ReturnType<typeof createMatrixHarness>): void {
  harness.gridView.applyFilter({
    filter: { status: { eq: 'open' } },
    filterValueLabel: 'Open',
  });
  harness.gridView.applySort({
    column: 'name',
    direction: 'desc',
    sort: [{ column: 'name', dir: 'desc' }],
  });
  harness.gridView.setSortPanelOpen(true);
  harness.detail.openHeaderMenu(
    { column, columnIndex: 0, bounds: { x: 0, y: 0, width: 1, height: 1 } },
  );
  harness.workView.setAnswersColumn('c1');
}

function switchSheet(
  harness: ReturnType<typeof createMatrixHarness>,
  origin: 'navigate' | 'projectExternal',
): void {
  harness.route[origin]({ ...base, sheetId: 's2' });
}

describe('routeStore three-entry write seam', () => {
  it('projectExternal is a structural-bail projection with zero history writes', () => {
    const route = createRouteStore('p1');
    const write = vi.fn();
    const controller = createRouteSyncController({ routeStore: route, write });

    route.projectExternal(base);
    route.projectExternal({ ...base, panel: null });

    expect(route.store.get()).toEqual(base);
    expect(write).not.toHaveBeenCalled();
    controller.dispose();
  });

  it('navigate writes one matrix-approved entry and normalize writes one replace', () => {
    const route = createRouteStore('p1');
    route.projectExternal(base);
    const write = vi.fn();
    const controller = createRouteSyncController({ routeStore: route, write });

    route.navigate({ ...base, sheetId: 's2' });
    route.normalize({ ...base, sheetId: 's1' });

    expect(write.mock.calls).toEqual([
      [{ ...base, sheetId: 's2' }, 'push'],
      [{ ...base, sheetId: 's1' }, 'replace'],
    ]);
    controller.dispose();
  });

  it.each(['projectExternal', 'navigate', 'normalize'] as const)(
    '%s rejects a route for a different mounted project',
    (entry) => {
      const route = createRouteStore('p1');
      expect(() => route[entry]({ ...base, projectId: 'p2' })).toThrow(
        'same-project route rejected projectId',
      );
      expect(route.store.get().projectId).toBe('p1');
    },
  );

  it('permits only one live history writer', () => {
    const route = createRouteStore('p1');
    const unsubscribe = route.subscribeHistoryCommands(() => {});
    expect(() => route.subscribeHistoryCommands(() => {})).toThrow(
      'route history writer already attached',
    );
    unsubscribe();
    expect(() => route.subscribeHistoryCommands(() => {})).not.toThrow();
  });

  it('does not expose the raw mutable store at runtime', () => {
    const route = createRouteStore('p1');
    expect('set' in route.store).toBe(false);
  });
});

describe('applyRoute fixed six-class matrix', () => {
  it.each(['navigate', 'projectExternal'] as const)(
    'class 1: click-only divergence fields now reset for %s',
    (origin) => {
      const harness = createMatrixHarness();
      seedClassOne(harness);

      switchSheet(harness, origin);

      expect(harness.gridView.store.get().applied).toEqual({
        filter: null,
        sort: null,
        filterValueLabel: null,
      });
      expect(harness.gridView.store.get().sortPanelOpen).toBe(false);
      expect(harness.detail.store.get().headerMenu).toBeNull();
      expect(harness.workView.store.get().answersView.chosenColumnId).toBeNull();
    },
  );

  it('class 1 exception: only sort direction resets in the remaining draft state', () => {
    const harness = createMatrixHarness();
    seedClassOne(harness);
    const before = harness.gridView.store.get().draft;

    harness.route.navigate({ ...base, sheetId: 's2' });

    const after = harness.gridView.store.get().draft;
    expect(after.sortDirection).toBe('asc');
    expect(after.sortColumn).toBe(before.sortColumn);
  });

  it('class 1 exception: answers chosen column resets for every origin', () => {
    for (const origin of ['navigate', 'projectExternal'] as const) {
      const harness = createMatrixHarness();
      harness.workView.setAnswersColumn('c1');
      switchSheet(harness, origin);
      expect(harness.workView.store.get().answersView.chosenColumnId).toBeNull();
    }
  });

  it.each(['navigate', 'projectExternal'] as const)(
    'class 5 work-view survivors remain exact while both nested Answers choices reset for %s',
    (origin) => {
      const harness = createMatrixHarness();
      harness.workView.setGridOnlySheetId('s1');
      harness.workView.setActivePromotedKey('map:s1:c1');
      harness.workView.setAnswersViewSheetId('s1');
      harness.workView.setAnswersColumn('c1');
      harness.workView.setAnswersActiveLink(42);

      switchSheet(harness, origin);

      expect(harness.workView.store.get()).toEqual({
        gridOnlySheetId: 's1',
        activePromotedKey: 'map:s1:c1',
        answersViewSheetId: 's1',
        answersView: { chosenColumnId: null, activeLinkId: null },
      });
    },
  );

  it.each(['navigate', 'projectExternal'] as const)(
    'class 2: %s clears selection/lens/error and invalidates preview before commit',
    (origin) => {
      const order: string[] = [];
      const gridView = createGridViewStore();
      const selection = createSelectionStore();
      const detail = createDetailStore();
      const workView = createWorkViewStore();
      const lensView = createLensViewStore();
      const previewView = createPreviewViewStore(api);
      selection.setSelectedRows({ sheetId: 's1', rowIds: ['r1'], rowIndexes: [0] });
      lensView.setLensView({
        lensId: 1,
        name: 'lens',
        sheetId: 's1',
        rowIds: [1],
        scores: {},
        total: 1,
      });
      lensView.setLensOpenError('stale');
      const route = createRouteStore('p1', {
        resetForSheetChange: (sheetId) => {
          order.push('reset');
          resetForRouteSheetChange(
            { gridView, selection, detail, workView, lensView, previewView },
            sheetId,
          );
        },
      });
      route.projectExternal(base);
      order.length = 0;

      let generation = 1;
      const capturedGeneration = generation;
      let stalePreviewCommits = 0;
      route.registerSheetChangeTeardown(() => {
        order.push('teardown');
        generation += 1;
      });
      route.store.subscribe(() => {
        order.push('hydrate');
        if (capturedGeneration === generation) stalePreviewCommits += 1;
      });

      route[origin]({ ...base, sheetId: 's2' });

      expect(order).toEqual(['teardown', 'reset', 'hydrate']);
      expect(stalePreviewCommits).toBe(0);
      expect(selection.store.get().selectedRows).toEqual({
        sheetId: 's2',
        rowIds: [],
        rowIndexes: [],
      });
      expect(lensView.store.get().lensView).toBeNull();
      expect(lensView.store.get().lensOpenError).toBeNull();
    });

  it('class 3: route commits before target row hydration runs', () => {
    const harness = createMatrixHarness();
    const row = { id: 'r2' } as Row;
    const observed: string[] = [];
    harness.route.store.subscribe(() => {
      observed.push(`route:${harness.route.store.get().sheetId}`);
      harness.detail.openRow(row);
      observed.push(`row:${harness.detail.store.get().rowDrawer?.id}`);
    });

    harness.route.navigate({
      ...base,
      sheetId: 's2',
      panel: { kind: 'row', rowId: 'r2' },
    });

    expect(observed).toEqual(['route:s2', 'row:r2']);
    expect(harness.route.store.get().panel).toEqual({ kind: 'row', rowId: 'r2' });
  });

  it('class 4: sheet-keyed maps are untouched by a sheet change', () => {
    const harness = createMatrixHarness();
    harness.gridView.setColumnOrder('s1', ['status']);
    harness.gridView.setFrozenColumnCount('s1', 2);
    harness.gridView.setHiddenColumns('s1', ['secret']);
    const before = harness.gridView.store.get();

    harness.route.navigate({ ...base, sheetId: 's2' });

    const after = harness.gridView.store.get();
    expect(after.columnOrderBySheet).toBe(before.columnOrderBySheet);
    expect(after.frozenColumnCountBySheet).toBe(before.frozenColumnCountBySheet);
    expect(after.hiddenColumnsBySheet).toBe(before.hiddenColumnsBySheet);
  });

  it('class 4 exception: flat columnGroupSpecs survive for SheetGrid remount handling', () => {
    const harness = createMatrixHarness();
    harness.gridView.setColumnGroupSpecs([
      {
        run_id: 1,
        label: 'AI',
        columns: ['summary'],
        show_confidence: true,
        show_justification: true,
      },
    ]);
    const groups = harness.gridView.store.get().columnGroupSpecs;

    harness.route.navigate({ ...base, sheetId: 's2' });

    expect(harness.gridView.store.get().columnGroupSpecs).toBe(groups);
  });

  it('class 5: project-scoped job state survives a sheet change', () => {
    const harness = createMatrixHarness();
    const job = createJobStore('p1', api);
    const run = { runId: 'keep-running' } as JobState['run'];
    job.store.set((state) => ({ ...state, run }));

    harness.route.navigate({ ...base, sheetId: 's2' });

    expect(job.store.get().run).toBe(run);
  });

  it('class 5 exception: openSplit survives; capability loss is a separate trigger', () => {
    const harness = createMatrixHarness();
    const preferences: HydratedChromePreferences = {
      ribbonMode: 'ribbon',
      activeRibbonTab: 'analyze',
      discoverOpen: true,
      discoverTab: 'Facets',
      promotedViews: [],
      openSplit: null,
      documentView: null,
      documentAnnotationPreferences: {},
    };
    const chrome = createChromeStore('p1', preferences);
    const split = { kind: 'map', sheetId: 's1', columnId: 'geo' } as const;
    chrome.setOpenSplit(split);

    harness.route.navigate({ ...base, sheetId: 's2' });

    expect(chrome.store.get().openSplit).toBe(split);
  });

  it('class 6: row cache re-keys without clearing old slots', () => {
    const harness = createMatrixHarness();
    const rowCache = createRowCacheStore();
    const oldSlot = rowCache.getSlot('s1');
    const rows = [{ id: 'r1' }] as Row[];
    oldSlot.setPage('s1:0', 0, rows, 1);

    harness.route.navigate({ ...base, sheetId: 's2' });

    expect(rowCache.getSlot('s1')).toBe(oldSlot);
    expect(rowCache.getSlot('s1').getPage(0)).toBe(rows);
    expect(rowCache.getSlot('s2')).not.toBe(oldSlot);
  });

  it('class 6 exception: reveal-children childFilter survives set-then-navigate', () => {
    const harness = createMatrixHarness();
    const carriedIntent = {
      sheetId: 's2',
      parentSheetName: 'Parents',
      parentRowId: 'r1',
      parentRowIndex: 0,
      count: 3,
    };
    harness.detail.setChildFilter(carriedIntent);

    harness.route.navigate({ ...base, sheetId: 's2' });

    expect(harness.detail.store.get().childFilter).toBe(carriedIntent);
  });
});
