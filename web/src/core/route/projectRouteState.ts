// The ONE function that reads a raw incoming routePanel and decides what to do
// with it. There is exactly one place that looks at a raw routes.ts RoutePanel
// (whose type still carries 'map'/'graph') before narrowing it into
// RouteState.panel.
//
// map/graph are deep-link ENTRY points, not steady-state RouteState. They
// are hydrated as a side effect (chrome.hydrateOpenSplit) then normalized out of
// the URL (write(base, 'replace')) — ordered strictly AFTER the base projection
// is constructed, so the normalize always sees the sheetId/actionKind/review
// this projection just computed, never stale values.

import type { RoutePanel } from '../../routes';
import { narrowPanel, type RouteState } from './RouteState';

/** The interim adapter over the chrome-persisted openSplit. A future stage
 *  gives this a real chromeStore; until then bind/ implements it over the
 *  legacy setOpenSplit + the current sheet's geo_point validation. */
export interface ChromeHandle {
  hydrateOpenSplit(
    spec:
      | { kind: 'map'; sheetId: string; columnId: string }
      | { kind: 'graph'; sheetId: string },
  ): void;
}

export interface RouteProps {
  projectId: string;
  routeSheetId: string | null;
  routeActionKind: string | null;
  routeReview: boolean;
  routePanel: RoutePanel | undefined;
}

export function projectRouteState(
  props: RouteProps,
  chrome: ChromeHandle,
  write: (next: RouteState, mode: 'push' | 'replace') => void,
): RouteState {
  // URL-hygiene divergence: base.sheetId is the RAW routeSheetId; the old
  // normalize used the RESOLVED sheet id. They differ only for a deep link to
  // a nonexistent sheet id (already-broken edge): the UI falls back to the
  // first sheet while the normalized URL keeps the stale id. Accepted --
  // fixing it would couple this pure projection to resolved model state.
  const base: RouteState = {
    projectId: props.projectId,
    sheetId: props.routeSheetId,
    actionKind: props.routeActionKind,
    review: props.routeReview,
    panel: narrowPanel(props.routePanel),
  };

  const panel = props.routePanel;
  if (panel?.kind === 'map') {
    chrome.hydrateOpenSplit({ kind: 'map', sheetId: base.sheetId ?? '', columnId: panel.columnId });
    write(base, 'replace'); // drops the panel from the URL
  } else if (panel?.kind === 'graph') {
    chrome.hydrateOpenSplit({ kind: 'graph', sheetId: base.sheetId ?? '' });
    write(base, 'replace');
  }

  return base;
}
