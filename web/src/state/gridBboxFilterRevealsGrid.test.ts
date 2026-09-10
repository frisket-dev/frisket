// The host-side `grid.filter.applyBbox` capability (useWorkspaceModel.tsx's
// gridFilter handle -> applyGridBboxFilter) applies a bbox grid filter the user cannot
// see when the map is the active MAIN view (a promoted map tab renders
// full-window WITHOUT the grid — App.tsx's WorkspaceMainViewBody: "A promoted
// view renders full-window", keyed off useWorkspaceModel.tsx's
// activePromotedView = promotedViews entry matching workView's
// activePromotedKey). Applying a grid filter expresses intent to SEE the
// grid, so the host must also reveal the grid surface. Host-side and
// plugin-agnostic: the geo plugin package must not change.
//
// The composition is cross-store (gridView/detail/selection plus workView/chrome
// layout state). The workspace-transition invariant requires
// every such composition to live as ONE exported *Transition function in
// state/workspaceTransitions.ts that production calls. So this suite freezes:
//
//   applyGridBboxFilterTransition(
//     stores,   // { gridView, detail, selection, workView } store handles
//     chrome,   // PersistedChromeWrites: { setOpenSplit, setDocumentView, setPromotedViews }
//     layout,   // current layout reads, plain data (closePromotedTabTransition
//               // precedent): { promotedViews, activePromotedKey }
//     sheetId,
//     filter,   // the same GridViewFilterApplication applyGridFilterTransition takes
//   ): void
//
// DONE = the transition exists, production's applyGridBboxFilter calls it, and:
//  (1) it applies the bbox filter with the SAME detail/selection clears the
//      plain applyGridFilterTransition performs (no parity regression);
//  (2) called while a promoted map tab hides the grid, it reveals the grid —
//      afterwards no promotedViews entry matches workView's activePromotedKey,
//      so the full-window promoted surface stands down and the grid renders
//      (whether the map additionally stays visible as the work.companion
//      split is the implementer's choice; this suite does not pin it);
//  (3) called while the grid is already visible, it leaves the layout alone.
//
// If the reveal is implemented at a different layer (e.g. render-side in
// App.tsx) making this state-level contract unsatisfiable, fix the contract's
// layer boundary; do not weaken these assertions.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { createGridViewStore } from './gridViewStore';
import { createSelectionStore } from './selectionStore';
import { createDetailStore } from './detailStore';
import { createWorkViewStore } from './workViewStore';
import { createChromeStore } from './chromeStore';
import * as workspaceTransitions from './workspaceTransitions';
import { emptySelectedRows } from '../workspace/workspaceState';
import type { GridFilterSpec, Row } from '../api/open';
import type { PromotedView } from './chromeStore';

type GridViewHandle = ReturnType<typeof createGridViewStore>;
type SelectionHandle = ReturnType<typeof createSelectionStore>;
type DetailHandle = ReturnType<typeof createDetailStore>;
type WorkViewHandle = ReturnType<typeof createWorkViewStore>;
type ChromeHandle = ReturnType<typeof createChromeStore>;

type ApplyGridBboxFilterTransition = (
  stores: {
    gridView: GridViewHandle;
    detail: DetailHandle;
    selection: SelectionHandle;
    workView: WorkViewHandle;
  },
  chrome: Pick<ChromeHandle, 'setOpenSplit' | 'setDocumentView' | 'setPromotedViews'>,
  layout: { promotedViews: PromotedView[]; activePromotedKey: string | null },
  sheetId: string,
  filter: {
    column: string;
    operator: string;
    value: string;
    start: string;
    end: string;
    filter: GridFilterSpec;
  },
) => void;

// Accessed dynamically (not a named import) so a still-missing export fails
// the typeof assertions below SEMANTICALLY instead of failing module
// evaluation — the same "semantic red, not collection error" discipline as
// the pytest admission gate.
const applyGridBboxFilterTransition = (
  workspaceTransitions as unknown as Record<string, unknown>
)['applyGridBboxFilterTransition'] as ApplyGridBboxFilterTransition | undefined;

const BBOX_FILTER: GridFilterSpec = {
  geo: { bbox: { min_lon: 0, min_lat: 0, max_lon: 1, max_lat: 1 } },
};

function bboxApplication() {
  // Same application shape production's applyGridBboxFilter builds today
  // (useWorkspaceModel.tsx — the bbox variant of applyGridFilterTransition's
  // arguments, pinned by workspaceTransitions.test.ts's bbox parity test).
  return {
    column: 'geo',
    operator: 'bbox',
    value: '',
    start: '',
    end: '',
    filter: BBOX_FILTER,
  };
}

function seedStores(projectId: string) {
  const gridView = createGridViewStore();
  const detail = createDetailStore();
  const selection = createSelectionStore();
  const workView = createWorkViewStore();
  const chrome = createChromeStore(projectId);
  // Seed the detail/selection state every filter apply must clear (the same
  // seeds workspaceTransitions.test.ts uses for filter parity).
  selection.setSelectedRows({ sheetId: 's1', rowIds: ['1', '2'], rowIndexes: [0, 1] });
  detail.openRow({ id: '1' } as unknown as Row);
  return { gridView, detail, selection, workView, chrome };
}

/** The production grid-visibility derivation this suite pins, by referent:
 *  useWorkspaceModel.tsx's activePromotedView (the promotedViews entry whose
 *  key === workView.activePromotedKey for the current sheet) is rendered
 *  full-window INSTEAD of the grid (App.tsx's WorkspacePrimarySurface /
 *  "A promoted view renders full-window — no grid toolbar"). No matching
 *  entry -> the grid surface renders. */
function activePromotedView(
  chrome: ChromeHandle,
  workView: WorkViewHandle,
  sheetId: string,
): PromotedView | null {
  const key = workView.store.get().activePromotedKey;
  if (!key) return null;
  return (
    chrome.store.get().promotedViews.find(
      (view) => view.key === key && view.sheetId === sheetId,
    ) ?? null
  );
}

describe('grid.filter.applyBbox reveals the grid (grid-filter-apply-reveals-grid-v1)', () => {
  it('exports applyGridBboxFilterTransition — the cross-store composition the host capability calls', () => {
    expect(
      typeof applyGridBboxFilterTransition,
      'state/workspaceTransitions.ts must export applyGridBboxFilterTransition — ' +
        'the grid.filter.applyBbox capability composition that applies the bbox ' +
        'filter AND reveals the grid surface; a filter applied from a map-only ' +
        'main view must not change state the user cannot see',
    ).toBe('function');
  });

  it('map promoted tab is the active main view (grid hidden): applying the bbox filter applies it AND reveals the grid', () => {
    expect(typeof applyGridBboxFilterTransition).toBe('function');
    const { gridView, detail, selection, workView, chrome } = seedStores('p-bbox-reveal');
    const mapTab: PromotedView = {
      key: 'map:s1:c-geo',
      sheetId: 's1',
      kind: 'map',
      columnId: 'c-geo',
      label: 'Map of places',
    };
    chrome.setPromotedViews([mapTab]);
    workView.setActivePromotedKey(mapTab.key);
    // Precondition: the grid is NOT visible — the promoted map tab renders
    // full-window in its place.
    expect(activePromotedView(chrome, workView, 's1')).toEqual(mapTab);

    applyGridBboxFilterTransition!(
      { gridView, detail, selection, workView },
      chrome,
      {
        promotedViews: chrome.store.get().promotedViews,
        activePromotedKey: workView.store.get().activePromotedKey,
      },
      's1',
      bboxApplication(),
    );

    // (a) The filter is applied, with the same detail/selection clears as the
    // plain applyGridFilterTransition (parity — no regression of the existing
    // composition).
    expect(gridView.store.get().applied.filter).toEqual(BBOX_FILTER);
    expect(detail.store.get().rowDrawer).toBeNull();
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));

    // (b) THE MISSING BEHAVIOR: the grid is revealed — the full-window
    // promoted map surface no longer hides it.
    expect(
      activePromotedView(chrome, workView, 's1'),
      'applying a bbox grid filter while a promoted map tab renders full-window ' +
        'must reveal the grid surface (the user just expressed intent to see ' +
        'the filtered grid) — the promoted tab must stop hiding it',
    ).toBeNull();

    // The other grid-replacing surfaces stay stood down (sanity: the reveal
    // must not swap one grid-hiding surface for another).
    expect(chrome.store.get().documentView).toBeNull();
    expect(workView.store.get().answersViewSheetId).toBeNull();
  });

  it('grid already visible: applies the filter and leaves the layout alone', () => {
    expect(typeof applyGridBboxFilterTransition).toBe('function');
    const { gridView, detail, selection, workView, chrome } = seedStores('p-bbox-noop');
    // No promoted tab, no split — the plain grid layout.
    expect(activePromotedView(chrome, workView, 's1')).toBeNull();
    expect(chrome.store.get().openSplit).toBeNull();

    applyGridBboxFilterTransition!(
      { gridView, detail, selection, workView },
      chrome,
      { promotedViews: [], activePromotedKey: null },
      's1',
      bboxApplication(),
    );

    expect(gridView.store.get().applied.filter).toEqual(BBOX_FILTER);
    expect(selection.store.get().selectedRows).toEqual(emptySelectedRows('s1'));
    // No gratuitous layout churn when the grid is already on screen.
    expect(workView.store.get().activePromotedKey).toBeNull();
    expect(chrome.store.get().openSplit).toBeNull();
    expect(chrome.store.get().promotedViews).toEqual([]);
  });

  it('production wiring: useWorkspaceModel.tsx routes the applyBbox path through applyGridBboxFilterTransition', () => {
    // Grep-shaped production pin (precedent: workspaceTransitions.test.ts's
    // adjacent-statement scan; core/route/writeCutover.test.ts): the state
    // tests above prove the transition's behavior, this proves production's
    // grid.filter.applyBbox path actually CALLS it — implementing the
    // function without wiring the capability would leave the bug live.
    const here = dirname(fileURLToPath(import.meta.url));
    const source = readFileSync(
      resolve(here, '../workspace/useWorkspaceModel.tsx'),
      'utf-8',
    );
    expect(
      /applyGridBboxFilterTransition\(/.test(source),
      "useWorkspaceModel.tsx's applyGridBboxFilter (the grid.filter.applyBbox " +
        'capability body) must call applyGridBboxFilterTransition so the bbox ' +
        'apply and the grid reveal are one transaction',
    ).toBe(true);
  });
});
