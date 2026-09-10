// The URL→routeStore projection. useRoute() (routes.ts) remains the sole owner of
// URL→React-state DERIVATION; this effect watches the SAME route props App
// already derives and projects them into routeStore. It never DERIVES route
// state from window.location and does NOT listen to popstate — so there is still
// exactly one URL reader. It does read window.location.pathname for ONE purpose:
// a staleness gate (propsMatchPathname). Two rapid popstates can render Back#1's
// props, then Back#2 moves window.location before Back#1's effect runs;
// projecting Back#1's now-stale props would desync routeStore from the URL and
// the controller would misread the diff as a command and re-push the stale route.
// The gate compares this render's props against the CURRENT pathname and bails
// when they no longer match — the fresher render's own effect projects the right
// one. This is a consistency read, not a second URL→state owner.
//
// projectRouteState also hydrates the map/graph deep-link ENTRY points
// (chrome.hydrateOpenSplit) and normalizes them out of the URL as a side effect —
// this hook is the single place that runs it.
//
// chrome/write/route are caller-stable (a useMemo handle, a useCallback commit
// adapter, and the project-session store handle respectively), so listing them
// in the dep array satisfies react-hooks/exhaustive-deps without a suppression,
// and the effect still only re-runs when a route prop moves.
//
// `ready` gates only the map/graph SIDE EFFECTS (hydrate openSplit + normalize the
// URL): a cold map/graph deep link must NOT normalize the panel out of the URL
// before the sheet has loaded, or the split would be silently dropped. The base
// RouteState is ALWAYS projected regardless of `ready` so routeStore is never
// stale; when `ready` flips true the effect re-runs and the hydrate+normalize
// fires against the now-loaded sheet.

import { useEffect } from 'react';
import { parsePathname, type RoutePanel } from '../routes';
import { projectRouteState, type ChromeHandle } from '../core/route/projectRouteState';
import { propsMatchPathname } from '../core/route/locationMatchesRouteState';
import type { RouteStoreHandle } from '../state/routeStore';

export interface RouteProjectionProps {
  projectId: string;
  routeSheetId: string | null;
  routeActionKind: string | null;
  routeReview: boolean;
  routePanel: RoutePanel | undefined;
}

const NOOP_CHROME: ChromeHandle = { hydrateOpenSplit() {} };
const NOOP_WRITE = (): void => {};

export function useRouteProjection(
  props: RouteProjectionProps,
  route: RouteStoreHandle,
  chrome: ChromeHandle,
  ready: boolean,
): void {
  const { projectId, routeSheetId, routeActionKind, routeReview, routePanel } = props;
  useEffect(() => {
    // Staleness gate: if a faster popstate already moved the URL past the
    // location these props describe, bail — projecting stale props would desync
    // routeStore from the URL and the controller would re-push the stale route.
    // The fresher render's own effect projects the current location.
    if (
      !propsMatchPathname(
        { projectId, routeSheetId, routeActionKind, routeReview, routePanel },
        window.location.pathname,
        parsePathname,
      )
    ) {
      return;
    }
    // projectRouteState runs the map/graph hydrate+normalize side effects,
    // then returns the narrowed base snapshot. route.projectExternal applies the
    // structural-equality bail so a write-induced popstate echo of an
    // equal route is a no-op. When the sheet isn't loaded yet, the side-effect
    // deps are swapped for no-ops so only the base projection runs (see the
    // `ready` note above).
    const next = projectRouteState(
      { projectId, routeSheetId, routeActionKind, routeReview, routePanel },
      ready ? chrome : NOOP_CHROME,
      ready ? (normalized) => route.normalize(normalized) : NOOP_WRITE,
    );
    // route.projectExternal writes an EXTERNAL store (routeStore handle), not a parent
    // React setState — it forces no ancestor re-render and applies its own
    // structural-equality bail. This whole effect is a URL→store
    // projection with side effects (window.location staleness read, openSplit
    // hydrate, history normalize); it is not render-derivable. no-pass-data-to-
    // parent here is a false positive — allowlisted in doctor.config.ts.
    route.projectExternal(next);
  }, [projectId, routeSheetId, routeActionKind, routeReview, routePanel, route, chrome, ready]);
}
