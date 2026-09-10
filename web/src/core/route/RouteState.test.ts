// The RouteState codec + structural equality. The codec round-trip
// is anchored on routes.ts's REAL formatter (routePath) — RouteState narrows
// routes.ts's RoutePanel (which still carries map/graph) and must format back
// to the exact same URL strings the app's history layer produces.

import { describe, expect, it } from 'vitest';
import type { RoutePanel } from '../../routes';
import { routePath } from '../../routes';
import {
  narrowPanel,
  panelsEqual,
  routeStateToRoute,
  routeStatesEqual,
  type RouteState,
} from './RouteState';

const base: RouteState = {
  projectId: 'p1',
  sheetId: null,
  actionKind: null,
  review: false,
  panel: null,
};

describe('narrowPanel — map/graph are structurally excluded', () => {
  it('drops map to null (hydrated as an entry point, not carried)', () => {
    expect(narrowPanel({ kind: 'map', columnId: 'c1' })).toBeNull();
  });
  it('drops graph to null', () => {
    expect(narrowPanel({ kind: 'graph' })).toBeNull();
  });
  it('undefined → null', () => {
    expect(narrowPanel(undefined)).toBeNull();
  });
  it('preserves row (with optional columnId)', () => {
    expect(narrowPanel({ kind: 'row', rowId: 'r1' })).toEqual({ kind: 'row', rowId: 'r1', columnId: undefined });
    expect(narrowPanel({ kind: 'row', rowId: 'r1', columnId: 'c1' })).toEqual({ kind: 'row', rowId: 'r1', columnId: 'c1' });
  });
  it('preserves column', () => {
    expect(narrowPanel({ kind: 'column', columnId: 'c1' })).toEqual({ kind: 'column', columnId: 'c1' });
  });
  it('preserves sourceHealth', () => {
    expect(narrowPanel({ kind: 'sourceHealth', sourceId: 's1' })).toEqual({ kind: 'sourceHealth', sourceId: 's1' });
  });
});

describe('panelsEqual', () => {
  it('null === null', () => expect(panelsEqual(null, null)).toBe(true));
  it('null !== row', () => expect(panelsEqual(null, { kind: 'row', rowId: 'r1' })).toBe(false));
  it('same row === same row', () =>
    expect(panelsEqual({ kind: 'row', rowId: 'r1', columnId: 'c1' }, { kind: 'row', rowId: 'r1', columnId: 'c1' })).toBe(true));
  it('row differing columnId !==', () =>
    expect(panelsEqual({ kind: 'row', rowId: 'r1', columnId: 'c1' }, { kind: 'row', rowId: 'r1', columnId: 'c2' })).toBe(false));
  it('row !== column of same id', () =>
    expect(panelsEqual({ kind: 'row', rowId: 'x' }, { kind: 'column', columnId: 'x' })).toBe(false));
  it('sourceHealth differing id !==', () =>
    expect(panelsEqual({ kind: 'sourceHealth', sourceId: 'a' }, { kind: 'sourceHealth', sourceId: 'b' })).toBe(false));
});

describe('routeStatesEqual — structural snapshot equality (C3)', () => {
  it('equal snapshots (different object refs) are equal', () => {
    const a: RouteState = { ...base, sheetId: 's1', panel: { kind: 'row', rowId: 'r1' } };
    const b: RouteState = { ...base, sheetId: 's1', panel: { kind: 'row', rowId: 'r1' } };
    expect(a).not.toBe(b);
    expect(routeStatesEqual(a, b)).toBe(true);
  });
  it('differs on projectId', () => expect(routeStatesEqual(base, { ...base, projectId: 'p2' })).toBe(false));
  it('differs on sheetId', () => expect(routeStatesEqual(base, { ...base, sheetId: 's1' })).toBe(false));
  it('differs on actionKind', () => expect(routeStatesEqual(base, { ...base, actionKind: 'geocode' })).toBe(false));
  it('differs on review', () => expect(routeStatesEqual(base, { ...base, review: true })).toBe(false));
  it('differs on panel', () =>
    expect(routeStatesEqual(base, { ...base, panel: { kind: 'row', rowId: 'r1' } })).toBe(false));
});

describe('routeStateToRoute + routePath — codec round-trip against routes.ts real formatter', () => {
  const cases: Array<{ name: string; state: RouteState; url: string }> = [
    { name: 'bare project', state: base, url: '/p/p1' },
    { name: 'sheet', state: { ...base, sheetId: 's1' }, url: '/p/p1/s/s1' },
    { name: 'action', state: { ...base, sheetId: 's1', actionKind: 'geocode' }, url: '/p/p1/s/s1/action/geocode' },
    { name: 'review', state: { ...base, sheetId: 's1', review: true }, url: '/p/p1/s/s1/review' },
    { name: 'row', state: { ...base, sheetId: 's1', panel: { kind: 'row', rowId: 'r1' } }, url: '/p/p1/s/s1/row/r1' },
    {
      name: 'row+column',
      state: { ...base, sheetId: 's1', panel: { kind: 'row', rowId: 'r1', columnId: 'c1' } },
      url: '/p/p1/s/s1/row/r1/column/c1',
    },
    { name: 'column', state: { ...base, sheetId: 's1', panel: { kind: 'column', columnId: 'c1' } }, url: '/p/p1/s/s1/column/c1' },
    {
      name: 'sourceHealth',
      state: { ...base, sheetId: 's1', panel: { kind: 'sourceHealth', sourceId: 'src1' } },
      url: '/p/p1/s/s1/source/src1/health',
    },
  ];
  for (const { name, state, url } of cases) {
    it(`${name} → ${url}`, () => {
      expect(routePath(routeStateToRoute(state))).toBe(url);
    });
  }
});

describe('narrowing a routes.ts RoutePanel fixture (the parser output shape) into RouteState.panel', () => {
  // The shape routes.ts's parseParts emits. narrowPanel is the parse-side
  // half: routes.ts owns URL→RoutePanel; RouteState narrows it.
  const parsed: Array<[RoutePanel | undefined, RouteState['panel']]> = [
    [{ kind: 'row', rowId: 'r1', columnId: 'c1' }, { kind: 'row', rowId: 'r1', columnId: 'c1' }],
    [{ kind: 'column', columnId: 'c1' }, { kind: 'column', columnId: 'c1' }],
    [{ kind: 'sourceHealth', sourceId: 's1' }, { kind: 'sourceHealth', sourceId: 's1' }],
    [{ kind: 'map', columnId: 'c1' }, null],
    [{ kind: 'graph' }, null],
    [undefined, null],
  ];
  for (const [input, expected] of parsed) {
    it(`${JSON.stringify(input)} → ${JSON.stringify(expected)}`, () => {
      expect(narrowPanel(input)).toEqual(expected);
    });
  }
});
