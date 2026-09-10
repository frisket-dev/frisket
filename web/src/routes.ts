// Browser-history routing — no router dependency.
//
// Routes:
//   /                              → project picker
//   /p/{projectId}                 → project workspace
//   /p/{projectId}/s/{id}          → project workspace, specific sheet
//   /p/{projectId}/action/{kind}   → project workspace, open action form
//   /p/{projectId}/s/{id}/action/{kind}
//   /p/{projectId}/s/{id}/review   → project workspace, review overlay
//   /p/{projectId}/s/{id}/graph    → project workspace, graph neighborhood view
//   /p/{projectId}[/s/{id}]/source/{sourceId}/health
//                                   → project workspace, source health main view
//   /p/{projectId}/s/{id}/row/{rowId}[/column/{columnId}]
//   /p/{projectId}/s/{id}/column/{columnId}
//   /settings/personal/profile     → personal settings
//   /settings/organization/{section}
//   /p/{projectId}/settings/project/{section}
//   /account                       → redirects to /settings/personal/profile
//   /admin                         → admin overview

import { useEffect, useState } from 'react';
import { splitPathForRoute } from './routeContext';
import {
  SETTINGS_DEFAULT_SECTIONS,
  type SettingsScope,
} from './settings/settingsRegistry';

export type Route =
  | { kind: 'picker' }
  | { kind: 'admin' }
  | SettingsRoute
  | {
      kind: 'project';
      projectId: string;
      sheetId?: string;
      actionKind?: string;
      review?: boolean;
      panel?: RoutePanel;
    };

export type SettingsRoute = {
  kind: 'settings';
  projectId?: string;
  scope: SettingsScope;
  section: string;
};

// 'map'/'graph' are DEEP-LINK-ONLY panels: parseParts still accepts
// /s/{id}/graph and /s/{id}/map/column/{colId} so pasted links keep working —
// projectRouteState hydrates them into the chrome openSplit and immediately
// normalizes them out of the URL. No producer constructs them for
// serialization, so routePath has no map/graph branches anymore (see
// routePath's comment).
export type RoutePanel =
  | { kind: 'row'; rowId: string; columnId?: string }
  | { kind: 'column'; columnId: string }
  | { kind: 'map'; columnId: string }
  | { kind: 'graph' }
  | { kind: 'sourceHealth'; sourceId: string };

interface ParsedRoute {
  route: Route;
  canonicalize?: boolean;
}

const splitPath = (path: string) => splitPathForRoute(path);

const SETTINGS_SCOPES = new Set<SettingsScope>(['personal', 'project', 'organization']);

function isSettingsScope(value: string | undefined): value is SettingsScope {
  return value !== undefined && SETTINGS_SCOPES.has(value as SettingsScope);
}

function defaultSettingsSection(scope: SettingsScope): string {
  return SETTINGS_DEFAULT_SECTIONS[scope];
}

function parseGlobalSettings(parts: string[]): ParsedRoute {
  const parsedScope = parts[1];
  const scope: SettingsScope = isSettingsScope(parsedScope) ? parsedScope : 'personal';
  const section = parts[2] ?? defaultSettingsSection(scope);
  const route: SettingsRoute = { kind: 'settings', scope, section };
  return {
    route,
    canonicalize: parts.length < 3 || !isSettingsScope(parsedScope) || parts.length > 3,
  };
}

function parseProjectSettings(projectId: string, parts: string[], cursor: number): ParsedRoute {
  const parsedScope = parts[cursor + 1];
  const scope: SettingsScope = isSettingsScope(parsedScope) ? parsedScope : 'project';
  const section = parts[cursor + 2] ?? defaultSettingsSection(scope);
  const route: SettingsRoute = {
    kind: 'settings',
    projectId: scope === 'project' ? projectId : undefined,
    scope,
    section,
  };
  return {
    route,
    canonicalize: scope !== 'project' || parts.length < cursor + 3 || !isSettingsScope(parsedScope) || parts.length > cursor + 3,
  };
}

function parseParts(parts: string[]): ParsedRoute {
  if (parts[0] === 'account') {
    return {
      route: { kind: 'settings', scope: 'personal', section: 'profile' },
      canonicalize: true,
    };
  }
  if (parts[0] === 'settings') return parseGlobalSettings(parts);
  if (parts[0] === 'admin') return { route: { kind: 'admin' } };
  if (parts[0] === 'p' && parts[1]) {
    let cursor = 2;
    let sheetId: string | undefined;
    if (parts[cursor] === 's' && parts[cursor + 1]) {
      sheetId = parts[cursor + 1];
      cursor += 2;
    }
    if (parts[cursor] === 'settings') {
      return parseProjectSettings(parts[1], parts, cursor);
    }
    if (parts[cursor] === 'action' && parts[cursor + 1]) {
      return {
        route: {
          kind: 'project',
          projectId: parts[1],
          sheetId,
          actionKind: parts[cursor + 1],
        },
      };
    }
    if (parts[cursor] === 'review') {
      return { route: { kind: 'project', projectId: parts[1], sheetId, review: true } };
    }
    if (
      parts[cursor] === 'source' &&
      parts[cursor + 1] &&
      parts[cursor + 2] === 'health'
    ) {
      return {
        route: {
          kind: 'project',
          projectId: parts[1],
          sheetId,
          panel: { kind: 'sourceHealth', sourceId: parts[cursor + 1] },
        },
      };
    }
    if (sheetId && parts[cursor] === 'graph') {
      return {
        route: {
          kind: 'project',
          projectId: parts[1],
          sheetId,
          panel: { kind: 'graph' },
        },
      };
    }
    if (sheetId && parts[cursor] === 'row' && parts[cursor + 1]) {
      const rowId = parts[cursor + 1];
      const columnId =
        parts[cursor + 2] === 'column' && parts[cursor + 3]
          ? parts[cursor + 3]
          : undefined;
      return {
        route: {
          kind: 'project',
          projectId: parts[1],
          sheetId,
          panel: { kind: 'row', rowId, columnId },
        },
      };
    }
    if (
      sheetId &&
      parts[cursor] === 'map' &&
      parts[cursor + 1] === 'column' &&
      parts[cursor + 2]
    ) {
      return {
        route: {
          kind: 'project',
          projectId: parts[1],
          sheetId,
          panel: { kind: 'map', columnId: parts[cursor + 2] },
        },
      };
    }
    if (sheetId && parts[cursor] === 'column' && parts[cursor + 1]) {
      return {
        route: {
          kind: 'project',
          projectId: parts[1],
          sheetId,
          panel: { kind: 'column', columnId: parts[cursor + 1] },
        },
      };
    }
    if (parts[2] === 's' && parts[3]) {
      return {
        route: { kind: 'project', projectId: parts[1], sheetId: parts[3] },
        canonicalize: parts.length > cursor,
      };
    }
    return {
      route: { kind: 'project', projectId: parts[1] },
      canonicalize: parts.length > cursor,
    };
  }
  return { route: { kind: 'picker' } };
}

export function routePath(r: Route): string {
  switch (r.kind) {
    case 'picker':
      return '/';
    case 'admin':
      return '/admin';
    case 'settings':
      if (r.scope === 'project' && r.projectId) {
        return `/p/${encodeURIComponent(r.projectId)}/settings/project/${encodeURIComponent(r.section)}`;
      }
      return `/settings/${encodeURIComponent(r.scope)}/${encodeURIComponent(r.section)}`;
    case 'project':
      {
        let path = `/p/${encodeURIComponent(r.projectId)}`;
        if (r.sheetId) path += `/s/${encodeURIComponent(r.sheetId)}`;
        if (r.actionKind) path += `/action/${encodeURIComponent(r.actionKind)}`;
        if (r.review) path += '/review';
        if (r.panel?.kind === 'row') {
          path += `/row/${encodeURIComponent(r.panel.rowId)}`;
          if (r.panel.columnId) path += `/column/${encodeURIComponent(r.panel.columnId)}`;
        }
        if (r.panel?.kind === 'column') {
          path += `/column/${encodeURIComponent(r.panel.columnId)}`;
        }
        // No 'map'/'graph' branches: those panels are deep-link-only (see the
        // RoutePanel comment). Nothing constructs them for serialization —
        // steady-state RouteState narrows them out (core/route/RouteState's
        // narrowPanel) and projectRouteState normalizes an incoming deep link
        // to the base URL. A pasted map/graph deep link therefore rewrites
        // straight to the base path; its hydration still runs off the parsed
        // Route object on the same load.
        if (r.panel?.kind === 'sourceHealth') {
          path += `/source/${encodeURIComponent(r.panel.sourceId)}/health`;
        }
        return path;
      }
  }
}

const currentSearch = (): string => window.location.search;

function commitRoute(r: Route, replace: boolean): void {
  const url = routePath(r) + currentSearch();
  if (replace) {
    window.history.replaceState(null, '', url);
  } else {
    window.history.pushState(null, '', url);
  }
  window.dispatchEvent(new PopStateEvent('popstate'));
}

/** Navigate with a history entry (back returns to the previous screen). */
export function navigate(r: Route): void {
  commitRoute(r, false);
}

/** Update the path without a history entry (sheet switches — avoids history spam). */
export function replaceRoute(r: Route): void {
  commitRoute(r, true);
}

/** Pure pathname → Route using the SAME parser readRoute() drives
 *  (splitPathForRoute + parseParts), minus readRoute's side effects (legacy-hash
 *  rewrite, canonicalize replaceState). Exported for the semantic idempotence
 *  helper (core/route/locationMatchesRouteState): a framework-free comparison
 *  must reuse this exact decode+parse so encoding-only URL spellings (trailing
 *  slash, %7E vs ~, upper/lower percent-escapes) collapse to one Route. Malformed
 *  percent-encoding → picker, matching readRoute(). */
export function parsePathname(pathname: string): Route {
  const path = splitPath(pathname);
  if (path.malformed) {
    return { kind: 'picker' };
  }
  return parseParts(path.parts).route;
}

function readRoute(): Route {
  const path = splitPath(window.location.pathname);
  if (path.malformed) {
    return { kind: 'picker' };
  }
  const parsed = parseParts(path.parts);
  if (parsed.canonicalize) {
    window.history.replaceState(null, '', routePath(parsed.route) + currentSearch());
  }
  return parsed.route;
}

/** The current route, kept in sync with back/forward. */
export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => readRoute());
  useEffect(() => {
    const onNav = () => {
      setRoute(readRoute());
    };
    window.addEventListener('popstate', onNav);
    return () => {
      window.removeEventListener('popstate', onNav);
    };
  }, []);
  return route;
}
