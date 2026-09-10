// The route-driven data effect: a pure CONSUMER of routeStore. It reads the
// committed RouteState.panel and (a) syncs the drawer stores to it
// (detail.closeAll/syncColumn/syncRow/loadRow + selection.setSelectedColumnId)
// and (b) loads the row for a row panel.
//
// `alive`: the stale-async guard over the locateSheetRow→getSheetData fetch
// chain. This guards one effect invocation's async chain, not a
// recurring/superseding job stream; `api.locateSheetRow`/`getSheetData` take no
// AbortSignal, so a core/jobs epoch would only swap which primitive answers
// "am I still current," not what's guarded — a plain flag is the right tool
// here, not the epoch pattern jobStore uses.

import { useEffect } from 'react';
import type { ColumnDef, GridFilterSpec, GridSortSpec } from '../api/open';
import { panelsEqual } from '../core/route/RouteState';
import type { RouteStoreHandle } from '../state/routeStore';
import type { DetailStoreHandle } from '../state/detailStore';
import type { SelectionStoreHandle } from '../state/selectionStore';
import { useSelector } from './useSelector';
import { useWorkspaceStores } from './useWorkspaceStores';

export interface RouteRowFetchDeps {
  route: RouteStoreHandle;
  detail: DetailStoreHandle;
  selection: SelectionStoreHandle;
  sheet: { id: string; columns: ColumnDef[] } | undefined;
  activeChildFilterParentRowId: string | null;
  activeGridFilter: GridFilterSpec | null;
  activeGridSort: GridSortSpec | null;
  rowDrawerId: string | null;
  showError: (message: string) => void;
}

export function useRouteRowFetch(deps: RouteRowFetchDeps): void {
  const { projectApi: api } = useWorkspaceStores();
  const {
    route,
    detail,
    selection,
    sheet,
    activeChildFilterParentRowId,
    activeGridFilter,
    activeGridSort,
    rowDrawerId,
    showError,
  } = deps;

  // Consume the COMMITTED RouteState.panel, not the raw routePanel prop.
  // panelsEqual keeps this subscription from re-notifying on a value-equal
  // re-projection (the mandate for non-primitive selections, bind/useSelector).
  const panel = useSelector(route.store, (s) => s.panel, panelsEqual);

  useEffect(() => {
    if (!sheet) return;
    // Stale-window guard: the projection commits in its own passive effect,
    // so the render-captured panel can be one navigation behind the store
    // for a single effect pass (direct row A→B open: raw props say B while
    // the store still says A). Re-read the
    // committed panel at effect time and skip the pass if the snapshot is
    // stale — the projection's commit re-triggers this effect with the fresh
    // value, so nothing is lost and no obsolete locateSheetRow fires.
    if (!panelsEqual(route.store.get().panel, panel)) return;
    let alive = true;

    // map/graph are already narrowed out of RouteState.panel (null) — they, an
    // absent panel, and the full-area sourceHealth projection all reduce to
    // "no drawer".
    if (!panel || panel.kind === 'sourceHealth') {
      detail.closeAll();
      selection.setSelectedColumnId(null);
      return () => {
        alive = false;
      };
    }

    if (panel.kind === 'column') {
      const column = sheet.columns.find((col) => col.id === panel.columnId) ?? null;
      detail.syncColumn(column);
      selection.setSelectedColumnId(null);
      return () => {
        alive = false;
      };
    }

    // panel.kind === 'row'
    const columnId = sheet.columns.some((col) => col.id === panel.columnId)
      ? panel.columnId ?? null
      : null;
    detail.syncRow();
    selection.setSelectedColumnId(columnId);
    if (rowDrawerId === panel.rowId) {
      return () => {
        alive = false;
      };
    }

    const scope = {
      parentRowId: activeChildFilterParentRowId,
      filter: activeGridFilter,
      sort: activeGridSort,
    };
    void api
      .locateSheetRow(sheet.id, panel.rowId, scope)
      .then((location) => {
        if (!location.found || location.pageOffset === null) return null;
        return api
          .getSheetData(sheet.id, location.pageOffset, location.pageSize, scope)
          .then((page) => page.rows.find((candidate) => candidate.id === panel.rowId) ?? null);
      })
      .then((row) => {
        if (alive) {
          detail.loadRow(row);
        }
      })
      .catch((e: Error) => {
        if (alive) showError(e.message);
      });
    return () => {
      alive = false;
    };
  }, [
    panel,
    // route.store backs the stale-window guard (route.store.get().panel above).
    // The handle is stable for a project's store lifetime, so listing it adds no
    // in-session refetch churn; it makes the guard's dependency explicit so a
    // route-store swap (a new project's stores) re-runs the fetch rather than
    // relying on `route` staying referentially fixed.
    route.store,
    sheet,
    detail,
    selection,
    activeChildFilterParentRowId,
    activeGridFilter,
    activeGridSort,
    rowDrawerId,
    showError,
  ]);
}
