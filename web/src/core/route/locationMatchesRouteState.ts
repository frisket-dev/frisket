// Semantic idempotence over URL pathnames. Two guards in the write-cutover need
// to answer "does this pathname ALREADY mean X?" WITHOUT raw string equality:
//
//   - commitRoute (the controller's single write adapter): skip a re-commit when
//     the URL already holds `next` — a browser back/forward feeds props→store to
//     a route the URL already carries, and re-committing would double-write
//     history. Raw `pathname === routePath(next)` re-pushed on ANY
//     non-canonical spelling — a trailing slash, %7E vs ~, upper vs lower
//     percent-escapes — because routeContext.ts decodes/normalizes path segments
//     while routePath emits canonical encodeURIComponent paths. Back then got
//     misread as a command and the controller re-pushed DURING Back → corruption.
//   - useRouteProjection (the passive URL→store projection): bail when a render's
//     props no longer describe the CURRENT location (rapid back/back can render
//     Back#1's props after Back#2 already moved window.location).
//
// Both reuse routes.ts's OWN parser (injected as `parse` — a value import would
// pull React into framework-free core/, so it stays type-only + injected; the
// established pattern, see RouteState.ts's type-only routes.ts dependency). The
// parser runs routeContext.ts's decode, so encoding-only spellings collapse to
// one Route before comparison.

import type { Route, RoutePanel } from '../../routes';
import { narrowPanel, routeStatesEqual, type RouteState } from './RouteState';

/** routes.ts's pure pathname parser (parsePathname), injected to keep core/
 *  framework-free. */
export type ParsePathname = (pathname: string) => Route;

/** A parsed Route → RouteState, or null when the pathname is NOT a steady-state
 *  project-workspace route:
 *   - picker/settings/admin are never RouteState (RouteState is always a project
 *     workspace route), and
 *   - a URL still carrying a map/graph panel is a transient deep-link ENTRY point,
 *     not steady-state RouteState. Returning null (rather than a
 *     panel-narrowed RouteState) forces a mismatch, so the normalize write —
 *     which drops the map/graph panel FROM the URL — is never mistaken for an
 *     idempotent no-op and always proceeds.
 *  Row/column/sourceHealth narrow via the SAME narrowPanel the props→RouteState
 *  projection uses (projectRouteState). */
export function routeToRouteState(route: Route): RouteState | null {
  if (route.kind !== 'project') return null;
  if (route.panel?.kind === 'map' || route.panel?.kind === 'graph') return null;
  return {
    projectId: route.projectId,
    sheetId: route.sheetId ?? null,
    actionKind: route.actionKind ?? null,
    review: route.review === true,
    panel: narrowPanel(route.panel),
  };
}

/** Does `pathname` (parsed by the app's own parser) ALREADY encode the steady-
 *  state RouteState `next`? Replaces commitRoute's raw string equality. */
export function locationMatchesRouteState(
  pathname: string,
  next: RouteState,
  parse: ParsePathname,
): boolean {
  const parsed = routeToRouteState(parse(pathname));
  return parsed !== null && routeStatesEqual(parsed, next);
}

/** Full RoutePanel equality INCLUDING map/graph — the props-level projection
 *  staleness check must see the raw panel (map/graph deep links carry it), unlike
 *  RouteState.panelsEqual which only compares the narrowed steady-state panels. */
export function routePanelsEqual(
  a: RoutePanel | undefined,
  b: RoutePanel | undefined,
): boolean {
  if (a === b) return true; // both undefined, or same reference
  if (!a || !b) return false;
  if (a.kind !== b.kind) return false;
  switch (a.kind) {
    case 'row':
      return b.kind === 'row' && a.rowId === b.rowId && a.columnId === b.columnId;
    case 'column':
      return b.kind === 'column' && a.columnId === b.columnId;
    case 'map':
      return b.kind === 'map' && a.columnId === b.columnId;
    case 'graph':
      return b.kind === 'graph';
    case 'sourceHealth':
      return b.kind === 'sourceHealth' && a.sourceId === b.sourceId;
  }
}

/** The route-significant props useRouteProjection derives from App's route (the
 *  same shape projectRouteState consumes). */
export interface RouteSignificantProps {
  projectId: string;
  routeSheetId: string | null;
  routeActionKind: string | null;
  routeReview: boolean;
  routePanel: RoutePanel | undefined;
}

/** Do these route props still describe `pathname` (the CURRENT location, parsed
 *  by the app's parser)? Guards the passive URL→store projection against a stale
 *  render: two rapid popstates can render Back#1's props, then Back#2 moves
 *  window.location BEFORE Back#1's effect runs; projecting Back#1's now-stale
 *  props would desync store from URL and the controller would misread the diff as
 *  a command and re-push the stale route. When the parsed location no longer
 *  matches the props the projection bails — the fresher render's own effect
 *  projects the right one. Compares the FULL panel (routePanelsEqual) so a
 *  map/graph deep link — whose props DO match the un-normalized URL — still
 *  projects and runs its B1 hydrate+normalize side effect. */
export function propsMatchPathname(
  props: RouteSignificantProps,
  pathname: string,
  parse: ParsePathname,
): boolean {
  const route = parse(pathname);
  if (route.kind !== 'project') return false;
  return (
    route.projectId === props.projectId &&
    (route.sheetId ?? null) === props.routeSheetId &&
    (route.actionKind ?? null) === props.routeActionKind &&
    (route.review === true) === props.routeReview &&
    routePanelsEqual(route.panel, props.routePanel)
  );
}
