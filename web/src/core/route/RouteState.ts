// The route-significant state subset + its codec and structural equality.
// Route-significant state ONLY: everything ephemeral (open split map/graph,
// documentView, promotedViews, gridOnly, compare/preview/lens, all draft grid
// state) is defined OUT of RouteState by construction, so it can never desync
// the URL.
//
// The codec does NOT reimplement URL parsing/formatting: it reuses routes.ts's
// Route/RoutePanel types and (in the bind write-adapter + tests) routes.ts's
// routePath formatter. This module keeps a type-only dependency on routes.ts so
// it stays framework-free at runtime (routes.ts's URL reader is React-based;
// none of that is pulled in by a `type` import).

import type { Route, RoutePanel } from '../../routes';

export interface RouteState {
  projectId: string;
  sheetId: string | null;
  actionKind: string | null;
  review: boolean;
  panel:
    | { kind: 'row'; rowId: string; columnId?: string }
    | { kind: 'column'; columnId: string }
    | { kind: 'sourceHealth'; sourceId: string }
    | null;
}

export type RouteStatePanel = RouteState['panel'];

/** Narrow routes.ts's RoutePanel (which still carries 'map'/'graph' for the URL
 *  codec) into RouteState.panel. map/graph are ENTRY points hydrated by
 *  projectRouteState, never carried in steady-state RouteState — so they
 *  narrow to null here. */
export function narrowPanel(panel: RoutePanel | undefined): RouteStatePanel {
  if (!panel) return null;
  switch (panel.kind) {
    case 'row':
      return { kind: 'row', rowId: panel.rowId, columnId: panel.columnId };
    case 'column':
      return { kind: 'column', columnId: panel.columnId };
    case 'sourceHealth':
      return { kind: 'sourceHealth', sourceId: panel.sourceId };
    case 'map':
    case 'graph':
      return null;
  }
}

export function panelsEqual(a: RouteStatePanel, b: RouteStatePanel): boolean {
  if (a === b) return true;
  if (a === null || b === null) return false;
  if (a.kind !== b.kind) return false;
  switch (a.kind) {
    case 'row':
      return b.kind === 'row' && a.rowId === b.rowId && a.columnId === b.columnId;
    case 'column':
      return b.kind === 'column' && a.columnId === b.columnId;
    case 'sourceHealth':
      return b.kind === 'sourceHealth' && a.sourceId === b.sourceId;
  }
}

/** Structural route-snapshot equality. Two snapshots with different object
 *  references but identical route meaning are equal — this is the whole re-entry
 *  guard: a write-induced synthetic popstate (routes.ts) that round-trips back
 *  to an equal RouteState must not re-notify subscribers. */
export function routeStatesEqual(a: RouteState, b: RouteState): boolean {
  return (
    a.projectId === b.projectId &&
    a.sheetId === b.sheetId &&
    a.actionKind === b.actionKind &&
    a.review === b.review &&
    panelsEqual(a.panel, b.panel)
  );
}

/** RouteState → routes.ts Route (the 'project' variant). The inverse of the
 *  props→RouteState narrowing (projectRouteState): formats back to exactly the
 *  URL routePath produces. RouteState.panel never holds map/graph, so mapping it
 *  to RoutePanel is total. */
export function routeStateToRoute(rs: RouteState): Route {
  return {
    kind: 'project',
    projectId: rs.projectId,
    sheetId: rs.sheetId ?? undefined,
    actionKind: rs.actionKind ?? undefined,
    review: rs.review ? true : undefined,
    panel: rs.panel ?? undefined,
  };
}
