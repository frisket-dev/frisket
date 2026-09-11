// Pins the single-ownership invariant the controller MOUNT completes:
//   1. The RouteSyncController is mounted exactly once per workspace (the bind
//      hook exists + imports createRouteSyncController + is called once in
//      useWorkspaceModel).
//   2. Zero remaining DIRECT workspace-route write sites: every navigate()/
//      replaceRoute() in useWorkspaceModel.tsx + App.tsx is either the single
//      controller write adapter (commitRoute) or an ALLOWLISTED app-shell
//      navigation. These cannot double-write with the controller, but NOT
//      because none is RouteState-representable — a bare project route IS
//      ({ projectId, sheetId: null }). They are safe for structural reasons:
//        - picker/settings navigate OUT of the workspace (they are genuinely not
//          a project-workspace RouteState);
//        - project-open (App.tsx: openHome helper) fires from the HomeScreen, OUTSIDE the
//          mounted workspace — there is no controller yet to double-write — and
//          switching projects UNMOUNTS/remounts the workspace + its controller
//          (keyed on route.projectId / state.project.id);
//        - the controller writes only in response to routeStore mutations and
//          never listens to popstate, so it cannot echo any of these navigations.
//      (grep-shaped.)
//   3. review-close's raw window.history.back() is preserved (the one call site
//      pushVsReplace never sees; kept, not forced through routeStore).
//   4. pushVsReplace matrix spot-subset is live.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { pushVsReplace } from './RouteSyncController';
import type { RouteState } from './RouteState';

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = resolve(here, '../..'); // web/src
const read = (rel: string): string => readFileSync(resolve(srcRoot, rel), 'utf8');

// Lines that legitimately contain navigate(/replaceRoute( AFTER the cutover:
//  - the controller's single write adapter (commitRoute → navigate/replaceRoute)
//  - app-shell navigations (picker/settings navigate OUT of the workspace;
//    project-open fires from the HomeScreen before the workspace controller
//    mounts, and a project switch unmounts/remounts it). The controller only
//    writes on routeStore mutations and never listens to popstate, so none of
//    these can double-write — regardless of whether the target is itself
//    RouteState-representable (a bare project route is).
const ALLOW: Record<string, string[]> = {
  'workspace/useWorkspaceModel.tsx': [
    "if (mode === 'replace') replaceRoute(nextRoute);",
    'else navigate(nextRoute);',
    "navigate({ kind: 'settings', projectId: project.id, scope: 'project', section: 'general' });",
    "navigate({ kind: 'settings', projectId: project.id, scope: 'project', section: 'notifications' });",
  ],
  'App.tsx': [
    "navigate({ kind: 'project', projectId: p.id });",
    "onClick={() => navigate({ kind: 'picker' })}",
  ],
};

const isCommentLine = (l: string): boolean =>
  l.startsWith('//') || l.startsWith('*') || l.startsWith('/*');

describe('route write-site cutover — single ownership', () => {
  for (const [file, allow] of Object.entries(ALLOW)) {
    it(`${file}: every navigate()/replaceRoute() is the controller adapter or an allowlisted app-shell nav`, () => {
      const offenders = read(file)
        .split('\n')
        .map((l) => l.trim())
        .filter((l) => !isCommentLine(l))
        // `route.navigate(...)` is the authorized store/controller seam;
        // only bare routes.ts calls are direct browser writes.
        .filter((l) => /(?:^|[^\w.])(navigate|replaceRoute)\(/.test(l))
        .filter((l) => !allow.includes(l));
      expect(offenders).toEqual([]);
    });
  }

  it('the RouteSyncController is mounted exactly once per workspace', () => {
    const bind = read('bind/useRouteSyncController.ts');
    expect(bind).toMatch(/createRouteSyncController\(/);
    const model = read('workspace/useWorkspaceModel.tsx');
    const mounts = model.match(/useRouteSyncController\(/g) ?? [];
    expect(mounts).toHaveLength(1);
  });

  it('review-close preserves the raw history.back() carve-out', () => {
    expect(read('workspace/useWorkspaceModel.tsx')).toMatch(/window\.history\.back\(\)/);
  });
});

describe('pushVsReplace matrix — live spot-subset', () => {
  const base: RouteState = {
    projectId: 'p1',
    sheetId: 's1',
    actionKind: null,
    review: false,
    panel: null,
  };
  it('sheet change → push', () =>
    expect(pushVsReplace(base, { ...base, sheetId: 's2' })).toBe('push'));
  it('open row panel → push', () =>
    expect(pushVsReplace(base, { ...base, panel: { kind: 'row', rowId: 'r1' } })).toBe('push'));
  it('close row panel → replace', () =>
    expect(pushVsReplace({ ...base, panel: { kind: 'row', rowId: 'r1' } }, base)).toBe('replace'));
  it('open action → push', () =>
    expect(pushVsReplace(base, { ...base, actionKind: 'geocode' })).toBe('push'));
  it('open review → push', () =>
    expect(pushVsReplace(base, { ...base, review: true })).toBe('push'));
  it('close review → replace', () =>
    expect(pushVsReplace({ ...base, review: true }, base)).toBe('replace'));
});
