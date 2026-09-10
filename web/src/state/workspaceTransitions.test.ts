// Invariant: no imperative cross-store subscribers outside this module. Every
// real cross-domain composition (gridView/detail/selection/workView/chrome, …)
// must live as ONE exported *Transition function here that production calls —
// not re-composed inline at call sites. Cross-domain transaction-parity tests
// live here, not in state/gridViewStore.test.ts, which keeps only its own
// store-level unit tests.
//
// FINAL ASSERTION: a grep-shaped scan (precedent: core/route/writeCutover.test.ts,
// scripts/check-substrate-boundaries.mjs) pins that no two ADJACENT statement
// lines in useWorkspaceModel.tsx or App.tsx call two DIFFERENT workspace store
// handles directly — every real cross-store handler sequence in those files must
// collapse into a single call to a *Transition function from this module. See
// that test's own comment for what "adjacent" means and the calibration notes on
// false positives it deliberately does not chase.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { createGridViewStore } from './gridViewStore';
import { createSelectionStore } from './selectionStore';
import { createDetailStore } from './detailStore';
import { createWorkViewStore } from './workViewStore';
import { createCompareViewStore } from './compareViewStore';
import { createLensViewStore } from './lensViewStore';
import { createChromeStore } from './chromeStore';
import {
  actionLaunchForRoute,
  applyGridFilterTransition,
  applyGridSortTransition,
  applySavedViewTransition,
  clearGridFilterTransition,
  openCompareTabTransition,
  focusCompareTabTransition,
  toggleProvenanceWithDrawersTransition,
  openRowPanelTransition,
  openColumnPanelTransition,
  openRowRefTransition,
  runActionFromSurfaceTransition,
  closeRoutePanelTransition,
  confirmDeleteRowsTransition,
  applyLensTransition,
} from './workspaceTransitions';
import {
  emptySelectedRows,
} from '../workspace/workspaceState';
import type { ColumnDef, GridFilterSpec, GridSortSpec, Row } from '../api/open';
import type { ChildFilter } from '../workspace/workspaceState';
import type { RouteState } from '../core/route/RouteState';

describe('route hydration exceptions', () => {
  it('class 3 exception: a stale actionLaunch never hydrates a different route kind', () => {
    const launch = { kind: 'media.ocr', initial: { sourceColumn: 'file' } };
    expect(actionLaunchForRoute(launch, 'media.ocr')).toBe(launch);
    expect(actionLaunchForRoute(launch, 'media.transcribe')).toBeNull();
    expect(actionLaunchForRoute(launch, null)).toBeNull();
  });

  it('keeps the scalar source prefill in browser launch state', () => {
    const setActionLaunch = vi.fn();
    const openActionPanel = vi.fn();
    const navigate = vi.fn();
    const nextRoute: RouteState = {
      projectId: 'p1', sheetId: 's1', actionKind: 'map.extract', review: false, panel: null,
    };

    runActionFromSurfaceTransition(
      {
        actSurface: { setActionLaunch },
        chrome: { openActionPanel },
        route: { navigate },
      } as never,
      'extract',
      'story',
      nextRoute,
      { prompt: 'What happened?' },
    );

    expect(setActionLaunch).toHaveBeenCalledWith({
      kind: 'extract',
      initial: { sourceColumn: 'story', prompt: 'What happened?' },
    });
    expect(openActionPanel).toHaveBeenCalledOnce();
    expect(navigate).toHaveBeenCalledWith(nextRoute);
  });
});

describe('gridViewStore + selection/detail stores — cross-domain transaction parity', () => {
  // Together, gridView's action AND selectionStore's/detailStore's own
  // actions must reproduce exactly the composed cross-store transition —
  // the composition lives in ONE place, state/workspaceTransitions.ts
  // (applyGridFilterTransition/applyGridSortTransition/
  // applySavedViewTransition/clearGridFilterTransition/
  // selectSheetTransition); useWorkspaceModel.tsx's handlers call those
  // functions, and every test below calls the SAME functions instead of
  // re-composing the store calls, so a dropped call in a handler (e.g. a
  // removed detail.clearForGridTransition()) fails this suite instead of
  // silently passing it.

  const seedChildFilter: ChildFilter = {
    sheetId: 's1',
    parentSheetName: 'p',
    parentRowId: '1',
    parentRowIndex: 0,
    count: 3,
  };
  const seedRow = { id: '1' } as unknown as Row;
  const seedColumn = { id: 'c1' } as unknown as ColumnDef;

  function seedSelectionAndDetail() {
    const selection = createSelectionStore();
    const detail = createDetailStore();
    selection.setSelectedRows({ sheetId: 's1', rowIds: ['1', '2'], rowIndexes: [0, 1] });
    detail.setChildFilter(seedChildFilter);
    detail.openRow(seedRow); // seeds rowDrawer
    detail.openColumn(seedColumn); // seeds columnDrawer (note: openColumn also clears rowDrawer —
    // re-open the row after, so both fields end up independently seeded).
    detail.store.set((s) => ({ ...s, rowDrawer: seedRow }));
    return { selection, detail };
  }

  it('applyGridFilter: filter applies AND childFilter/selectedRows/rowDrawer clear', () => {
    const gridView = createGridViewStore();
    const { selection, detail } = seedSelectionAndDetail();

    const filter: GridFilterSpec = { status: { eq: 'open' } };
    // = production's applyGridFilterForColumn, via applyGridFilterTransition
    // (state/workspaceTransitions.ts) — the same function the bind layer calls.
    applyGridFilterTransition({ gridView, detail, selection }, 's1', {
      filter,
    });

    expect(gridView.store.get().applied.filter).toEqual(filter);
    expect(detail.store.get().childFilter).toBeNull();
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
    expect(detail.store.get().rowDrawer).toBeNull();
    // applyGridFilter's half never touched columnDrawer — pin the asymmetry
    // (only applySavedViewGrid/clearForSavedView clears columnDrawer too).
    expect(detail.store.get().columnDrawer).not.toBeNull();
  });

  it('applyGridFilter (bbox variant — "filter to this area"): same clear behavior as a manual filter', () => {
    const gridView = createGridViewStore();
    const { selection, detail } = seedSelectionAndDetail();

    // = production's applyGridBboxFilter — same applyGridFilterTransition
    // as the manual-filter test above, only the filter args differ.
    applyGridFilterTransition({ gridView, detail, selection }, 's1', {
      filter: { geo: { bbox: { min_lon: 0, min_lat: 0, max_lon: 1, max_lat: 1 } } },
    });

    expect(gridView.store.get().applied.filter).toEqual({
      geo: { bbox: { min_lon: 0, min_lat: 0, max_lon: 1, max_lat: 1 } },
    });
    expect(detail.store.get().childFilter).toBeNull();
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
    expect(detail.store.get().rowDrawer).toBeNull();
  });

  it('applyGridFilter carries the mention group’s spelling through to applied.filterValueLabel', () => {
    const gridView = createGridViewStore();
    const { selection, detail } = seedSelectionAndDetail();

    // = production's applyGridEntityFilter (useWorkspaceModel.tsx), the ONE
    // application that carries a display label. It routes through
    // applyGridBboxFilterTransition, which delegates the apply half to this
    // function (pinned separately by gridBboxFilterRevealsGrid.test.ts) — so
    // the label surviving THIS hop is what makes the toolbar chip able to name
    // the value. The fingerprint is a comparison token nobody wrote; without
    // the carried spelling the chip could name the type alone.
    applyGridFilterTransition({ gridView, detail, selection }, 's1', {
      filter: { entities: { entity_eq: { type: 'organization', fingerprint: 'acme corp' } } },
      filterValueLabel: 'Acme Corp.',
    });

    expect(gridView.store.get().applied.filterValueLabel).toBe('Acme Corp.');

    // And a filter applied WITHOUT one drops it — no spelling outlives the
    // filter it described.
    applyGridFilterTransition({ gridView, detail, selection }, 's1', {
      filter: { status: { eq: 'open' } },
    });
    expect(gridView.store.get().applied.filterValueLabel).toBeNull();
  });

  it('production wiring: applyGridEntityFilter is the one site that feeds filterValueLabel', () => {
    // The label's whole value is that it reaches the store from the Mentions
    // panel's group click. The panel->host half is pinned by
    // tests/component/MentionsPanel.test.tsx (the click emits the spelling as
    // its third argument) and the store half by the test above; this pins the
    // hop between them, which lives inside a React hook and has no other
    // seam. Red if applyGridEntityFilter stops forwarding its valueLabel.
    const source = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), '../workspace/useWorkspaceModel.tsx'),
      'utf8',
    );
    const body = source.slice(source.indexOf('const applyGridEntityFilter'));
    expect(
      /\(columnName: string, value: GridFilterEntityValue, valueLabel\?: string\)/.test(body),
      'applyGridEntityFilter must accept the clicked group\u2019s spelling',
    ).toBe(true);
    expect(
      /filterValueLabel: valueLabel \?\? null,/.test(body),
      'applyGridEntityFilter must put that spelling on the filter application',
    ).toBe(true);
    // …and the host capability must hand it over rather than dropping it on
    // the floor at the columnId->name resolution.
    expect(
      /applyGridEntityFilter\(column\.name, value, valueLabel\)/.test(source),
      'grid.filter.applyEntity must forward valueLabel to applyGridEntityFilter',
    ).toBe(true);
  });

  it('applyGridSort: sort applies AND childFilter/selectedRows/rowDrawer clear', () => {
    const gridView = createGridViewStore();
    const { selection, detail } = seedSelectionAndDetail();

    const sort: GridSortSpec = [{ column: 'name', dir: 'asc' }];
    // = production's applyGridSortForColumn, via applyGridSortTransition.
    applyGridSortTransition({ gridView, detail, selection }, 's1', {
      column: 'name',
      direction: 'asc',
      sort,
    });

    expect(gridView.store.get().applied.sort).toEqual(sort);
    expect(detail.store.get().childFilter).toBeNull();
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
    expect(detail.store.get().rowDrawer).toBeNull();
  });

  it('inline filter (applyInlineFilter, non-empty branch): applies AND clears the same triple', () => {
    const gridView = createGridViewStore();
    const { selection, detail } = seedSelectionAndDetail();

    const filter: GridFilterSpec = { name: { contains: 'foo' } };
    // = production's applyInlineFilter non-empty branch — shares
    // applyGridFilterTransition with applyGridFilterForColumn/
    // applyGridBboxFilter above.
    applyGridFilterTransition({ gridView, detail, selection }, 's1', {
      filter,
    });

    expect(gridView.store.get().applied.filter).toEqual(filter);
    expect(detail.store.get().childFilter).toBeNull();
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
  });

  it('inline filter (applyInlineFilter, empty branch): clearFilter applies AND selectedRows clears (sheetId present) — childFilter/rowDrawer untouched', () => {
    const gridView = createGridViewStore();
    gridView.applyFilter({ filter: { name: { contains: 'foo' } } });
    const { selection, detail } = seedSelectionAndDetail();

    // = production's applyInlineFilter empty branch, via
    // clearGridFilterTransition — the same function clearGridFilter uses
    // (see the sheetId-gate test below).
    clearGridFilterTransition({ gridView, selection }, 's1');

    expect(gridView.store.get().applied.filter).toBeNull();
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
    // clearGridFilter's half does NOT touch childFilter/rowDrawer — only
    // applyGridFilter/applyGridSort/applySavedViewGrid do.
    expect(detail.store.get().childFilter).not.toBeNull();
    expect(detail.store.get().rowDrawer).not.toBeNull();
  });

  it('clearGridFilter/clearGridSort: selectedRows clears ONLY when the bind-layer caller has a sheetId to pass', () => {
    // The `action.sheetId ? {...} : state` gate — a whole-
    // state Object.is bail on no-sheetId — lives INSIDE
    // clearGridFilterTransition itself (state/workspaceTransitions.ts), not
    // at the useWorkspaceModel.tsx call site: clearGridFilter passes
    // `sheet?.id`, so this is directly testable here with both a defined and
    // an undefined sheetId.
    const gridView = createGridViewStore();
    gridView.applyFilter({ filter: { status: { eq: 'open' } } });
    const selection = createSelectionStore();
    selection.setSelectedRows({ sheetId: 's1', rowIds: ['1', '2'], rowIndexes: [0, 1] });
    const before = selection.store.get();

    // No sheetId — the filter still clears, but selectedRows does NOT.
    clearGridFilterTransition({ gridView, selection }, undefined);
    expect(gridView.store.get().applied.filter).toBeNull();
    expect(selection.store.get().selectedRows).toBe(before.selectedRows);

    // A sheetId present — selectedRows clears too.
    clearGridFilterTransition({ gridView, selection }, 's1');
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
  });

  it('applySavedViewGrid: grid fields replace wholesale AND selectedRows/rowDrawer/columnDrawer clear — childFilter is NOT cleared (asymmetry pinned, matches pre-migration :448-469)', () => {
    const gridView = createGridViewStore();
    const { selection, detail } = seedSelectionAndDetail();

    // = production's applySavedView, via applySavedViewTransition.
    applySavedViewTransition({ gridView, detail, selection }, 's1', {
      viewId: 1,
      sheetId: 's1',
      filter: { status: { eq: 'open' } },
      sort: null,
      draftSortColumn: '',
      draftSortDirection: 'asc',
      columns: ['status'],
      hiddenColumns: [],
      columnGroupSpecs: [],
    });

    expect(gridView.store.get().applied.filter).toEqual({ status: { eq: 'open' } });
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
    expect(detail.store.get().rowDrawer).toBeNull();
    expect(detail.store.get().columnDrawer).toBeNull();
    expect(detail.store.get().childFilter).not.toBeNull(); // the documented asymmetry
  });
});

describe('workspaceTransitions.ts — chrome/detail/actSurface cross-domain transactions', () => {
  // Pins the compositions found still inline
  // in useWorkspaceModel.tsx/App.tsx handlers (openOcrCompareTab/
  // openTranscribeCompareTab, the ⌘K palette's
  // toggleProvenance command body + App.tsx's toolbar ⋯ overflow Provenance
  // item) — same structural-pin discipline as the suite above: every
  // test calls the SAME exported function production now calls, so a future
  // dropped store call fails here instead of silently passing.

  it('openCompareTabTransition("ocr"): opens OCR, clears promoted/grid-only, then closes the split in exact order', () => {
    const chrome = createChromeStore('p1');
    const workView = createWorkViewStore();
    const compareView = createCompareViewStore();
    chrome.setOpenSplit({ kind: 'map', sheetId: 's1' });
    workView.setActivePromotedKey('map:s1:');
    workView.setGridOnlySheetId('s1');
    const calls: string[] = [];
    const open = compareView.open;
    const setActivePromotedKey = workView.setActivePromotedKey;
    const setGridOnlySheetId = workView.setGridOnlySheetId;
    const setOpenSplit = chrome.setOpenSplit;
    compareView.open = (kind) => {
      calls.push(`open:${kind}`);
      open(kind);
    };
    workView.setActivePromotedKey = (key) => {
      calls.push(`promoted:${key}`);
      setActivePromotedKey(key);
    };
    workView.setGridOnlySheetId = (id) => {
      calls.push(`grid:${id}`);
      setGridOnlySheetId(id);
    };
    chrome.setOpenSplit = (split) => {
      calls.push(`split:${split}`);
      setOpenSplit(split);
    };

    // = production's openOcrCompareTab (useWorkspaceModel.tsx).
    openCompareTabTransition(chrome, workView, compareView, 'ocr');

    expect(compareView.store.get().ocr.open).toBe(true);
    expect(workView.store.get().activePromotedKey).toBeNull();
    expect(workView.store.get().gridOnlySheetId).toBeNull();
    expect(chrome.store.get().openSplit).toBeNull();
    expect(calls).toEqual(['open:ocr', 'promoted:null', 'grid:null', 'split:null']);
  });

  it('openCompareTabTransition("transcribe"): opens Transcribe and closes the split', () => {
    const chrome = createChromeStore('p1');
    const workView = createWorkViewStore();
    const compareView = createCompareViewStore();
    chrome.setOpenSplit({ kind: 'graph', sheetId: 's1' });

    // = production's openTranscribeCompareTab (useWorkspaceModel.tsx).
    openCompareTabTransition(chrome, workView, compareView, 'transcribe');

    expect(compareView.store.get().transcribe.open).toBe(true);
    expect(chrome.store.get().openSplit).toBeNull();
  });

  it('openCompareTabTransition("topic"): opens Topic Compare and closes the split', () => {
    const chrome = createChromeStore('p1');
    const workView = createWorkViewStore();
    const compareView = createCompareViewStore();
    chrome.setOpenSplit({ kind: 'graph', sheetId: 's1' });

    openCompareTabTransition(chrome, workView, compareView, 'topic');

    expect(compareView.store.get().topic).toMatchObject({ open: true, active: true });
    expect(chrome.store.get().openSplit).toBeNull();
  });

  it('focusCompareTabTransition focuses without reopening or clearing grid-only/split', () => {
    const chrome = createChromeStore('p1');
    const workView = createWorkViewStore();
    const compareView = createCompareViewStore();
    chrome.setOpenSplit({ kind: 'map', sheetId: 's1' });
    workView.setGridOnlySheetId('s1');
    workView.setActivePromotedKey('map:s1:');

    focusCompareTabTransition(workView, compareView, 'translate');

    expect(compareView.store.get().translate).toMatchObject({ open: false, active: true });
    expect(workView.store.get().activePromotedKey).toBeNull();
    expect(workView.store.get().gridOnlySheetId).toBe('s1');
    expect(chrome.store.get().openSplit).toEqual({ kind: 'map', sheetId: 's1' });
  });

  it('toggleProvenanceWithDrawersTransition: closes the Detail drawers AND flips chrome provenanceOpen — the SAME helper the ⌘K command body and the toolbar overflow item both call', () => {
    const detail = createDetailStore();
    const chrome = createChromeStore('p1');
    const seedRow = { id: '1' } as unknown as Row;
    detail.openRow(seedRow);
    expect(detail.store.get().rowDrawer).not.toBeNull();
    expect(chrome.store.get().provenanceOpen).toBe(false);

    // = production's ⌘K palette toggleProvenance command body AND App.tsx's
    // ToolbarOverflowMenu Provenance item — two call sites, one function.
    toggleProvenanceWithDrawersTransition({ detail, chrome });

    expect(detail.store.get().rowDrawer).toBeNull();
    expect(chrome.store.get().provenanceOpen).toBe(true);

    // A second fire (re-opening the drawer first) flips provenanceOpen back
    // off and closes the drawer again — pins the toggle, not just a one-shot.
    detail.openRow(seedRow);
    toggleProvenanceWithDrawersTransition({ detail, chrome });
    expect(detail.store.get().rowDrawer).toBeNull();
    expect(chrome.store.get().provenanceOpen).toBe(false);
  });
});

describe('workspaceTransitions.ts — invariant 10b closure sweep (workspace-substrate-6c-transitions-sweep-v1)', () => {
  // The six remaining inline cross-store compositions
  // enumerated but not extracted earlier: openRowPanel,
  // openColumnPanel, closeRoutePanel, openRowRef (route-panel handlers,
  // useWorkspaceModel.tsx), confirmDeleteRows's delete-succeeded .then()
  // composition, and applyLens's post-resolve composition. Same discipline:
  // every test below calls the SAME exported function production now calls.

  const baseRoute: RouteState = {
    projectId: 'p1',
    sheetId: 's1',
    actionKind: null,
    review: false,
    panel: null,
  };

  it('openRowPanelTransition: opens the row drawer, sets the selected column, and writes the /row route panel — same order as production (detail, then selection, then the route write)', () => {
    const detail = createDetailStore();
    const selection = createSelectionStore();
    const writeRoute = vi.fn<(next: RouteState) => void>();
    const row = { id: 'r1' } as unknown as Row;
    const col = { id: 'c1' } as unknown as ColumnDef;

    // = production's openRowPanel (useWorkspaceModel.tsx).
    const preview = {
      column: { name: 'sample', columnType: 'text', format: null, hidden: false, overwritesColumnId: 'c1' },
      cell: { value: null, error: 'preview failed' },
    };
    openRowPanelTransition({ detail, selection }, writeRoute, baseRoute, row, col, preview);

    expect(detail.store.get().rowDrawer).toBe(row);
    expect(detail.store.get().rowDrawerPreview).toBe(preview);
    expect(selection.store.get().selectedColumnId).toBe('c1');
    expect(writeRoute).toHaveBeenCalledTimes(1);
    expect(writeRoute).toHaveBeenCalledWith({
      ...baseRoute,
      panel: { kind: 'row', rowId: 'r1', columnId: 'c1' },
    });
  });

  it('openRowPanelTransition: an omitted column clears selectedColumnId (not just leaves it) and the route panel carries no columnId', () => {
    const detail = createDetailStore();
    const selection = createSelectionStore();
    selection.setSelectedColumnId('stale-column');
    const writeRoute = vi.fn<(next: RouteState) => void>();
    const row = { id: 'r2' } as unknown as Row;

    openRowPanelTransition({ detail, selection }, writeRoute, baseRoute, row);

    expect(selection.store.get().selectedColumnId).toBeNull();
    expect(writeRoute).toHaveBeenCalledWith({
      ...baseRoute,
      panel: { kind: 'row', rowId: 'r2', columnId: undefined },
    });
  });

  it('openColumnPanelTransition: opens the column drawer, clears the selected column, and writes the /column route panel', () => {
    const detail = createDetailStore();
    const selection = createSelectionStore();
    selection.setSelectedColumnId('c-old');
    const writeRoute = vi.fn<(next: RouteState) => void>();
    const col = { id: 'c2' } as unknown as ColumnDef;

    // = production's openColumnPanel (useWorkspaceModel.tsx).
    openColumnPanelTransition({ detail, selection }, writeRoute, baseRoute, col);

    expect(detail.store.get().columnDrawer).toBe(col);
    expect(selection.store.get().selectedColumnId).toBeNull();
    expect(writeRoute).toHaveBeenCalledWith({ ...baseRoute, panel: { kind: 'column', columnId: 'c2' } });
  });

  it('openRowRefTransition: writes the cross-sheet row route FIRST, then sets the grid selection — order preserved (opposite of openRowPanelTransition/openColumnPanelTransition)', () => {
    const selection = createSelectionStore();
    const writeRoute = vi.fn<(next: RouteState) => void>();
    // vi.spyOn (unlike vi.fn) calls through to the real implementation by
    // default, so selection's actual state still updates while call order
    // is tracked via .mock.invocationCallOrder.
    const setSelectedRowsSpy = vi.spyOn(selection, 'setSelectedRows');

    // = production's openRowRef (useWorkspaceModel.tsx) — a cross-sheet deep
    // link, so it builds the full RouteState itself rather than starting
    // from the caller's current-sheet routeStateBase().
    openRowRefTransition({ selection }, writeRoute, 'p1', 42, 7);

    expect(writeRoute).toHaveBeenCalledWith({
      projectId: 'p1',
      sheetId: '42',
      actionKind: null,
      review: false,
      panel: { kind: 'row', rowId: '7' },
    });
    expect(selection.store.get().selectedRows).toEqual({
      sheetId: '42',
      rowIds: ['7'],
      rowIndexes: [],
    });
    expect(setSelectedRowsSpy).toHaveBeenCalledWith({ sheetId: '42', rowIds: ['7'], rowIndexes: [] });
    expect(writeRoute.mock.invocationCallOrder[0]).toBeLessThan(
      setSelectedRowsSpy.mock.invocationCallOrder[0],
    );
  });

  it('closeRoutePanelTransition: closes every Detail drawer, clears the selected column, then writes the route back to its panel-less base', () => {
    const detail = createDetailStore();
    const selection = createSelectionStore();
    const seedRow = { id: '1' } as unknown as Row;
    detail.openRow(seedRow);
    selection.setSelectedColumnId('c1');
    const writeRoute = vi.fn<(next: RouteState) => void>();

    // = production's closeRoutePanel (useWorkspaceModel.tsx).
    closeRoutePanelTransition({ detail, selection }, writeRoute, baseRoute);

    expect(detail.store.get().rowDrawer).toBeNull();
    expect(detail.store.get().columnDrawer).toBeNull();
    expect(selection.store.get().selectedColumnId).toBeNull();
    expect(writeRoute).toHaveBeenCalledWith(baseRoute);
  });

  it('confirmDeleteRowsTransition: clears the row selection for the deleted sheet AND closes the drawers with childFilter cleared', () => {
    const selection = createSelectionStore();
    const detail = createDetailStore();
    selection.setSelectedRows({ sheetId: 's1', rowIds: ['1', '2'], rowIndexes: [0, 1] });
    const seedRow = { id: '1' } as unknown as Row;
    detail.openRow(seedRow);
    detail.setChildFilter({
      sheetId: 's1',
      parentSheetName: 'p',
      parentRowId: '1',
      parentRowIndex: 0,
      count: 3,
    });

    // = production's confirmDeleteRows delete-succeeded .then() body
    // (useWorkspaceModel.tsx) — updateData/refreshSheets/refreshHistory stay
    // bind-layer-only around this call (not WorkspaceStores members).
    confirmDeleteRowsTransition({ selection, detail }, 's1');

    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
    expect(detail.store.get().rowDrawer).toBeNull();
    expect(detail.store.get().childFilter).toBeNull();
  });

  it('applyLensTransition: resets the grid filter/sort for lens entry, closes the header menu, clears any stale lens-open error, and records the resolved lens view', () => {
    const gridView = createGridViewStore();
    const detail = createDetailStore();
    const lensView = createLensViewStore();
    gridView.applyFilter({ filter: { status: { eq: 'open' } } });
    const seedColumn = { id: 'c1' } as unknown as ColumnDef;
    detail.openHeaderMenu(
      { column: seedColumn, columnIndex: 0, bounds: { x: 0, y: 0, width: 1, height: 1 } },
    );
    lensView.setLensOpenError('This view is out of date — refresh the index before opening it.');

    // = production's applyLens post-resolve composition (useWorkspaceModel.tsx,
    // whose body literally comments "Cross-domain transition").
    applyLensTransition(
      { gridView, detail, lensView },
      { lensId: 9, name: 'my lens', sheetId: 's1', rowIds: [1, 2, 3], scores: {}, total: 3 },
    );

    expect(gridView.store.get().applied.filter).toBeNull();
    expect(detail.store.get().headerMenu).toBeNull();
    expect(lensView.store.get().lensOpenError).toBeNull();
    expect(lensView.store.get().lensView).toEqual({
      lensId: 9,
      name: 'my lens',
      sheetId: 's1',
      rowIds: [1, 2, 3],
      scores: {},
      total: 3,
    });
  });
});

describe('invariant 10b closure — no multi-store handler sequence survives outside this module (grep-shaped, precedent: core/route/writeCutover.test.ts)', () => {
  // Cross-store compositions go through state/workspaceTransitions.ts
  // so the composition is single-sourced. This scan pins the NEGATIVE half:
  // useWorkspaceModel.tsx/App.tsx — the two files with direct handles onto
  // more than one workspace store — must not have any statement-adjacent
  // pair of calls onto TWO DIFFERENT store handles left inline.
  //
  // "Adjacent" = two store-handle call lines (`storeName.method(...)`,
  // trimmed, comments excluded) that are the SAME line or one line apart
  // (a blank/other statement line between them still counts — the six sites
  // this sweep fixed were all zero-gap; calibration below found this window
  // catches every real composition with zero false positives). A WIDER
  // window (checked during authoring, not asserted here) also flags
  // unrelated sibling JSX callback props that each fire exactly one store
  // call each (e.g. SheetGrid's `onColumnGroupsChange`/`onSelectedRowsChange`
  // — two independent one-line handlers, not a composed sequence) — that is
  // a real false-positive shape for a purely textual scan, which is why the
  // window stays tight rather than wide.
  //
  // Store handle names scanned: every WorkspaceStores member with a plain
  // (non-`stores.`) local binding in these two files — gridView, selection,
  // detail, savedViews, workView, chrome, route, pluginLayout, actSurface.
  // `route.*` is intentionally included even though the real route write
  // path is the separate `writeRoute(...)` wrapper (core/route/
  // writeCutover.test.ts already pins that path) — this scan only concerns
  // itself with the store-handle-dot-method shape.

  const here = dirname(fileURLToPath(import.meta.url));
  const srcRoot = resolve(here, '..'); // web/src
  const read = (rel: string): string => readFileSync(resolve(srcRoot, rel), 'utf8');

  const STORE_NAMES = [
    'gridView',
    'selection',
    'detail',
    'savedViews',
    'workView',
    'compareView',
    'chrome',
    'route',
    'pluginLayout',
    'actSurface',
  ];
  const callLineRe = new RegExp(`^(${STORE_NAMES.join('|')})\\.[a-zA-Z]+\\(`);
  const isCommentLine = (l: string): boolean =>
    l.startsWith('//') || l.startsWith('*') || l.startsWith('/*');

  // No known-safe exceptions exist today (the sweep's six sites were the
  // last ones) — kept as an explicit empty allowlist, matching writeCutover
  // .test.ts's ALLOW-map shape, so a future genuinely-safe exception has a
  // documented place to go instead of widening the scan's window.
  const ALLOW: Record<string, string[]> = {
    'workspace/useWorkspaceModel.tsx': [],
    'App.tsx': [],
  };

  function adjacentCrossStoreOffenders(file: string, allow: string[]): string[] {
    const lines = read(file).split('\n');
    const calls: { line: number; store: string; text: string }[] = [];
    lines.forEach((raw, i) => {
      const l = raw.trim();
      if (isCommentLine(l)) return;
      const m = l.match(callLineRe);
      if (m) calls.push({ line: i + 1, store: m[1], text: l });
    });
    const offenders: string[] = [];
    for (let i = 0; i < calls.length - 1; i++) {
      const a = calls[i];
      const b = calls[i + 1];
      if (b.line - a.line <= 2 && a.store !== b.store) {
        const pair = `${file}:${a.line}-${b.line}: ${a.text} / ${b.text}`;
        if (!allow.includes(pair)) offenders.push(pair);
      }
    }
    return offenders;
  }

  for (const [file, allow] of Object.entries(ALLOW)) {
    it(`${file}: no two adjacent lines call different workspace store handles directly`, () => {
      expect(adjacentCrossStoreOffenders(file, allow)).toEqual([]);
    });
  }

  it('the six swept call sites now call a *Transition helper, not the raw store pair, at their production call sites', () => {
    const model = read('workspace/useWorkspaceModel.tsx');
    expect(model).toMatch(/openRowPanelTransition\(\{ detail, selection \}/);
    expect(model).toMatch(/openColumnPanelTransition\(\{ detail, selection \}/);
    expect(model).toMatch(/openRowRefTransition\(\{ selection \}/);
    expect(model).toMatch(/closeRoutePanelTransition\(\{ detail, selection \}/);
    expect(model).toMatch(/confirmDeleteRowsTransition\(\{ selection, detail \}/);
    expect(model).toMatch(/applyLensTransition\(\s*\{ gridView, detail, lensView: lensViewHandle \}/);
  });
});
