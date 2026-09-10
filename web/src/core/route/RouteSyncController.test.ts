// Two things under test:
//   1. pushVsReplace — the EXPANDED push/replace matrix: sheet select, panel
//      open/close, actionKind set/change/clear, review open/close, sourceHealth,
//      row→row identity change.
//   2. createRouteSyncController — the store→URL write path, incl. the
//      structural-equality re-entry guard (this test proves a write-induced
//      synthetic-popstate echo of the SAME logical route causes exactly ONE
//      write, not two).

import { describe, expect, it, vi } from 'vitest';
import type { RouteState } from './RouteState';
import { createRouteStore } from '../../state/routeStore';
import { projectRouteState, type ChromeHandle } from './projectRouteState';
import { createRouteSyncController, pushVsReplace } from './RouteSyncController';

const base: RouteState = {
  projectId: 'p1',
  sheetId: 's1',
  actionKind: null,
  review: false,
  panel: null,
};

describe('pushVsReplace — expanded matrix', () => {
  const cases: Array<{ name: string; prev: RouteState; next: RouteState; mode: 'push' | 'replace' }> = [
    { name: 'sheet select (sheetId change)', prev: base, next: { ...base, sheetId: 's2' }, mode: 'push' },
    { name: 'open row panel (null→row)', prev: base, next: { ...base, panel: { kind: 'row', rowId: 'r1' } }, mode: 'push' },
    { name: 'open column panel (null→column)', prev: base, next: { ...base, panel: { kind: 'column', columnId: 'c1' } }, mode: 'push' },
    { name: 'open sourceHealth (null→sourceHealth)', prev: base, next: { ...base, panel: { kind: 'sourceHealth', sourceId: 'src1' } }, mode: 'push' },
    { name: 'close row panel (row→null)', prev: { ...base, panel: { kind: 'row', rowId: 'r1' } }, next: base, mode: 'replace' },
    { name: 'close column panel (column→null)', prev: { ...base, panel: { kind: 'column', columnId: 'c1' } }, next: base, mode: 'replace' },
    { name: 'close sourceHealth (sourceHealth→null)', prev: { ...base, panel: { kind: 'sourceHealth', sourceId: 'src1' } }, next: base, mode: 'replace' },
    { name: 'row A → row B (identity change)', prev: { ...base, panel: { kind: 'row', rowId: 'A' } }, next: { ...base, panel: { kind: 'row', rowId: 'B' } }, mode: 'push' },
    { name: 'actionKind set (null→set)', prev: base, next: { ...base, actionKind: 'geocode' }, mode: 'push' },
    { name: 'actionKind change (set→other)', prev: { ...base, actionKind: 'enrich.geocode' }, next: { ...base, actionKind: 'map.translate' }, mode: 'push' },
    { name: 'actionKind clear (set→null)', prev: { ...base, actionKind: 'geocode' }, next: base, mode: 'replace' },
    { name: 'review open (false→true)', prev: base, next: { ...base, review: true }, mode: 'push' },
    { name: 'review close (true→false)', prev: { ...base, review: true }, next: base, mode: 'replace' },
  ];
  for (const { name, prev, next, mode } of cases) {
    it(`${name} → ${mode}`, () => {
      expect(pushVsReplace(prev, next)).toBe(mode);
    });
  }

  it('sheetId change dominates a simultaneous panel close (sheet switch is push)', () => {
    // selectSheet today navigate()s (push) even when it also drops a drawer.
    const prev: RouteState = { ...base, sheetId: 's1', panel: { kind: 'row', rowId: 'r1' } };
    const next: RouteState = { ...base, sheetId: 's2', panel: null };
    expect(pushVsReplace(prev, next)).toBe('push');
  });
});

describe('createRouteSyncController — classified write path', () => {
  const deps = (initial: RouteState) => {
    const route = createRouteStore(initial.projectId);
    route.projectExternal(initial);
    const write = vi.fn<(next: RouteState, mode: 'push' | 'replace') => void>();
    const ctrl = createRouteSyncController({ routeStore: route, write });
    return { route, write, ctrl };
  };

  it('navigate writes once with the unchanged pushVsReplace matrix', () => {
    const { route, write } = deps(base);
    route.navigate({ ...base, panel: { kind: 'row', rowId: 'r1' } });
    expect(write).toHaveBeenCalledTimes(1);
    expect(write).toHaveBeenCalledWith(
      { ...base, panel: { kind: 'row', rowId: 'r1' } },
      'push',
    );
  });

  it('navigate accepts an explicit history mode without changing matrix cases', () => {
    const { route, write } = deps(base);
    route.navigate({ ...base, panel: { kind: 'row', rowId: 'r1' } }, 'replace');
    expect(write).toHaveBeenCalledWith(
      { ...base, panel: { kind: 'row', rowId: 'r1' } },
      'replace',
    );
  });

  it('projectExternal writes history zero times, including a real external change', () => {
    const { route, write } = deps(base);
    route.projectExternal({ ...base, sheetId: 's2' });
    expect(route.store.get().sheetId).toBe('s2');
    expect(write).not.toHaveBeenCalled();
  });

  it('normalize writes exactly one replace', () => {
    const { route, write } = deps(base);
    route.normalize({ ...base, sheetId: 's2' });
    expect(write).toHaveBeenCalledTimes(1);
    expect(write).toHaveBeenCalledWith({ ...base, sheetId: 's2' }, 'replace');
  });

  it('a structural-equality projection echo causes no second write', () => {
    const { route, write } = deps(base);
    const rowState: RouteState = { ...base, panel: { kind: 'row', rowId: 'r1' } };
    route.navigate(rowState);
    route.projectExternal({ ...rowState, panel: { kind: 'row', rowId: 'r1' } });
    expect(write).toHaveBeenCalledTimes(1);
  });

  it('dispose detaches the sole history writer', () => {
    const { route, write, ctrl } = deps(base);
    ctrl.dispose();
    route.navigate({ ...base, panel: { kind: 'row', rowId: 'r1' } });
    expect(write).not.toHaveBeenCalled();
  });
});

describe('projectRouteState — map/graph deep-link hydration', () => {
  const chrome = (): { chrome: ChromeHandle; calls: unknown[] } => {
    const calls: unknown[] = [];
    return { chrome: { hydrateOpenSplit: (spec) => calls.push(spec) }, calls };
  };

  it('row panel: no hydrate, no normalize write; panel preserved', () => {
    const { chrome: c, calls } = chrome();
    const write = vi.fn();
    const rs = projectRouteState(
      { projectId: 'p1', routeSheetId: 's1', routeActionKind: null, routeReview: false, routePanel: { kind: 'row', rowId: 'r1' } },
      c,
      write,
    );
    expect(calls).toHaveLength(0);
    expect(write).not.toHaveBeenCalled();
    expect(rs.panel).toEqual({ kind: 'row', rowId: 'r1', columnId: undefined });
  });

  it('map deep-link: hydrates openSplit then normalizes with replace; base panel is null', () => {
    const { chrome: c, calls } = chrome();
    const write = vi.fn();
    const rs = projectRouteState(
      { projectId: 'p1', routeSheetId: 's1', routeActionKind: null, routeReview: false, routePanel: { kind: 'map', columnId: 'geo' } },
      c,
      write,
    );
    expect(calls).toEqual([{ kind: 'map', sheetId: 's1', columnId: 'geo' }]);
    expect(write).toHaveBeenCalledTimes(1);
    expect(write.mock.calls[0][1]).toBe('replace');
    expect(rs.panel).toBeNull();
  });

  it('graph deep-link: hydrates openSplit then normalizes with replace', () => {
    const { chrome: c, calls } = chrome();
    const write = vi.fn();
    const rs = projectRouteState(
      { projectId: 'p1', routeSheetId: 's1', routeActionKind: null, routeReview: false, routePanel: { kind: 'graph' } },
      c,
      write,
    );
    expect(calls).toEqual([{ kind: 'graph', sheetId: 's1' }]);
    expect(write).toHaveBeenCalledTimes(1);
    expect(write.mock.calls[0][1]).toBe('replace');
    expect(rs.panel).toBeNull();
  });
});
