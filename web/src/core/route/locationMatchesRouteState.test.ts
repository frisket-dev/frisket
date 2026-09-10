// The semantic idempotence helpers. These pin the exact bug the raw
// `pathname === routePath(next)`
// guard had: a non-canonical URL spelling that MEANS `next` (trailing slash,
// %7E vs ~, upper vs lower percent-escapes, empty segments) must be recognized as
// idempotent — else Back gets misread as a command and the controller re-pushes
// DURING Back, corrupting history. The tests drive routes.ts's REAL parser
// (parsePathname), so they exercise routeContext.ts's real decode path.

import { describe, expect, it } from 'vitest';
import { parsePathname } from '../../routes';
import {
  locationMatchesRouteState,
  propsMatchPathname,
  routePanelsEqual,
  routeToRouteState,
  type RouteSignificantProps,
} from './locationMatchesRouteState';
import type { RouteState } from './RouteState';

const base: RouteState = {
  projectId: 'p1',
  sheetId: 's1',
  actionKind: null,
  review: false,
  panel: null,
};

const match = (pathname: string, next: RouteState): boolean =>
  locationMatchesRouteState(pathname, next, parsePathname);

// Pin the encodeURIComponent behavior the helper relies on, so a future Node/
// spec change that alters it fails HERE with a clear message rather than silently
// breaking idempotence.
describe('encodeURIComponent baseline (pinned)', () => {
  it('leaves ~ unescaped (routePath emits a literal ~)', () => {
    expect(encodeURIComponent('~')).toBe('~');
  });
  it('escapes [ to UPPERCASE %5B, and %5b decodes back to [', () => {
    expect(encodeURIComponent('[')).toBe('%5B');
    expect(decodeURIComponent('%5b')).toBe('[');
  });
  it('multi-byte é → %C3%A9, lower-case %c3%a9 decodes identically', () => {
    expect(encodeURIComponent('é')).toBe('%C3%A9');
    expect(decodeURIComponent('%c3%a9')).toBe('é');
  });
});

describe('locationMatchesRouteState — canonical spellings match', () => {
  it('exact canonical URL matches', () => {
    expect(match('/p/p1/s/s1', base)).toBe(true);
  });
  it('a different route does NOT match', () => {
    expect(match('/p/p1/s/s2', base)).toBe(false);
  });
  it('multi-segment panel (row + column) matches its canonical URL', () => {
    const next: RouteState = { ...base, panel: { kind: 'row', rowId: 'r1', columnId: 'c1' } };
    expect(match('/p/p1/s/s1/row/r1/column/c1', next)).toBe(true);
  });
  it('row-only vs row+column do NOT match', () => {
    expect(match('/p/p1/s/s1/row/r1', { ...base, panel: { kind: 'row', rowId: 'r1', columnId: 'c1' } })).toBe(false);
  });
});

describe('locationMatchesRouteState — non-canonical spellings still match (the fix)', () => {
  it('trailing slash', () => {
    expect(match('/p/p1/s/s1/', base)).toBe(true);
  });
  it('doubled / empty segments', () => {
    expect(match('/p/p1//s//s1', base)).toBe(true);
  });
  it('%7E in the URL vs a literal ~ id (encodeURIComponent leaves ~ unescaped)', () => {
    // sheetId '~' → routePath spells '/p/p1/s/~'; a URL carrying '%7E' means the
    // SAME sheet. Raw string equality would MISS this and re-push.
    const next: RouteState = { ...base, sheetId: '~' };
    expect(match('/p/p1/s/%7E', next)).toBe(true);
    expect(next.sheetId).toBe(decodeURIComponent('%7E'));
  });
  it('lower-case percent-escape %5b vs canonical %5B', () => {
    const next: RouteState = { ...base, panel: { kind: 'row', rowId: '[' } };
    expect(match('/p/p1/s/s1/row/%5b', next)).toBe(true);
  });
  it('lower-case multi-byte escape %c3%a9 vs canonical %C3%A9', () => {
    const next: RouteState = { ...base, panel: { kind: 'column', columnId: 'é' } };
    expect(match('/p/p1/s/s1/column/%c3%a9', next)).toBe(true);
  });
});

describe('locationMatchesRouteState — B1 map/graph normalize still writes', () => {
  // A URL still carrying a map/graph panel is a transient ENTRY point, NOT a
  // steady RouteState. The normalize write commits `base` (panel dropped); the
  // guard must NOT treat the map/graph URL as already-idempotent with base, or
  // the panel would never leave the URL.
  it('a /map/column/X pathname does NOT match a base RouteState without the panel', () => {
    expect(match('/p/p1/s/s1/map/column/geo', base)).toBe(false);
  });
  it('a /graph pathname does NOT match a base RouteState without the panel', () => {
    expect(match('/p/p1/s/s1/graph', base)).toBe(false);
  });
});

describe('locationMatchesRouteState — non-workspace URLs never match', () => {
  it('picker URL does not match any RouteState', () => {
    expect(match('/', base)).toBe(false);
  });
  it('settings URL does not match', () => {
    expect(match('/settings/personal/profile', base)).toBe(false);
  });
});

describe('routeToRouteState', () => {
  it('project route narrows via narrowPanel', () => {
    expect(routeToRouteState({ kind: 'project', projectId: 'p1', sheetId: 's1', panel: { kind: 'row', rowId: 'r1' } })).toEqual({
      projectId: 'p1',
      sheetId: 's1',
      actionKind: null,
      review: false,
      panel: { kind: 'row', rowId: 'r1', columnId: undefined },
    });
  });
  it('map panel → null (transient entry point, forces normalize mismatch)', () => {
    expect(routeToRouteState({ kind: 'project', projectId: 'p1', sheetId: 's1', panel: { kind: 'map', columnId: 'c' } })).toBeNull();
  });
  it('graph panel → null', () => {
    expect(routeToRouteState({ kind: 'project', projectId: 'p1', sheetId: 's1', panel: { kind: 'graph' } })).toBeNull();
  });
  it('picker/settings/admin → null', () => {
    expect(routeToRouteState({ kind: 'picker' })).toBeNull();
    expect(routeToRouteState({ kind: 'settings', scope: 'personal', section: 'profile' })).toBeNull();
    expect(routeToRouteState({ kind: 'admin' })).toBeNull();
  });
});

describe('routePanelsEqual — full panel equality including map/graph', () => {
  it('both undefined', () => expect(routePanelsEqual(undefined, undefined)).toBe(true));
  it('map columnId matters', () => {
    expect(routePanelsEqual({ kind: 'map', columnId: 'a' }, { kind: 'map', columnId: 'a' })).toBe(true);
    expect(routePanelsEqual({ kind: 'map', columnId: 'a' }, { kind: 'map', columnId: 'b' })).toBe(false);
  });
  it('graph === graph', () => expect(routePanelsEqual({ kind: 'graph' }, { kind: 'graph' })).toBe(true));
  it('map !== graph', () => expect(routePanelsEqual({ kind: 'map', columnId: 'a' }, { kind: 'graph' })).toBe(false));
  it('row columnId matters', () => {
    expect(routePanelsEqual({ kind: 'row', rowId: 'r', columnId: 'c' }, { kind: 'row', rowId: 'r', columnId: 'c' })).toBe(true);
    expect(routePanelsEqual({ kind: 'row', rowId: 'r' }, { kind: 'row', rowId: 'r', columnId: 'c' })).toBe(false);
  });
});

describe('propsMatchPathname — projection staleness gate', () => {
  const propsFor = (partial: Partial<RouteSignificantProps>): RouteSignificantProps => ({
    projectId: 'p1',
    routeSheetId: 's1',
    routeActionKind: null,
    routeReview: false,
    routePanel: undefined,
    ...partial,
  });

  it('props describing the current location → project (true)', () => {
    expect(propsMatchPathname(propsFor({}), '/p/p1/s/s1', parsePathname)).toBe(true);
  });

  it('STALE props (URL already moved on) → bail (false)', () => {
    // Back#1 rendered props for /p/p1/s/s1/row/r1 but Back#2 already moved the URL
    // to /p/p1/s/s2. Projecting Back#1's props would desync the store from the URL.
    const stale = propsFor({ routePanel: { kind: 'row', rowId: 'r1' } });
    expect(propsMatchPathname(stale, '/p/p1/s/s2', parsePathname)).toBe(false);
  });

  it('map/graph deep link: props DO match the un-normalized URL → project (runs B1)', () => {
    const mapProps = propsFor({ routePanel: { kind: 'map', columnId: 'geo' } });
    expect(propsMatchPathname(mapProps, '/p/p1/s/s1/map/column/geo', parsePathname)).toBe(true);
  });

  it('non-canonical current URL still matches fresh props (trailing slash)', () => {
    expect(propsMatchPathname(propsFor({}), '/p/p1/s/s1/', parsePathname)).toBe(true);
  });

  it('a non-project current URL never matches project props', () => {
    expect(propsMatchPathname(propsFor({}), '/settings/personal/profile', parsePathname)).toBe(false);
  });
});
