import { useCallback, useEffect, useMemo, useRef } from 'react';
import { gridFromPresentation } from './viewDefinition';
import { groupManagedHiddenColumnNames } from './workspaceColumnVisibility';
import type {
  ColumnDef, GridFilterBboxValue, GridFilterEntityValue, GridFilterOperator,
  GridFilterSpec, GridSortDirection, ProjectInfo, SheetMeta, ViewSelection,
} from '../api/open';
import { useCurrentViewSelection } from '../bind/useCurrentViewSelection';
import { withSearch, type RowSearch } from './rowFiltering';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useGridViewHandle } from '../bind/useGridViewHandle';
import { useProjectDataResource, useRouteHandle } from '../bind/useRouteHandle';
import { useViewsHandle } from '../bind/useViewsHandle';
import { useSelectionHandle } from '../bind/useSelectionHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkViewHandle } from '../bind/useWorkViewHandle';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import type { useWorkspaceChromeState } from './useWorkspaceChromeState';
import {
  applyGridBboxFilterTransition, applyGridFilterTransition, applyGridSortTransition,
  clearGridFilterTransition, clearGridSortTransition, insertColumnBesideTransition,
  completeAddColumnTransition,
} from '../state/workspaceTransitions';
import { filterOperatorForColumnType, type HeaderMenuState } from './workspaceState';
import {
  clampFrozenColumnCount, columnOrderKey, exactViewHiddenColumns,
  filterConditionFromDraft, frozenColumnsKey, hasCustomColumnOrder,
  hiddenColumnsKey, shownDefaultColumnsKey, loadColumnOrder,
  loadFrozenColumnCount, loadHiddenColumns, normalizeColumnOrder, spliceColumnOrderBeside,
} from './gridColumnState';

/** Grid commands and their persisted display preferences share the existing grid handle. */
export function useWorkspaceGridController({
  project, sheet, chromeState, refreshHistory, refreshSheets, showError,
}: {
  project: ProjectInfo;
  sheet: SheetMeta | undefined;
  chromeState: Pick<ReturnType<typeof useWorkspaceChromeState>,
    'setOpenSplit' | 'setDocumentView' | 'setPromotedViews' | 'promotedViews'>;
  refreshHistory: () => Promise<unknown>;
  refreshSheets: () => Promise<unknown>;
  showError: (message: string) => void;
}) {
  const { projectApi } = useWorkspaceStores();
  const projectData = useProjectDataResource();
  const route = useRouteHandle();
  const gridView = useGridViewHandle();
  const detail = useDetailHandle();
  const selection = useSelectionHandle();
  const actSurface = useActSurfaceHandle();
  const workView = useWorkViewHandle();
  const viewsStore = useViewsHandle();
  const views = useSelector(viewsStore.store, (state) => state.views);
  const activeViewId = useSelector(gridView.store, (state) => state.activeViewId);
  const childFilter = useSelector(detail.store, (state) => state.childFilter);
  const headerMenu = useSelector(detail.store, (state) => state.headerMenu);
  const addColumnPrompt = useSelector(actSurface.store, (state) => state.addColumnPrompt);
  const activePromotedKey = useSelector(workView.store, (state) => state.activePromotedKey);
  const { setOpenSplit, setDocumentView, setPromotedViews, promotedViews } = chromeState;
  const currentSelection = useCurrentViewSelection(sheet);
  const closeHeaderMenu = useCallback(() => detail.closeHeaderMenu(), [detail]);
  const activeGridFilter = useSelector(gridView.store, (s) => s.applied.filter);
  const activeGridSort = useSelector(gridView.store, (s) => s.applied.sort);
  const scopeRowIds = useSelector(gridView.store, (s) => s.applied.scopeRowIds ?? null);
  const wrapText = useSelector(gridView.store, (s) => s.wrapText);
  const columnGroupSpecs = useSelector(gridView.store, (s) => s.columnGroupSpecs);
  const columnOrderBySheet = useSelector(gridView.store, (s) => s.columnOrderBySheet);
  const frozenColumnCountBySheet = useSelector(gridView.store, (s) => s.frozenColumnCountBySheet);
  const hiddenColumnsBySheet = useSelector(gridView.store, (s) => s.hiddenColumnsBySheet);
  const columnGroupsVersion = useSelector(gridView.store, (s) => s.columnGroupsVersion);
  const toggleWrapText = useCallback(() => {
    gridView.toggleWrap();
    localStorage.setItem('frisket:wrap-text', gridView.store.get().wrapText ? '1' : '0');
  }, [gridView]);

  const activeColumnOrder = useMemo(
    () => (sheet
      ? normalizeColumnOrder(
          columnOrderBySheet[sheet.id] ?? loadColumnOrder(project.id, sheet),
          sheet,
        )
      : []),
    [columnOrderBySheet, project.id, sheet],
  );
  const columnOrderIsCustom = sheet ? hasCustomColumnOrder(activeColumnOrder, sheet) : false;
  const storedActiveHiddenColumns = useMemo(
    () => (sheet ? hiddenColumnsBySheet[sheet.id] ?? loadHiddenColumns(project.id, sheet) : []),
    [hiddenColumnsBySheet, project.id, sheet],
  );
  const groupManagedHiddenColumns = useMemo(
    () => groupManagedHiddenColumnNames(columnGroupSpecs),
    [columnGroupSpecs],
  );
  const presentationEdited = useSelector(gridView.store, (state) => state.presentationEdited);
  const activeView = useMemo(
    () => activeViewId === null || !sheet
      ? null
      : views.find(
          (view) => view.id === activeViewId && String(view.sheet_id) === sheet.id,
        ) ?? null,
    [activeViewId, sheet, views],
  );
  const activeExactViewColumns = useMemo(() => {
    if (!sheet || presentationEdited || !activeView?.spec) return null;
    return gridFromPresentation(sheet, activeView.spec.presentation).columns;
  }, [activeView, presentationEdited, sheet]);
  const activeExactHiddenColumns = useMemo(() => {
    if (!sheet || activeExactViewColumns === null) return [];
    return exactViewHiddenColumns(sheet, activeExactViewColumns, groupManagedHiddenColumns);
  }, [activeExactViewColumns, groupManagedHiddenColumns, sheet]);
  const activeHiddenColumns = useMemo(
    () => [...new Set([...storedActiveHiddenColumns, ...activeExactHiddenColumns])],
    [activeExactHiddenColumns, storedActiveHiddenColumns],
  );

  useEffect(() => {
    if (!sheet || !activeView || activeExactViewColumns === null) return;
    if (JSON.stringify(storedActiveHiddenColumns) === JSON.stringify(activeExactHiddenColumns)) return;
    if (gridView.store.get().activeViewId !== activeView.id) return;
    gridView.reconcileViewHiddenColumns(
      activeView.id,
      sheet.id,
      activeExactHiddenColumns,
    );
    if (gridView.store.get().activeViewId === activeView.id) {
      localStorage.setItem(
        hiddenColumnsKey(project.id, sheet.id),
        JSON.stringify(activeExactHiddenColumns),
      );
    }
  }, [
    activeExactHiddenColumns,
    activeExactViewColumns,
    activeView,
    gridView,
    project.id,
    sheet,
    storedActiveHiddenColumns,
  ]);

  const activeDisplayHiddenColumns = useMemo(() => {
    const hidden = new Set(activeHiddenColumns);
    for (const name of groupManagedHiddenColumns) hidden.add(name);
    return [...hidden];
  }, [activeHiddenColumns, groupManagedHiddenColumns]);
  const currentViewColumns = useMemo(() => {
    if (!sheet) return null;
    const defaultOrder = sheet.columns.map((column) => column.name);
    const visible = activeColumnOrder.filter((name) => !activeDisplayHiddenColumns.includes(name));
    return columnOrderIsCustom || activeDisplayHiddenColumns.length > 0 ||
      JSON.stringify(visible) !== JSON.stringify(defaultOrder)
      ? visible
      : null;
  }, [activeColumnOrder, activeDisplayHiddenColumns, columnOrderIsCustom, sheet]);
  const headerMenuColumnStillVisible =
    !!headerMenu && !!sheet?.columns.some((column) => column.id === headerMenu.column.id);
  const visibleHeaderMenu = headerMenuColumnStillVisible ? headerMenu : null;
  const activeChildFilter =
    childFilter && sheet && childFilter.sheetId === sheet.id ? childFilter : null;
  const applyGridFilterForColumn = useCallback(
    (
      columnName: string,
      operatorIn: GridFilterOperator,
      rawValue: string,
      rawStart = '',
      rawEnd = '',
    ) => {
      if (!sheet || !columnName) return;
      const selectedColumn = sheet.columns.find((column) => column.name === columnName);
      const operator = filterOperatorForColumnType(selectedColumn?.type, operatorIn);
      const value = rawValue.trim();
      const start = rawStart.trim();
      const end = rawEnd.trim();
      const next: GridFilterSpec = {
        [columnName]: filterConditionFromDraft(operator, value, start, end),
      };

      applyGridFilterTransition({ gridView, detail, selection }, sheet.id, {
        filter: next,
      });

    },
    [detail, gridView, selection, sheet],
  );

  const applyGridBboxFilter = useCallback(
    (columnName: string, bbox: [number, number, number, number]) => {
      if (!sheet || !columnName) return;
      const value: GridFilterBboxValue = {
        min_lon: bbox[0],
        min_lat: bbox[1],
        max_lon: bbox[2],
        max_lat: bbox[3],
      };

      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheet.id,
        {
          filter: { [columnName]: { bbox: value } },
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      sheet,
    ],
  );

  const applyGridFilterSpec = useCallback(
    (filter: GridFilterSpec | null) => {
      if (!sheet) return;
      const [columnName, condition] = Object.entries(filter ?? {})[0] ?? [];
      if (!columnName || !condition) {
        clearGridFilterTransition({ gridView, selection }, sheet.id);
        return;
      }
      const previous = gridView.store.get().applied;
      const preservesEntityLabel = previous.filterValueLabel !== null
        && Object.entries(previous.filter ?? {}).some(([column, previousCondition]) => (
          previousCondition.entity_eq !== undefined
          && JSON.stringify(filter?.[column]?.entity_eq) === JSON.stringify(previousCondition.entity_eq)
        ));
      applyGridFilterTransition({ gridView, detail, selection }, sheet.id, {
        filter: filter ?? {},
        filterValueLabel: preservesEntityLabel ? previous.filterValueLabel : null,
      });
    },
    [detail, gridView, selection, sheet],
  );

  const applyGridFailedFilter = useCallback(
    (sheetId: string, columnId: string, outcome: string) => {
      const sourceSheet = projectData.store.get().sheets.find((candidate) => candidate.id === sheetId);
      const column = sourceSheet?.columns.find((candidate) => candidate.id === columnId);
      if (!column) return;
      route.navigate({ ...route.baseRoute(), sheetId, actionKind: null, review: false, panel: null }, 'push');
      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheetId,
        {
          filter: { [column.name]: { failed: outcome } },
          filterColumnIds: { [column.name]: Number(column.id) },
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      projectData,
      route,
    ],
  );

  const applyGridEntityFilter = useCallback(
    (columnName: string, value: GridFilterEntityValue, valueLabel?: string) => {
      if (!sheet || !columnName) return;
      applyGridBboxFilterTransition(
        { gridView, detail, selection, workView },
        { setOpenSplit, setDocumentView, setPromotedViews },
        { promotedViews, activePromotedKey },
        sheet.id,
        {
          filter: { [columnName]: { entity_eq: value } },
          filterValueLabel: valueLabel ?? null,
        },
      );
    },
    [
      activePromotedKey,
      detail,
      gridView,
      promotedViews,
      workView,
      selection,
      setDocumentView,
      setOpenSplit,
      setPromotedViews,
      sheet,
    ],
  );

  const applyGridSortForColumn = useCallback(
    (columnName: string, direction: GridSortDirection) => {
      if (!sheet || !columnName) return;

      applyGridSortTransition({ gridView, detail, selection }, sheet.id, {
        sort: [{ column: columnName, dir: direction }],
      });
    },
    [detail, gridView, selection, sheet],
  );

  const sortHeaderColumn = useCallback(
    (column: ColumnDef) => {
      const current = activeGridSort?.find((rule) => rule.column === column.name);
      const direction: GridSortDirection = current?.dir === 'asc' ? 'desc' : 'asc';
      applyGridSortForColumn(column.name, direction);
    },
    [activeGridSort, applyGridSortForColumn],
  );

  const openGridHeaderMenu = useCallback(
    ({ column, columnIndex, bounds }: HeaderMenuState) => {
      detail.openHeaderMenu({ column, columnIndex, bounds });
    },
    [detail],
  );

  const clearGridFilter = useCallback(() => {

    clearGridFilterTransition({ gridView, selection }, sheet?.id);
  }, [gridView, selection, sheet]);

  const clearGridSort = useCallback(() => {
    clearGridSortTransition({ gridView, selection }, sheet?.id);
  }, [gridView, selection, sheet]);

  // Row commands can run after a delay (a debounced search), so they read the
  // selection current at that moment, never the one from the render that
  // scheduled them.
  const currentSelectionRef = useRef(currentSelection);
  useEffect(() => {
    currentSelectionRef.current = currentSelection;
  }, [currentSelection]);

  const replaceSelection = useCallback(
    (patch: (current: ViewSelection) => Partial<ViewSelection>) => {
      const current = currentSelectionRef.current;
      if (!current) return;
      gridView.setSelection({ ...current, ...patch(current) });
    },
    [gridView],
  );

  /** `sheetId` is the sheet the search was typed on; a search that arrives
   *  after navigation moved to another sheet is dropped. */
  const applyRowSearch = useCallback(
    (search: RowSearch, sheetId: string) => {
      if (route.store.get().sheetId !== sheetId) return;
      const next = withSearch(
        { selection: currentSelectionRef.current, filter: gridView.store.get().applied.filter },
        search,
      );
      applyGridFilterSpec(next.filter);
      replaceSelection((current) => ({
        search: next.selectionSearch,
        order: next.selectionSearch
          ? current.order
          : current.order.filter((item) => !('score' in item && item.score === 'search')),
      }));
    },
    [applyGridFilterSpec, gridView, replaceSelection, route],
  );

  const applyRowLimit = useCallback(
    (limit: number | null) => replaceSelection(() => ({ limit })),
    [replaceSelection],
  );

  /** Row order: a column sort, a relevance score, or none (the sheet's own order). */
  const applyRowOrder = useCallback(
    (order: { column: string; dir: GridSortDirection } | { score: 'search' | 'similarity' } | null) => {
      if (order && 'column' in order) {
        applyGridSortForColumn(order.column, order.dir);
        return;
      }
      clearGridSortTransition({ gridView, selection }, sheet?.id);
      replaceSelection((current) => ({
        order: order ? [{ score: order.score, dir: 'desc' }] : current.order.filter((item) => !('score' in item)),
      }));
    },
    [applyGridSortForColumn, gridView, replaceSelection, selection, sheet],
  );

  const headerMenuActiveSort = useMemo(() => {
    if (!headerMenu) return null;
    return activeGridSort?.find((item) => item.column === headerMenu.column.name) ?? null;
  }, [activeGridSort, headerMenu]);

  const headerMenuActiveFilter = useMemo(() => {
    if (!headerMenu) return null;
    return activeGridFilter?.[headerMenu.column.name] ?? null;
  }, [activeGridFilter, headerMenu]);

  const changeColumnOrder = useCallback(
    (columns: string[]) => {
      if (!sheet) return;
      const next = normalizeColumnOrder(columns, sheet);
      gridView.setColumnOrder(sheet.id, next);
      localStorage.setItem(columnOrderKey(project.id, sheet.id), JSON.stringify(next));
    },
    [gridView, project.id, sheet],
  );

  const hiddenColumnNames = activeDisplayHiddenColumns;

  const headerMenuAdjacentHidden = useMemo(() => {
    if (!headerMenu) return [];
    const userHidden = new Set(activeHiddenColumns);
    const index = activeColumnOrder.indexOf(headerMenu.column.name);
    if (index === -1) return [];
    const run: string[] = [];
    for (let i = index - 1; i >= 0 && userHidden.has(activeColumnOrder[i]); i -= 1) {
      run.unshift(activeColumnOrder[i]);
    }
    for (let i = index + 1; i < activeColumnOrder.length && userHidden.has(activeColumnOrder[i]); i += 1) {
      run.push(activeColumnOrder[i]);
    }
    return run;
  }, [activeColumnOrder, activeHiddenColumns, headerMenu]);

  const visibleOrderedColumnNames = useMemo(() => {
    if (!sheet) return [];
    const hidden = new Set(hiddenColumnNames);
    return activeColumnOrder.filter((name) => !hidden.has(name));
  }, [activeColumnOrder, hiddenColumnNames, sheet]);
  const visibleOrderedColumns = useMemo(() => {
    if (!sheet) return [];
    const byName = new Map(sheet.columns.map((column) => [column.name, column]));
    return visibleOrderedColumnNames
      .map((name) => byName.get(name))
      .filter((column): column is ColumnDef => column !== undefined);
  }, [sheet, visibleOrderedColumnNames]);

  const activeFrozenColumnCount = useMemo(() => {
    if (!sheet) return 0;
    return clampFrozenColumnCount(
      frozenColumnCountBySheet[sheet.id] ??
        loadFrozenColumnCount(project.id, sheet.id, visibleOrderedColumnNames.length),
      visibleOrderedColumnNames.length,
    );
  }, [frozenColumnCountBySheet, project.id, sheet, visibleOrderedColumnNames.length]);

  const changeFrozenColumnCount = useCallback(
    (count: number) => {
      if (!sheet) return;
      const next = clampFrozenColumnCount(count, visibleOrderedColumnNames.length);
      gridView.setFrozenColumnCount(sheet.id, next);
      localStorage.setItem(frozenColumnsKey(project.id, sheet.id), String(next));
      closeHeaderMenu();
    },
    [closeHeaderMenu, gridView, project.id, sheet, visibleOrderedColumnNames.length],
  );

  const setHiddenColumns = useCallback(
    (updater: (previous: string[]) => string[]) => {
      if (!sheet) return;
      const previous = hiddenColumnsBySheet[sheet.id] ?? loadHiddenColumns(project.id, sheet);
      const known = new Set(sheet.columns.map((col) => col.name));
      const seen = new Set<string>();
      const next = updater(previous).filter(
        (name) => known.has(name) && !seen.has(name) && (seen.add(name), true),
      );
      const nextSet = new Set(next);
      const newlyShownDefaults = previous.filter(
        (name) => !nextSet.has(name) && sheet.columns.some((column) => column.name === name && column.defaultHidden),
      );
      if (newlyShownDefaults.length) {
        const key = shownDefaultColumnsKey(project.id, sheet.id);
        const stored = localStorage.getItem(key);
        const shown = new Set<string>(stored ? JSON.parse(stored) as string[] : []);
        for (const name of newlyShownDefaults) shown.add(name);
        localStorage.setItem(key, JSON.stringify([...shown]));
      }
      gridView.setHiddenColumns(sheet.id, next);
      localStorage.setItem(hiddenColumnsKey(project.id, sheet.id), JSON.stringify(next));
    },
    [gridView, hiddenColumnsBySheet, project.id, sheet],
  );

  const hideColumn = useCallback(
    (name: string) => {
      setHiddenColumns((previous) => [...previous, name]);
      closeHeaderMenu();
    },
    [closeHeaderMenu, setHiddenColumns],
  );

  const setRowTitleColumn = useCallback(
    (columnId: string) => {
      if (!sheet) return;
      closeHeaderMenu();
      void projectApi
        .setSheetTitleColumn(sheet.id, columnId)
        .then(() => refreshSheets())
        .catch((e: Error) => showError(e.message));
    },
    [sheet, projectApi, refreshSheets, showError, closeHeaderMenu],
  );

  const revealColumns = useCallback(
    (names: string[]) => {
      const drop = new Set(names);
      setHiddenColumns((previous) => previous.filter((name) => !drop.has(name)));
      closeHeaderMenu();
    },
    [closeHeaderMenu, setHiddenColumns],
  );

  const revealAllHiddenColumns = useCallback(() => {
    setHiddenColumns(() => []);
  }, [setHiddenColumns]);

  const openAddColumnPrompt = useCallback((anchor: { x: number; y: number }) => {
    actSurface.openAddColumnPromptAt(anchor);
  }, [actSurface]);

  const closeAddColumnPrompt = useCallback(
    () => actSurface.closeAddColumnPrompt(),
    [actSurface],
  );

  const insertColumnBeside = useCallback(
    (side: 'left' | 'right') => {
      if (!sheet || !headerMenu) return;
      const index = sheet.columns.findIndex((col) => col.id === headerMenu.column.id);
      const position = index === -1 ? null : index + 1 + (side === 'right' ? 1 : 0);

      insertColumnBesideTransition(
        { detail, actSurface },
        {
          position,
          anchor: {
            x: headerMenu.bounds.x,
            y: headerMenu.bounds.y + headerMenu.bounds.height,
          },
          anchorName: headerMenu.column.name,
          side,
        },
      );
    },
    [actSurface, detail, headerMenu, sheet],
  );

  const submitAddColumn = useCallback(
    async (name: string, type: string) => {
      if (!sheet) return;
      const trimmed = name.trim();
      if (!trimmed) return;
      const position = addColumnPrompt?.position ?? null;
      const anchorName = addColumnPrompt?.anchorName ?? null;
      const side = addColumnPrompt?.side ?? 'right';
      try {
        const added = await projectApi.addColumn(sheet.id, trimmed, { type, position });
        const next = anchorName
          ? spliceColumnOrderBeside(activeColumnOrder, added.name, anchorName, side)
          : [...activeColumnOrder.filter((column) => column !== added.name), added.name];
        completeAddColumnTransition({ gridView, actSurface }, sheet.id, next);
        localStorage.setItem(columnOrderKey(project.id, sheet.id), JSON.stringify(next));
        projectData.invalidate();
        await refreshSheets();
        void refreshHistory();
      } catch (e: unknown) {
        showError(e instanceof Error ? e.message : String(e));
      }
    },
    [actSurface, activeColumnOrder, addColumnPrompt, gridView, project.id, projectApi, projectData, refreshHistory, refreshSheets, sheet, showError],
  );

  return useMemo(() => ({
    activeGridFilter, activeGridSort, scopeRowIds, wrapText, columnGroupSpecs,
    columnGroupsVersion, activeColumnOrder, activeHiddenColumns, hiddenColumnNames,
    currentViewColumns, visibleHeaderMenu, activeChildFilter,
    activeFrozenColumnCount, visibleOrderedColumns, headerMenuActiveSort, headerMenuActiveFilter,
    headerMenuAdjacentHidden, toggleWrapText, changeColumnOrder,
    changeFrozenColumnCount, hideColumn, revealColumns, revealAllHiddenColumns, setRowTitleColumn,
    applyGridFilterForColumn, applyGridBboxFilter, applyGridFilterSpec, applyGridFailedFilter,
    applyGridEntityFilter, applyGridSortForColumn, sortHeaderColumn,
    applyRowSearch, applyRowLimit, applyRowOrder, clearGridFilter, clearGridSort, openGridHeaderMenu, closeHeaderMenu,
    openAddColumnPrompt, closeAddColumnPrompt, insertColumnBeside, submitAddColumn,
  }), [
    activeGridFilter, activeGridSort, scopeRowIds, wrapText, columnGroupSpecs,
    columnGroupsVersion, activeColumnOrder, activeHiddenColumns, hiddenColumnNames,
    currentViewColumns, visibleHeaderMenu, activeChildFilter,
    activeFrozenColumnCount, visibleOrderedColumns, headerMenuActiveSort, headerMenuActiveFilter,
    headerMenuAdjacentHidden, toggleWrapText, changeColumnOrder,
    changeFrozenColumnCount, hideColumn, revealColumns, revealAllHiddenColumns, setRowTitleColumn,
    applyGridFilterForColumn, applyGridBboxFilter, applyGridFilterSpec, applyGridFailedFilter,
    applyGridEntityFilter, applyGridSortForColumn, sortHeaderColumn,
    applyRowSearch, applyRowLimit, applyRowOrder, clearGridFilter, clearGridSort, openGridHeaderMenu, closeHeaderMenu,
    openAddColumnPrompt, closeAddColumnPrompt, insertColumnBeside, submitAddColumn,
  ]);
}
